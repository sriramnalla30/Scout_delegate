"""
Nodes of the NEGOTIATION path. One run of this path = one message turn:

    check_negotiation -> (deal done / rejected / waiting) -> END
                      -> draft_message -> ⏸ PAUSE for human approval -> send_message -> END

The pause happens because graph.py compiles the graph with interrupt_before=["send_message"].
The graph is started again by the API when the shopkeeper replies (see /agent/check-reply).
Prompts "negotiation-judge" and "negotiation-drafter" come from Langfuse (saved copies in prompts/).
"""
import asyncio
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.state import AgentState
from app.config import settings
from app.llm import get_llm
from app.prompts import ainvoke_prompt
from app.tools.chat import normalize_phone, send_message

logger = logging.getLogger(__name__)


class NegotiationStatus:
    """All values `negotiation_status` can have."""
    CONTINUE = "continue"                    # we should send another message
    WAITING_FOR_REPLY = "waiting_for_reply"  # message sent, waiting for the shopkeeper
    DEAL_REACHED = "deal_reached"            # shopkeeper agreed
    REJECTED = "rejected"                    # shopkeeper said no for good
    SEND_FAILED = "send_failed"                # Twilio did not send our message
    TERMINATED = "terminated"                # user pressed "End" in the chat


def get_target_place(state: AgentState) -> dict:
    """The place we are negotiating with (set by /agent/negotiate/start)."""
    return (state.get("recommendations") or [{}])[0]


def get_target_phone(state: AgentState) -> str:
    """FORCE_TARGET_NUMBER (testing) if set, else the shop's phone from SerpStack, else DEFAULT_TARGET_NUMBER."""
    return (
        normalize_phone(settings.force_target_number)
        or normalize_phone(get_target_place(state).get("phone", ""))
        or normalize_phone(settings.default_target_number)
    )


def format_history(history: list[dict]) -> str:
    if not history:
        return "(no messages yet - this is the first message)"
    return "\n".join(f"{'YOU' if m['role'] == 'agent' else 'SHOPKEEPER'}: {m['content']}" for m in history)


# ------------------------------------------------------------------
# Node 1: check_negotiation
# ------------------------------------------------------------------

class ReplyVerdict(BaseModel):
    status: Literal["continue", "deal_reached", "rejected"] = Field(
        description="continue = keep negotiating, deal_reached = they accepted, rejected = they refused for good"
    )
    reasoning: str = Field(description="One sentence explaining the verdict")


async def check_negotiation_node(state: AgentState) -> dict:
    """Look at the conversation so far and decide what happens next."""
    history = state.get("negotiation_history", [])

    # Nothing sent yet -> write the first message
    if not history:
        return {"negotiation_status": NegotiationStatus.CONTINUE}

    # Our message is the last one -> nothing to answer yet
    if history[-1]["role"] == "agent":
        return {"negotiation_status": NegotiationStatus.WAITING_FOR_REPLY}

    # The shopkeeper replied -> let the LLM judge the reply
    llm = get_llm(temperature=0.1).with_structured_output(ReplyVerdict)
    verdict = await ainvoke_prompt(
        llm, "negotiation-judge",
        target_price=state.get("target_price") or "not set",
        conversation=format_history(history),
    )
    logger.info(f"check_negotiation: {verdict.status} ({verdict.reasoning})")
    return {"negotiation_status": verdict.status}


def after_check(state: AgentState) -> str:
    """Edge: only draft a new message if we should keep negotiating."""
    return "draft_message" if state["negotiation_status"] == NegotiationStatus.CONTINUE else "done"


# ------------------------------------------------------------------
# Node 2: draft_message
# ------------------------------------------------------------------

class DraftedMessage(BaseModel):
    reasoning: str = Field(description="Your negotiation strategy, including how you used the reviews")
    message: str = Field(description="The exact message to send")


async def draft_message_node(state: AgentState) -> dict:
    """Let the LLM write the next message. It is NOT sent yet - a human must approve it first."""
    place = get_target_place(state)
    reviews = state.get("reviews", {}).get(place.get("name"), [])

    llm = get_llm(temperature=0.6).with_structured_output(DraftedMessage)
    draft = await ainvoke_prompt(
        llm, "negotiation-drafter",
        shop_name=place.get("name"),
        shop_address=place.get("address"),
        customer_goal=state.get("user_intent"),
        target_price=state.get("target_price") or "get the best deal possible",
        reviews="\n".join(reviews) if reviews else "No reviews available.",
        conversation=format_history(state.get("negotiation_history", [])),
    )
    logger.info(f"draft_message: {draft.message!r}")
    return {"planned_message": draft.message, "strategy_reasoning": draft.reasoning}


# ------------------------------------------------------------------
# Node 3: send_message  (only runs after the human approved - see /agent/approve)
# ------------------------------------------------------------------

async def send_message_node(state: AgentState) -> dict:
    """Send the approved message and remember it in the conversation history."""
    message = state["planned_message"]
    phone = get_target_phone(state)

    # send_message is normal blocking code, so run it in a background thread
    if not phone or not await asyncio.to_thread(send_message, phone, message):
        logger.error(f"send_message: could not send to {phone or '(no phone number)'}")
        return {"negotiation_status": NegotiationStatus.SEND_FAILED}

    logger.info(f"send_message: sent to {phone}: {message!r}")
    history = state.get("negotiation_history", []) + [
        {"role": "agent", "content": message, "timestamp": int(time.time())}
    ]
    return {"negotiation_history": history, "negotiation_status": NegotiationStatus.WAITING_FOR_REPLY}
