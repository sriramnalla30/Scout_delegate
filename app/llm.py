import json
import uuid

from groq import BadRequestError
from langchain.agents.middleware import AgentMiddleware, ModelResponse, ModelRetryMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_groq import ChatGroq
from pydantic import BaseModel, ValidationError

from app import config


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
