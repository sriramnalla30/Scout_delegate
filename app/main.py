"""
The web server (FastAPI). Run it with:

    python -m uvicorn app.main:app --reload

then open http://localhost:8000

Endpoints, in the order the user meets them:

  SEARCH
    POST /agent/start            parse the sentence, search SerpStack, save the first state
    GET  /agent/stream           run the search graph and stream each step to the browser (SSE)

  NEGOTIATION
    POST /agent/negotiate/start  user clicked "Negotiate" -> LLM drafts the first SMS
    POST /agent/approve          user approved/edited the draft -> SMS is sent
    GET  /agent/check-reply      browser asks every 10 s: did the shop reply? -> LLM drafts an answer
    POST /agent/send-message     user typed an SMS themselves
    POST /agent/terminate        user ended the negotiation
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent.graph import agent, thread_config
from app.agent.negotiation_nodes import NegotiationStatus, get_target_phone, get_target_place
from app.agent.query_parser import build_search_text, parse_user_query
from app.config import settings
from app.schemas import ApprovalRequest, ManualMessageRequest, NegotiationStartRequest, StartRequest, ThreadRequest
from app.prompts import check_prompts
from app.telemetry import setup_telemetry, shutdown_telemetry, tag_span
from app.tools.serpstack import search_places
from app.tools.sms import get_inbox, same_phone, send_sms

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load every prompt from Langfuse once, so a missing prompt is reported right at startup
    await asyncio.to_thread(check_prompts)
    yield
    # Server is stopping (e.g. Render puts the free app to sleep): send buffered traces first
    shutdown_telemetry()


app = FastAPI(title="Place Discover Agent", lifespan=lifespan)

# Traces of every request, agent step and LLM call -> Langfuse (off when LANGFUSE_TRACING=false)
setup_telemetry(app)

# Allow a frontend hosted on another domain to call this API
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    """Any crash -> log the full error and send {"detail": "..."} so the UI can show it."""
    logger.exception(f"Error in {request.url.path}")
    return JSONResponse(status_code=500, content={"detail": str(error)})


async def load_state(thread_id: str) -> dict:
    """Read a conversation's saved state, or answer 404 if the thread_id is unknown."""
    snapshot = await agent.aget_state(thread_config(thread_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Unknown thread_id. Please start a new search.")
    return snapshot.values


@app.get("/health")
async def health():
    return {"status": "ok"}


# ==================================================================
# SEARCH
# ==================================================================

@app.post("/agent/start")
async def start(request: StartRequest):
    """
    1. LLM turns the sentence into city / area / place type.
    2. SerpStack finds places.
    3. Everything is saved as the first state of a new thread. The graph itself
       runs when the browser opens /agent/stream, so it can watch every step live.
    """
    parsed = await parse_user_query(request.user_query)
    search_text = build_search_text(parsed)
    places = await asyncio.to_thread(search_places, search_text)

    thread_id = str(uuid.uuid4())
    tag_span(thread_id=thread_id, city=parsed.city, place_type=parsed.place_type, places_found=len(places))
    await agent.aupdate_state(thread_config(thread_id), {
        "user_query": request.user_query,
        "city": parsed.city,
        "place_type": parsed.place_type,
        "user_intent": parsed.intent,
        "serp_results": places,
        "route": "search",
    })

    return {
        "thread_id": thread_id,
        "parsed": parsed.model_dump(),
        "search_text": search_text,
        "places_found": len(places),
    }


@app.get("/agent/stream")
async def stream(thread_id: str):
    """
    Run the search graph and send one Server-Sent Event per finished step:
        {"type": "step", "node": "rank_places", ...}
        {"type": "step", "node": "fetch_reviews", ...}
        {"type": "step", "node": "pick_winner", ...}
        {"type": "complete", "recommendations": [...], "all_places": [...]}
    """
    config = thread_config(thread_id)
    tag_span(thread_id=thread_id)

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    async def events():
        try:
            snapshot = await agent.aget_state(config)
            if not snapshot.values:
                yield sse({"type": "error", "error": "Unknown thread_id"})
                return

            # snapshot.next is empty when the graph has already finished (e.g. page reconnected)
            if snapshot.next:
                # stream_mode="updates" -> after each node we get {node_name: what_it_returned}
                async for update in agent.astream(None, config, stream_mode="updates"):
                    for node, output in update.items():
                        yield sse(describe_step(node, output or {}))

            values = (await agent.aget_state(config)).values
            yield sse({
                "type": "complete",
                "recommendations": values.get("recommendations", []),
                "all_places": values.get("serp_results", []),
            })
        except Exception as e:
            logger.exception("Streaming failed")
            yield sse({"type": "error", "error": str(e)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def describe_step(node: str, output: dict) -> dict:
    """Turn one node's output into a small event for the browser."""
    event = {"type": "step", "node": node}
    if node == "rank_places":
        event["show_all"] = output.get("show_all", False)
        event["candidates"] = [p["name"] for p in output.get("recommendations", [])]
    elif node == "fetch_reviews":
        event["reviews"] = output.get("reviews", {})
    elif node == "pick_winner":
        event["summary"] = output.get("winner_summary", "")
    return event


# ==================================================================
# NEGOTIATION
# ==================================================================

@app.post("/agent/negotiate/start")
async def negotiate_start(request: NegotiationStartRequest):
    """
    Start the negotiation path of the graph for one place.
    The graph runs check_negotiation -> draft_message and then PAUSES before send_sms.
    """
    state = await load_state(request.thread_id)
    tag_span(thread_id=request.thread_id, place=request.place_name)

    place = next(
        (p for p in state.get("serp_results", []) if p["name"] == request.place_name),
        {"name": request.place_name, "address": "", "phone": ""},
    )

    await agent.ainvoke({
        "route": "negotiate",
        "recommendations": [place],
        "target_price": request.target_price,
        "user_intent": request.goal or f"Get the best price at {request.place_name}",
        "negotiation_history": [],
        "negotiation_start_time": int(time.time()),
    }, thread_config(request.thread_id))

    state = await load_state(request.thread_id)
    return {
        "place": place,
        "phone": get_target_phone(state),
        "planned_message": state.get("planned_message", ""),
        "reasoning": state.get("strategy_reasoning", ""),
    }


@app.post("/agent/approve")
async def approve(request: ApprovalRequest):
    """
    Human-in-the-loop: the graph is paused before send_sms.
    Approve -> (optionally replace the draft with the user's edit) -> resume -> SMS is sent.
    Reject  -> nothing is sent; we keep waiting for the shopkeeper.
    """
    config = thread_config(request.thread_id)
    tag_span(thread_id=request.thread_id, approved=request.approved)
    snapshot = await agent.aget_state(config)
    if snapshot.next != ("send_sms",):
        raise HTTPException(status_code=409, detail="There is no drafted message waiting for approval.")

    if not request.approved:
        await agent.aupdate_state(config, {"negotiation_status": NegotiationStatus.WAITING_FOR_REPLY})
        return {"status": "skipped"}

    if request.edited_message:
        await agent.aupdate_state(config, {"planned_message": request.edited_message})

    await agent.ainvoke(None, config)  # None = "continue where you paused"

    state = await load_state(request.thread_id)
    return {
        "status": state["negotiation_status"],  # "waiting_for_reply" or "sms_failed"
        "sent_message": state["planned_message"],
        "place_name": get_target_place(state).get("name", "Shop"),
        "phone": get_target_phone(state),
    }


@app.get("/agent/check-reply")
async def check_reply(thread_id: str):
    """
    Called by the browser every 10 seconds while waiting.
    If the shop answered: add the reply to the history and run the graph again,
    which judges the reply and (if needed) drafts our next SMS for approval.
    """
    tag_span(thread_id=thread_id)
    state = await load_state(thread_id)
    status = state.get("negotiation_status")
    if status != NegotiationStatus.WAITING_FOR_REPLY:
        return {"has_reply": False, "status": status}

    reply = await find_new_reply(state)
    if not reply:
        return {"has_reply": False, "status": status}

    history = state.get("negotiation_history", []) + [
        {"role": "shopkeeper", "content": reply["text"], "timestamp": reply["timestamp"]}
    ]
    # A new input starts a new run from START -> route is still "negotiate" -> check_negotiation
    await agent.ainvoke({"negotiation_history": history}, thread_config(thread_id))

    state = await load_state(thread_id)
    status = state["negotiation_status"]
    return {
        "has_reply": True,
        "reply_text": reply["text"],
        "status": status,  # "continue" (draft ready), "deal_reached" or "rejected"
        "planned_message": state.get("planned_message", "") if status == NegotiationStatus.CONTINUE else "",
        "reasoning": state.get("strategy_reasoning", "") if status == NegotiationStatus.CONTINUE else "",
    }


async def find_new_reply(state: dict) -> dict | None:
    """
    Look in the phone's inbox for the oldest SMS that:
      - comes from the shop we texted,
      - arrived after the negotiation started,
      - is not already in the conversation history.
    """
    shop_phone = get_target_phone(state)
    started_at = state.get("negotiation_start_time", 0)
    already_seen = {
        (m["content"], m.get("timestamp"))
        for m in state.get("negotiation_history", [])
        if m["role"] == "shopkeeper"
    }

    candidates = []
    for sms in await asyncio.to_thread(get_inbox):
        text = (sms.get("message") or sms.get("body") or "").strip()
        try:
            timestamp = int(sms.get("timestamp_unix") or 0)
        except ValueError:
            timestamp = 0

        if not text or not same_phone(str(sms.get("number", "")), shop_phone):
            continue
        if timestamp and timestamp < started_at:
            continue
        if (text, timestamp) in already_seen:
            continue
        candidates.append({"text": text, "timestamp": timestamp})

    return min(candidates, key=lambda r: r["timestamp"]) if candidates else None


@app.post("/agent/send-message")
async def send_manual_message(request: ManualMessageRequest):
    """The user typed their own SMS: send it directly, without the LLM."""
    state = await load_state(request.thread_id)
    phone = get_target_phone(state)
    if not phone:
        raise HTTPException(status_code=400, detail="This shop has no phone number and DEFAULT_TARGET_NUMBER is not set.")

    if not await asyncio.to_thread(send_sms, phone, request.message):
        return {"success": False}

    history = state.get("negotiation_history", []) + [
        {"role": "agent", "content": request.message, "timestamp": int(time.time())}
    ]
    await agent.aupdate_state(thread_config(request.thread_id), {
        "negotiation_history": history,
        "negotiation_status": NegotiationStatus.WAITING_FOR_REPLY,
    })
    return {"success": True}


@app.post("/agent/terminate")
async def terminate(request: ThreadRequest):
    """User pressed "End": stop looking for replies."""
    tag_span(thread_id=request.thread_id)
    await load_state(request.thread_id)
    await agent.aupdate_state(thread_config(request.thread_id), {"negotiation_status": NegotiationStatus.TERMINATED})
    return {"status": NegotiationStatus.TERMINATED}


# ==================================================================
# FRONTEND
# Registered last, so the /agent/... and /health routes above are matched first.
# Serves frontend/index.html at http://localhost:8000/
# ==================================================================

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
