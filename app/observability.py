"""Langfuse: a trace for every run, and the system prompts saved there as versioned prompts.

Both are best-effort. If Langfuse is not configured or cannot be reached, the app runs the same.
"""
import atexit
import logging
from functools import lru_cache

from app import config
from app.prompts import load_prompt

log = logging.getLogger('scout.observability')

# local file in prompts/  ->  prompt name in Langfuse
PROMPTS = {'coordinator': 'coordinator', 'scout': 'scout-agent'}
_versions: dict[str, int] = {}


@lru_cache
def get_langfuse():
    """The Langfuse client, or None when tracing is off. Keys are passed in, not read from the environment."""
    if not config.LANGFUSE_ENABLED:
        return None
    from langfuse import Langfuse

    client = Langfuse(public_key=config.LANGFUSE_PUBLIC_KEY, secret_key=config.LANGFUSE_SECRET_KEY,
                      base_url=config.LANGFUSE_BASE_URL, environment=config.LANGFUSE_ENVIRONMENT)
    atexit.register(client.flush)  # short-lived runs (the CLI) must not lose their last traces
    return client


def sync_prompts() -> dict[str, int]:
    """Save prompts/*.md in Langfuse. A new version is created only when the text changed.
    The files stay the source of truth; Langfuse keeps the history of every version."""
    client = get_langfuse()
    if client is None:
        return {}
    for key, name in PROMPTS.items():
        text = load_prompt(key)
        try:
            try:
                current = client.get_prompt(name, type='text', label='production', cache_ttl_seconds=0)
            except Exception:  # not there yet (or unreachable: then the create below fails too and is logged)
                current = None
            if current is None or current.prompt != text:
                current = client.create_prompt(name=name, prompt=text, type='text', labels=['production'],
                                               commit_message=f'synced from prompts/{key}.md')
            _versions[key] = current.version
        except Exception as error:
            log.warning('could not save prompt %s in Langfuse: %s', name, type(error).__name__)
    return dict(_versions)


def prompt_versions() -> dict[str, int]:
    return dict(_versions)


def _last_text(messages, kind: str) -> str | None:
    for message in reversed(messages):
        if getattr(message, 'type', None) == kind:
            content = message.content
            return content if isinstance(content, str) else ''.join(b.get('text', '') for b in content if isinstance(b, dict))
    return None


def _clean_input(inputs):
    """What an agent was given: the person's text, not the whole LangGraph state."""
    if isinstance(inputs, dict) and inputs.get('messages'):
        text = _last_text([m if hasattr(m, 'type') else _as_message(m) for m in inputs['messages']], 'human')
        return [{'role': 'user', 'content': text}] if text is not None else inputs
    resume = getattr(inputs, 'resume', None)
    return [{'role': 'user', 'content': f'HITL answer: {resume}'}] if resume is not None else inputs


def _as_message(raw):
    from langchain_core.messages import convert_to_messages
    return convert_to_messages([raw])[0]


def _clean_output(outputs):
    """What an agent produced: its structured result, or its last reply, not the message list that began with the input."""
    if isinstance(outputs, dict):
        structured = outputs.get('structured_response')
        if structured is not None:
            return structured.model_dump() if hasattr(structured, 'model_dump') else structured
        text = _last_text(outputs.get('messages') or [], 'ai')
        if text:
            return text
    return outputs


def _text_of(content) -> str:
    return content if isinstance(content, str) else ''.join(b.get('text', '') for b in content if isinstance(b, dict))


_GRAPH_NODES = {'model', 'tools'}  # LangGraph's own steps; the model call and the tool call inside them are what matter


@lru_cache
def _handler_class():
    from langfuse.langchain import CallbackHandler

    class Handler(CallbackHandler):
        """The Langfuse handler without LangGraph's bookkeeping steps. Everything below a hidden step is attached to
        its parent instead, and an agent's input and output are what it was given and what it returned."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._hidden: dict = {}

        def _up(self, parent_run_id):
            while parent_run_id in self._hidden:
                parent_run_id = self._hidden[parent_run_id]
            return parent_run_id

        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
            if parent_run_id is not None and self.get_langchain_run_name(serialized, **kwargs) in _GRAPH_NODES:
                self._hidden[run_id] = parent_run_id
                return None
            return super().on_chain_start(serialized, _clean_input(inputs), run_id=run_id, parent_run_id=self._up(parent_run_id), **kwargs)

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
            if run_id in self._hidden:
                return None
            return super().on_chain_end(_clean_output(outputs), run_id=run_id, parent_run_id=self._up(parent_run_id), **kwargs)

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            if run_id in self._hidden:
                return None
            return super().on_chain_error(error, run_id=run_id, parent_run_id=self._up(parent_run_id), **kwargs)

    for name in ('on_chat_model_start', 'on_llm_start', 'on_tool_start', 'on_retriever_start',
                 'on_llm_end', 'on_llm_error', 'on_tool_end', 'on_tool_error', 'on_retriever_end', 'on_retriever_error'):
        def make(method):
            def wrapper(self, *args, parent_run_id=None, **kwargs):
                if method == 'on_tool_end' and args and getattr(args[0], 'type', None) == 'tool':
                    args = (_text_of(args[0].content),) + args[1:]  # show the tool's result, not the whole LangChain message
                return getattr(super(Handler, self), method)(*args, parent_run_id=self._up(parent_run_id), **kwargs)
            return wrapper
        setattr(Handler, name, make(name))
    return Handler


def trace_tags(tag_hitl: bool = False) -> list[str]:
    """Tags on every trace. The environment is also a tag, so a trace reads 'production' in the list as well as in the filter."""
    return ['scout', config.LANGFUSE_ENVIRONMENT] + (['hitl'] if tag_hitl else [])


class Turn:
    """One turn of a chat as one Langfuse trace: its input is what the person sent, its output is what they
    were shown (the reply, or the question the run paused on). The agent run is nested inside it.

    The span is not made current: the stream is read from different threads, and a thread-bound context
    would not survive that. The agent's own spans are attached to it by id instead."""

    def __init__(self, thread_id: str, name: str, text, tag_hitl: bool = False):
        self.config, self._span = {}, None
        client = get_langfuse()
        if client is None:
            return
        self._span = client.start_observation(name=name, as_type='chain', input=text,
                                              metadata={'prompt_versions': dict(_versions)})
        handler = _handler_class()(public_key=config.LANGFUSE_PUBLIC_KEY,
                                  trace_context={'trace_id': self._span.trace_id, 'parent_span_id': self._span.id})
        self.config = {'callbacks': [handler], 'run_name': 'Coordinator',
                       'metadata': {'langfuse_session_id': thread_id, 'langfuse_tags': trace_tags(tag_hitl)}}

    def hitl_asked(self, question: str) -> None:
        """Marks the point where the run paused to ask the person (human in the loop)."""
        if self._span is not None:
            self._span.start_observation(name='HITL: question sent to the person (run paused)', as_type='event',
                                         input={'question': question}, metadata={'hitl': 'asked'})

    def hitl_answered(self, question: str, answer: str) -> None:
        """Marks the point where the person's answer resumed the paused run."""
        if self._span is not None:
            self._span.start_observation(name='HITL: answer from the person (run resumed)', as_type='event',
                                         input={'question': question}, output={'answer': answer}, metadata={'hitl': 'answered'})

    def finish(self, output: str | None) -> None:
        if self._span is not None:
            self._span.update(output=[{'role': 'assistant', 'content': output}] if output else output)
            self._span.end()
            self._span = None


def start_turn(thread_id: str, name: str, text, tag_hitl: bool = False) -> Turn:
    """Turns of the same conversation share a Langfuse session (the chat's thread id)."""
    return Turn(thread_id, name, text, tag_hitl)


def flush() -> None:
    client = get_langfuse()
    if client is not None:
        client.flush()
