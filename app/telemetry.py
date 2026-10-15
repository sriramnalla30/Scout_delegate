"""
Observability: send traces of every request, agent step and LLM call to Langfuse.

Built on OpenTelemetry (OTel), the open standard for tracing; Langfuse receives the data
over OTLP and shows each search as a tree of steps with prompts, answers, tokens and cost.
See docs/observability/how-it-works.html for an animated explanation.

It is ON when the Langfuse keys are set and LANGFUSE_TRACING is not "false".
Set LANGFUSE_TRACING=false on your laptop to keep it light (prompts still load from Langfuse).

What gets recorded automatically:
    - every web request to FastAPI             (FastAPIInstrumentor)
    - every outgoing call made with `requests` (RequestsInstrumentor) - SerpStack, reviews, SMS
    - every LangGraph node and Groq LLM call   (LangChainInstrumentor from OpenInference)

Sessions: the web page creates one session id per browser tab and sends it with every request
(header X-Session-Id, or ?session_id= for the SSE stream). Every span of those requests gets
it, so Langfuse groups all traces of one visit under Sessions.

Prompts: each LLM call is linked to the Langfuse prompt version it used (see app/prompts.py).
"""
import base64
import logging
import re
from contextvars import ContextVar
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from opentelemetry import trace

from app.config import settings
from app.prompts import active_prompt

logger = logging.getLogger(__name__)

# Query parameters that must never leave the app: API keys, SMS recipient and text
SECRET_QUERY_PARAMS = {"access_key", "api_key", "apikey", "recipients", "message"}

# Session id of the request being handled (set by SessionIdMiddleware)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Created by setup_telemetry(); flushed by shutdown_telemetry()
_tracer_provider = None


def tag_span(**attributes) -> None:
    """Add app.* attributes to the current span, e.g. tag_span(city="Pune"). No-op when tracing is off."""
    trace.get_current_span().set_attributes({f"app.{k}": v for k, v in attributes.items() if v is not None})


def redact_url(url: str) -> str:
    """'...?access_key=abc&query=gym' -> '...?access_key=REDACTED&query=gym'"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [(k, "REDACTED" if k.lower() in SECRET_QUERY_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _redact_request_hook(span, request) -> None:
    """RequestsInstrumentor calls this when it starts a span: replace the stored URL with a safe one."""
    if not span.is_recording():
        return
    safe_url = redact_url(request.url)
    for key in ("url.full", "http.url"):  # new and old attribute name for the same thing
        if key in (span.attributes or {}):
            span.set_attribute(key, safe_url)


class SessionIdMiddleware:
    """
    Read the browser's session id from the X-Session-Id header (or ?session_id= for EventSource,
    which cannot send headers) and remember it for the whole request, including streamed responses.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        session_id = headers.get(b"x-session-id", b"").decode("latin-1")
        if not session_id:
            session_id = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("session_id", [""])[0]
        if not SESSION_ID_PATTERN.match(session_id):
            return await self.app(scope, receive, send)

        token = session_id_var.set(session_id)
        try:
            # The FastAPI server span was started before this middleware ran: tag it here.
            # Every span started later gets the id from _AppSpanProcessor.
            _set_session_attributes(trace.get_current_span(), session_id)
            await self.app(scope, receive, send)
        finally:
            session_id_var.reset(token)


def _set_session_attributes(span, session_id: str) -> None:
    # session.id = OpenTelemetry convention; langfuse.session.id = what Langfuse groups Sessions by
    span.set_attribute("session.id", session_id)
    span.set_attribute("langfuse.session.id", session_id)


def setup_telemetry(app) -> None:
    """Switch on tracing (to Langfuse, and optionally to Google Drive). Call once, right after creating the FastAPI app."""
    global _tracer_provider
    if not (settings.langfuse_tracing or settings.drive_traces):
        logger.info("Tracing is off (LANGFUSE_TRACING=false)")
        return

    # Imported here so that, when tracing is off, none of this is even loaded
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    class _AppSpanProcessor(SpanProcessor):
        """When a span starts: add the session id, and the prompt name + version if an LLM prompt is running."""

        def on_start(self, span, parent_context=None):
            if session_id := session_id_var.get():
                _set_session_attributes(span, session_id)
            if prompt := active_prompt.get():
                span.set_attribute("langfuse.observation.prompt.name", prompt.name)
                span.set_attribute("langfuse.observation.prompt.version", prompt.version)

    _tracer_provider = TracerProvider(resource=Resource.create({
        "service.name": settings.service_name,
        "deployment.environment": settings.deployment_environment,
    }))
    _tracer_provider.add_span_processor(_AppSpanProcessor())

    # Finished spans wait in a small in-memory buffer and are sent in batches, to every destination below
    destinations = []
    if settings.langfuse_tracing:
        token = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
        _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(  # every 5 s or 512 spans
            endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {token}"},
        )))
        destinations.append(f"Langfuse at {settings.langfuse_host}")
    if settings.drive_traces:
        try:
            from app.drive_export import DriveTraceExporter
            _tracer_provider.add_span_processor(BatchSpanProcessor(DriveTraceExporter(), schedule_delay_millis=15_000))
            destinations.append(f"Google Drive folder '{settings.drive_folder_name}'")
        except Exception as e:  # e.g. not logged in yet: keep the app running, without the Drive copy
            logger.warning(f"Saving traces to Google Drive is off: {e}")
    trace.set_tracer_provider(_tracer_provider)

    # Automatic instrumentation of the libraries we use
    FastAPIInstrumentor.instrument_app(
        app,
        # health pings, the page itself and static files are just noise
        excluded_urls=r"/health,\.(css|js|png|ico|svg)$,://[^/]+/(\?.*)?$",
        exclude_spans=["receive", "send"],  # no extra span per streamed SSE message
    )
    app.add_middleware(SessionIdMiddleware)
    RequestsInstrumentor().instrument(request_hook=_redact_request_hook)
    LangChainInstrumentor().instrument(tracer_provider=_tracer_provider)

    logger.info(f"Tracing is on: sending to {' and '.join(destinations) or 'nowhere'}")


def shutdown_telemetry() -> None:
    """Send whatever is still waiting in the buffer, then stop. Called when the server shuts down
    (Render's free plan stops idle apps, so without this the last few seconds of traces would be lost)."""
    global _tracer_provider
    if _tracer_provider:
        try:
            _tracer_provider.shutdown()  # shutdown() flushes first
        except Exception as e:
            logger.warning(f"Tracing shutdown failed: {e}")
        _tracer_provider = None
