"""The main agent. It keeps control of the whole flow:

    user message -> orchestrator (generic prompt) -> parser_agent(request)
                      parser says needs_input(question) -> ask_user(question) -> parser_agent(request + answer)
                      parser says ready(args)           -> orchestrator stops; the UI prints the args

The parser agent owns the knowledge of what the search tool needs (and calls geocode); the
orchestrator only routes. It never calls the search tool in this demo. Later agents (reviews, negotiation) plug in the same way: add a tool,
or hand off with a LangGraph Command.
"""
import json
import sqlite3
from functools import lru_cache

from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app import config
from app.llm import get_llm, retry_middleware
from app.prompts import load_prompt
from app.tools import ask_user, parser_agent


@lru_cache
def get_checkpointer() -> SqliteSaver:
    """The agent's memory, kept in a SQLite file so open conversations survive a restart."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(config.DATA_DIR / 'agent_memory.sqlite', check_same_thread=False))


@lru_cache
def get_orchestrator():
    return create_agent(get_llm(), tools=[ask_user, parser_agent], system_prompt=load_prompt('orchestrator'),
                        checkpointer=get_checkpointer(), middleware=[retry_middleware()], name='orchestrator')


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
    snapshot = get_orchestrator().get_state({'configurable': {'thread_id': thread_id}})
    return bool(snapshot.interrupts)


def thread_state(thread_id: str) -> dict:
    """Whether the agent's memory still holds this conversation and whether it awaits an answer."""
    snapshot = get_orchestrator().get_state({'configurable': {'thread_id': thread_id}})
    return {'exists': bool(snapshot.values), 'waiting': bool(snapshot.interrupts)}


def stream_events(thread_id: str, text: str):
    """Send one message (or the answer to a pending question) and yield plain-dict events:

    tool_call / tool_result   what the orchestrator called, and what came back
    sub                       what the parser sub-agent did inside parser_agent
    interrupt                 the question for the person; the run is now paused
    message                   the orchestrator's reply
    done                      end of this turn, with waiting=True if a question is pending
    """
    graph = get_orchestrator()
    config = {'configurable': {'thread_id': thread_id}}
    payload = Command(resume=text) if is_waiting(thread_id) else {'messages': [{'role': 'user', 'content': text}]}
    waiting = False
    for mode, chunk in graph.stream(payload, config, stream_mode=['updates', 'custom']):
        if mode == 'custom':
            yield {'type': 'sub', **chunk}
            continue
        if '__interrupt__' in chunk:
            waiting = True
            for item in chunk['__interrupt__']:
                yield {'type': 'interrupt', 'question': (item.value or {}).get('question', '')}
            continue
        for update in chunk.values():
            for message in (update or {}).get('messages', []):
                if message.type == 'ai' and message.tool_calls:
                    if _text(message.content).strip():  # the one-sentence note Scout writes before a tool call
                        yield {'type': 'thought', 'text': _text(message.content).strip()}
                    for call in message.tool_calls:
                        yield {'type': 'tool_call', 'agent': 'orchestrator', 'name': call['name'], 'args': call['args']}
                elif message.type == 'ai' and _text(message.content).strip():
                    yield {'type': 'message', 'text': _text(message.content).strip()}
                elif message.type == 'tool':
                    yield {'type': 'tool_result', 'agent': 'orchestrator', 'name': message.name, 'data': _json_or_text(message.content)}
    yield {'type': 'done', 'waiting': waiting}
