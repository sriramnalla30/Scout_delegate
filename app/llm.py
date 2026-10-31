from groq import BadRequestError
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_groq import ChatGroq

from app import config


def get_llm() -> ChatGroq:
    if not config.GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY is not set. Add it to .env (see .env.example).')
    return ChatGroq(model=config.LLM_MODEL, api_key=config.GROQ_API_KEY, temperature=0,
                    reasoning_effort=config.LLM_REASONING_EFFORT, max_retries=2, timeout=60)


RETRYABLE_CODES = ('output_parse_failed', 'tool_use_failed')


def _is_unparseable_output(error: Exception) -> bool:
    """gpt-oss sometimes writes its reasoning where a tool call belongs; Groq answers 400.
    The same request usually succeeds on the next try, so only these codes are retried."""
    return isinstance(error, BadRequestError) and any(code in str(error) for code in RETRYABLE_CODES)


def retry_middleware() -> ModelRetryMiddleware:
    return ModelRetryMiddleware(max_retries=3, retry_on=_is_unparseable_output, on_failure='error',
                                initial_delay=0.4, backoff_factor=1.5, jitter=False)
