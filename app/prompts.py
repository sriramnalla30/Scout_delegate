"""
Every LLM prompt comes from Langfuse Prompt Management - no prompt text is written in the code.

Edit a prompt in Langfuse (us.cloud.langfuse.com -> Prompts), give the new version the
"production" label, and the app uses it within a minute. No redeploy needed.

Use it like this:
    answer = await ainvoke_prompt(llm, "pick-winner", user_query="...", candidates="...")

    1. loads the "production" version of the prompt from Langfuse (kept in memory for 60 s)
    2. fills in its {{variables}}
    3. calls the LLM with it, and links that LLM call to the prompt version in the trace

The prompts/ folder holds a saved copy of each prompt for reading and for git history.
The app never reads it. Sync it with: python scripts/sync_prompts.py pull | push
"""
import asyncio
import base64
import json
import logging
import re
import time
import urllib.request
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger(__name__)

# All prompts the app needs (checked at startup)
PROMPT_NAMES = [
    "query-parser",
    "display-decision",
    "pick-winner",
    "review-extractor",
    "negotiation-judge",
    "negotiation-drafter",
]

CACHE_SECONDS = 60
VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# The prompt being used right now; app/telemetry.py writes it on the LLM spans
active_prompt: ContextVar["Prompt | None"] = ContextVar("active_prompt", default=None)


class PromptUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    messages: list[dict]  # [{"role": "system" | "user" | "assistant", "content": "... {{variable}} ..."}]

    @property
    def variables(self) -> set[str]:
        return {v for m in self.messages for v in VARIABLE.findall(m["content"])}

    def compile(self, **values) -> list[dict]:
        """Fill in the {{variables}}. Values are inserted as-is (never re-scanned for {{...}})."""
        missing = self.variables - values.keys()
        if missing:
            raise ValueError(f"Prompt '{self.name}' needs values for: {', '.join(sorted(missing))}")
        return [
            {"role": m["role"], "content": VARIABLE.sub(lambda match: str(values[match.group(1)]), m["content"])}
            for m in self.messages
        ]


_cache: dict[str, tuple[float, Prompt]] = {}


def get_prompt(name: str) -> Prompt:
    """The production version of a prompt, from memory if fetched in the last 60 s, else from Langfuse."""
    cached = _cache.get(name)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    try:
        prompt = _download(name)
    except Exception as e:
        if cached:  # Langfuse is unreachable: keep using the version we already have
            logger.warning(f"Langfuse unreachable, reusing prompt '{name}' v{cached[1].version}: {e}")
            _cache[name] = (time.monotonic(), cached[1])
            return cached[1]
        raise PromptUnavailable(
            f"Could not load prompt '{name}' (label '{settings.langfuse_prompt_label}') from Langfuse: {e}. "
            "Check the LANGFUSE_* settings, or upload the saved copies: python scripts/sync_prompts.py push"
        ) from e

    _cache[name] = (time.monotonic(), prompt)
    return prompt


def _download(name: str) -> Prompt:
    # urllib on purpose: calls made with `requests` are traced, and these would only be noise
    url = f"{settings.langfuse_host.rstrip('/')}/api/public/v2/prompts/{quote(name)}?label={quote(settings.langfuse_prompt_label)}"
    token = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)

    # "chat" prompts are a list of messages; "text" prompts are one string (sent as a user message)
    body = data["prompt"]
    messages = body if data.get("type") == "chat" else [{"role": "user", "content": body}]
    return Prompt(name=data["name"], version=data["version"], messages=[{"role": m["role"], "content": m["content"]} for m in messages])


async def ainvoke_prompt(llm, name: str, **values):
    """Load prompt `name`, fill in `values`, and call `llm` with it (async)."""
    prompt = await asyncio.to_thread(get_prompt, name)
    token = active_prompt.set(prompt)
    try:
        return await llm.ainvoke(prompt.compile(**values))
    finally:
        active_prompt.reset(token)


def invoke_prompt(llm, name: str, **values):
    """Same as ainvoke_prompt, for normal (blocking) code."""
    prompt = get_prompt(name)
    token = active_prompt.set(prompt)
    try:
        return llm.invoke(prompt.compile(**values))
    finally:
        active_prompt.reset(token)


def check_prompts() -> None:
    """Called at startup: load every prompt once, and say clearly which ones are missing."""
    missing = []
    for name in PROMPT_NAMES:
        try:
            get_prompt(name)
        except PromptUnavailable:
            missing.append(name)
    if missing:
        logger.error(
            f"Missing prompts in Langfuse: {', '.join(missing)}. "
            "Upload the saved copies with: python scripts/sync_prompts.py push"
        )
    else:
        logger.info(f"Loaded {len(PROMPT_NAMES)} prompts from Langfuse (label '{settings.langfuse_prompt_label}')")
