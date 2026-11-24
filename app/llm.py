import json
import logging
import uuid

from groq import BadRequestError
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRetryMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_groq import ChatGroq
from pydantic import BaseModel, ValidationError

from app import config

log = logging.getLogger('scout.llm')


def get_llm(name: str | None = None) -> ChatGroq:
    """`name` is what the model call is called in Langfuse, so each agent's calls can be told apart."""
    if not config.GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY is not set. Add it to .env (see .env.example).')
    return ChatGroq(model=config.LLM_MODEL, api_key=config.GROQ_API_KEY, temperature=0,
                    reasoning_effort=config.LLM_REASONING_EFFORT, max_retries=2, timeout=60, name=name)


RETRYABLE_CODES = ('output_parse_failed', 'tool_use_failed')


def _is_unparseable_output(error: Exception) -> bool:
    """gpt-oss sometimes writes its reasoning where a tool call belongs; Groq answers 400.
    The same request usually succeeds on the next try, so only these codes are retried."""
    return isinstance(error, BadRequestError) and any(code in str(error) for code in RETRYABLE_CODES)


def retry_middleware() -> ModelRetryMiddleware:
    return ModelRetryMiddleware(max_retries=3, retry_on=_is_unparseable_output, on_failure='error',
                                initial_delay=0.4, backoff_factor=1.5, jitter=False)


def _failed_generation(error: BadRequestError) -> str | None:
    """What the model wrote, when Groq refused it as `tool_use_failed` ("Tool choice is required, but model did not call a tool")."""
    body = getattr(error, 'body', None)
    body = body.get('error', body) if isinstance(body, dict) else None
    text = body.get('failed_generation') if isinstance(body, dict) and body.get('code') == 'tool_use_failed' else None
    return text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip() if isinstance(text, str) else None


class RecoverStructuredReply(AgentMiddleware):
    """The agent is forced to answer with its structured report (a tool call). gpt-oss sometimes writes that report as plain text
    instead; Groq then answers 400 `tool_use_failed` and returns the text in `failed_generation`. Retrying the same request can fail
    the same way every time (it did on a hosted deployment), so when the text is a valid report it is used as the answer."""

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except BadRequestError as error:
            recovered = self._recover(error, request)
            if recovered is None:
                raise
            return recovered

    @staticmethod
    def _recover(error: BadRequestError, request) -> ModelResponse | None:
        text = _failed_generation(error)
        specs = getattr(request.response_format, 'schema_specs', None) or []
        if not text or len(specs) != 1 or not (isinstance(specs[0].schema, type) and issubclass(specs[0].schema, BaseModel)):
            return None
        try:
            reply = specs[0].schema.model_validate_json(text)
        except (ValidationError, ValueError, json.JSONDecodeError):
            return None  # not a report: let the retry middleware ask again
        call_id = f'call_recovered_{uuid.uuid4().hex[:12]}'
        call = {'name': specs[0].name, 'args': reply.model_dump(mode='json'), 'id': call_id, 'type': 'tool_call'}
        note = request.response_format.tool_message_content or f'Returning structured response: {reply}'
        return ModelResponse(result=[AIMessage(content='', tool_calls=[call]), ToolMessage(content=note, tool_call_id=call_id, name=specs[0].name)],
                             structured_response=reply)


def recover_structured_reply() -> RecoverStructuredReply:
    return RecoverStructuredReply()


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content if isinstance(b, dict)) if isinstance(content, list) else ''


def _last_ai(response) -> AIMessage | None:
    messages = response.result if isinstance(response, ModelResponse) else [response]
    return next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)


def is_empty_reply(response) -> bool:
    """A model turn with no tool call and no text: nothing to run and nothing to show."""
    message = _last_ai(response)
    return message is not None and not message.tool_calls and not _text_of(message.content).strip()


REASONING_LADDER = {'low': 'medium', 'medium': 'high'}


def _stronger(model):
    """The same model asked to think a little harder, or the model itself when that is not possible."""
    effort = REASONING_LADDER.get(getattr(model, 'reasoning_effort', None))
    return model.model_copy(update={'reasoning_effort': effort}) if effort else model


class RetryEmptyReply(AgentMiddleware):
    """gpt-oss sometimes ends a turn with nothing at all (no tool call, no text, finish reason "stop"). It usually happens right after it wrote
    the one-sentence note before a tool call into its hidden reasoning: the call it meant to make never comes out. The person then sees a blank
    screen. An empty turn is never a decision, so the same request is asked again. The retries think a step harder: on the failing decision of
    a real chat that gave a tool call every time (8 of 8), where the first try gave one in only 4 of 8. The system prompt is not touched."""

    def __init__(self, retries: int = 3):
        super().__init__()
        self.retries = retries

    def wrap_model_call(self, request, handler):
        response = handler(request)
        for attempt in range(1, self.retries + 1):
            if not is_empty_reply(response):
                break
            log.warning('the model ended its turn with nothing (no tool call, no text): asking again (try %d of %d)', attempt, self.retries)
            request = request.override(model=_stronger(request.model))
            response = handler(request)
        return response


def retry_empty_reply(retries: int = 3) -> RetryEmptyReply:
    return RetryEmptyReply(retries)
