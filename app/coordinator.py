"""The main agent. It keeps control of the whole flow:

    user message -> coordinator (generic prompt) -> scout_agent(request)
                      scout says needs_input(question) -> ask_user(question) -> scout_agent(request + answer)
                      scout says ready(args, search)    -> coordinator tells the person what was found; the UI shows the stored results

The scout agent owns the knowledge of what the search needs: it calls geocode, then search_places (the Google Maps
scraper). The full results never pass through a model; they are stored by id (app/searches.py) and the UI loads
them. The coordinator only routes and relays. Later agents (reviews, negotiation) plug in the same way: add a tool,
or hand off with a LangGraph Command.
"""
import json
import sqlite3
from functools import lru_cache

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app import config, observability
from app.llm import get_llm, retry_middleware
from app.prompts import load_prompt
from app.tools import ask_user, scout_agent


@lru_cache
def get_checkpointer() -> SqliteSaver:
    """The agent's memory, kept in a SQLite file so open conversations survive a restart."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(config.DATA_DIR / 'agent_memory.sqlite', check_same_thread=False))


@lru_cache
def get_coordinator():
    return create_agent(get_llm('Coordinator: model call'), tools=[ask_user, scout_agent], system_prompt=load_prompt('coordinator'),
                        checkpointer=get_checkpointer(), middleware=[retry_middleware()], name='Coordinator')


def forget_thread(thread_id: str) -> None:
    get_checkpointer().delete_thread(thread_id)


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


def is_waiting(thread_id: str) -> bool:
    """True when this conversation is paused on an ask_user question."""
    snapshot = get_coordinator().get_state({'configurable': {'thread_id': thread_id}})
    return bool(snapshot.interrupts)


def thread_state(thread_id: str) -> dict:
    """Whether the agent's memory still holds this conversation and whether it awaits an answer."""
    snapshot = get_coordinator().get_state({'configurable': {'thread_id': thread_id}})
    return {'exists': bool(snapshot.values), 'waiting': bool(snapshot.interrupts)}


def _turn_name(thread_id: str, text: str, waiting: bool) -> str:
    """"Turn 2 (answer): Indiranagar". Every message and every answer to a question starts a new turn."""
    messages = get_coordinator().get_state({'configurable': {'thread_id': thread_id}}).values.get('messages', [])
    asked = sum(1 for m in messages if m.type == 'ai' for c in m.tool_calls if c['name'] == 'ask_user')
    started = sum(1 for m in messages if m.type == 'human') + asked - (1 if waiting else 0)
    short = text if len(text) <= 60 else text[:57] + '...'
    if waiting:
        return f'Turn {started + 1} (HITL answer): {short}'
    return f'Turn {started + 1} (message): {short}'


def stream_events(thread_id: str, text: str):
    """Send one message (or the answer to a pending question) and yield plain-dict events:

    tool_call / tool_result   what the coordinator called, and what came back
    sub                       what the scout sub-agent did inside scout_agent
    interrupt                 the question for the person; the run is now paused
    message                   the coordinator's reply
    done                      end of this turn, with waiting=True if a question is pending
    """
    graph = get_coordinator()
    state_config = {'configurable': {'thread_id': thread_id}}
    waiting_now = is_waiting(thread_id)
    payload = Command(resume=text) if waiting_now else {'messages': [{'role': 'user', 'content': text}]}
    asked = ''
    if waiting_now:  # the question this answer replies to, so the trace shows the exchange and not a bare answer
        asked = ((get_coordinator().get_state(state_config).interrupts[0].value or {}).get('question', ''))
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
        yield {'type': 'done', 'waiting': waiting}
    finally:
        trace.finish(shown)
