"""The main agent. It keeps control of the whole flow:

    user message -> coordinator (generic prompt) -> scout_agent(request)
                      scout says needs_input(question)            -> ask_user(question) -> scout_agent(request + answer)
                      scout says needs_input, missing "location"  -> ask_browser() -> scout_agent(request + one sentence)
                      scout says ready(args, search)              -> coordinator tells the person what was found; the UI shows the stored results

The scout agent owns the knowledge of what the search needs: it calls geocode, then search_places (the Google Maps
scraper), or, for "near me" once the browser has given the position, the code searches around that position. The full results never pass through a model; they are stored by id (app/searches.py) and the UI loads
them. The coordinator only routes and relays. Later agents (reviews, negotiation) plug in the same way: add a tool,
or hand off with a LangGraph Command.
"""
import json
import sqlite3
from functools import lru_cache

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app import config, location, observability
from app.llm import get_llm, retry_empty_reply, retry_middleware
from app.prompts import load_prompt
from app.tools import ask_browser, ask_user, scout_agent


@lru_cache
def get_checkpointer() -> SqliteSaver:
    """The agent's memory, kept in a SQLite file so open conversations survive a restart."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(config.DATA_DIR / 'agent_memory.sqlite', check_same_thread=False))


@lru_cache
def get_coordinator():
    return create_agent(get_llm('Coordinator: model call'), tools=[ask_user, ask_browser, scout_agent], system_prompt=load_prompt('coordinator'),
                        checkpointer=get_checkpointer(), middleware=[retry_middleware(), retry_empty_reply()], name='Coordinator')


# Shown when a turn ends with nothing for the person (the model gave an empty reply even after asking again): never a blank screen.
SILENT_NOTICE = 'I could not finish that. Please send your message again.'


def forget_thread(thread_id: str) -> None:
    get_checkpointer().delete_thread(thread_id)
    location.forget(thread_id)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content if isinstance(b, dict))


def _json_or_text(content):
    text = _text(content)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def pending_kind(thread_id: str) -> str | None:
    """What this conversation is paused on: 'question' (ask_user, answered in words), 'browser' (ask_browser, answered by the
    device position) or None when it is not paused."""
    snapshot = get_coordinator().get_state({'configurable': {'thread_id': thread_id}})
    if not snapshot.interrupts:
        return None
    return 'browser' if (snapshot.interrupts[0].value or {}).get('kind') == 'browser' else 'question'


def is_waiting(thread_id: str) -> bool:
    """True when this conversation is paused on a question or on the browser position."""
    return pending_kind(thread_id) is not None


def thread_state(thread_id: str) -> dict:
    """Whether the agent's memory still holds this conversation, whether it awaits an answer, and which kind."""
    snapshot = get_coordinator().get_state({'configurable': {'thread_id': thread_id}})
    kind = 'browser' if snapshot.interrupts and (snapshot.interrupts[0].value or {}).get('kind') == 'browser' else 'question' if snapshot.interrupts else None
    return {'exists': bool(snapshot.values), 'waiting': bool(snapshot.interrupts), 'kind': kind}


def _turn_name(thread_id: str, text: str, waiting: bool) -> str:
    """"Turn 2 (answer): Indiranagar". Every message and every answer to a question starts a new turn."""
    messages = get_coordinator().get_state({'configurable': {'thread_id': thread_id}}).values.get('messages', [])
    asked = sum(1 for m in messages if m.type == 'ai' for c in m.tool_calls if c['name'] in ('ask_user', 'ask_browser'))
    started = sum(1 for m in messages if m.type == 'human') + asked - (1 if waiting else 0)
    short = text if len(text) <= 60 else text[:57] + '...'
    if waiting:
        return f'Turn {started + 1} (HITL answer): {short}'
    return f'Turn {started + 1} (message): {short}'


def browser_answer(thread_id: str, text: str, position: dict | None, error: str | None) -> str:
    """The answer to an ask_browser pause, as the one sentence the coordinator gets. The position itself goes into the holder
    (app/location.py) and nowhere else, so the conversation memory and the traces never contain it."""
    if position is not None:
        location.hold(thread_id, position)
        return 'Position received.'
    location.mark_unavailable(thread_id)
    if text:
        return f'The person typed instead: {text}.'
    return f'Position not available: {location.reason(error)}.'


def stream_events(thread_id: str, text: str, position: dict | None = None, position_error: str | None = None):
    """Send one message (or the answer to a pending question) and yield plain-dict events:

    tool_call / tool_result   what the coordinator called, and what came back
    sub                       what the scout sub-agent did inside scout_agent
    interrupt                 the question for the person; the run is now paused
    browser                   the browser is asked for the person's position; the run is now paused
    message                   the coordinator's reply
    done                      end of this turn, with waiting=True if a question or the position is pending

    `position` (a checked position) or `position_error` (a reason code) answers a pending browser request. They are
    ignored when nothing is waiting for a position, and `text` then answers like any message (typed instead)."""
    graph = get_coordinator()
    state_config = {'configurable': {'thread_id': thread_id}}
    kind = pending_kind(thread_id)
    waiting_now = kind is not None
    if kind == 'browser':
        text = browser_answer(thread_id, text if not (position or position_error) else '', position, position_error)
        payload = Command(resume=text)
    elif waiting_now:
        payload = Command(resume=text)
    else:
        payload = {'messages': [{'role': 'user', 'content': text}]}
    asked = ''
    if waiting_now:  # the question this answer replies to, so the trace shows the exchange and not a bare answer
        asked = 'Asked the browser for the position' if kind == 'browser' else ((get_coordinator().get_state(state_config).interrupts[0].value or {}).get('question', ''))
    # an answer shows as a chat: the question that was asked, then the person's labelled HITL answer
    chat_input = ([{'role': 'assistant', 'content': f'HITL question: {asked}'}, {'role': 'user', 'content': f'HITL answer: {text}'}]
                  if waiting_now else [{'role': 'user', 'content': text}])
    trace = observability.start_turn(thread_id, _turn_name(thread_id, text, waiting_now), chat_input, waiting_now)  # a Langfuse trace when configured
    if waiting_now:
        trace.hitl_answered(asked, text)
    run_config = {**state_config, **trace.config}
    waiting, shown = False, None  # shown: what the person ends up seeing, kept as the trace's output
    try:
        for mode, chunk in graph.stream(payload, run_config, stream_mode=['updates', 'custom']):
            if mode == 'custom':
                yield {'type': 'sub', **chunk}
                continue
            if '__interrupt__' in chunk:
                waiting = True
                for item in chunk['__interrupt__']:
                    if (item.value or {}).get('kind') == 'browser':
                        shown = 'Asks the browser for the position'
                        trace.hitl_asked(shown)
                        yield {'type': 'browser'}
                        continue
                    question = (item.value or {}).get('question', '')
                    shown = f'Asks the person: {question}'
                    trace.hitl_asked(question)
                    yield {'type': 'interrupt', 'question': question}
                continue
            for update in chunk.values():
                for message in (update or {}).get('messages', []):
                    if message.type == 'ai' and message.tool_calls:
                        if _text(message.content).strip():  # the one-sentence note the coordinator writes before a tool call
                            yield {'type': 'thought', 'text': _text(message.content).strip()}
                        for call in message.tool_calls:
                            yield {'type': 'tool_call', 'agent': 'coordinator', 'name': call['name'], 'args': call['args']}
                    elif message.type == 'ai' and _text(message.content).strip():
                        shown = _text(message.content).strip()
                        yield {'type': 'message', 'text': shown}
                    elif message.type == 'tool':
                        yield {'type': 'tool_result', 'agent': 'coordinator', 'name': message.name, 'data': _json_or_text(message.content)}
        if not waiting and shown is None:  # the turn ended with no reply and no question
            shown = SILENT_NOTICE
            yield {'type': 'error', 'message': SILENT_NOTICE}
        yield {'type': 'done', 'waiting': waiting}
    finally:
        location.release(thread_id)  # the position lives for one request only
        trace.finish(shown)
