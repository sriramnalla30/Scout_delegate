"""Web server: serves the chat page and streams the coordinator's steps as server-sent events.

The page can also be hosted elsewhere (static/ is a complete site): it then calls this API across sites, which
ALLOWED_ORIGINS must name. Who may call what is decided in app/access.py.
"""
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import access, config, observability, places, searches, store
from app.coordinator import forget_thread, stream_events, thread_state

STATIC = Path(__file__).resolve().parent.parent / 'static'
FILES = {'/': ('index.html', 'text/html'), '/config.js': ('config.js', 'text/javascript'), '/app.js': ('app.js', 'text/javascript'),
         '/style.css': ('style.css', 'text/css'), '/places.js': ('places.js', 'text/javascript'), '/places.css': ('places.css', 'text/css')}
THREAD_ID = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
logger = logging.getLogger('scout.server')


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(observability.sync_prompts)  # save the system prompts in Langfuse (new version only if changed)
    yield
    await asyncio.to_thread(observability.flush)  # do not lose the last traces on shutdown


router = APIRouter()


async def denied(_request: Request, error: access.Denied):
    return JSONResponse({'error': error.message}, status_code=error.status)


async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'  # the browser never keeps an API answer
    return response


def create_app() -> FastAPI:
    """The web app with the current settings (config.ALLOWED_ORIGINS decides whether a page on another site may use the API)."""
    app = FastAPI(title='Scout place finder', docs_url=None, redoc_url=None, lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(access.Denied, denied)
    app.middleware('http')(no_store)
    if config.ALLOWED_ORIGINS:
        app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_methods=['GET', 'POST', 'PUT', 'DELETE'],
                           allow_headers=['Content-Type', 'X-Client-Id'], max_age=600)
    return app


def friendly(error: Exception) -> str:
    name = type(error).__name__
    if name == 'RateLimitError':
        return 'The model is rate-limited right now. Wait a moment and try again.'
    if name in ('AuthenticationError', 'PermissionDeniedError'):
        return 'The Groq key was rejected. Check GROQ_API_KEY (in .env, or in the host\'s environment settings).'
    if isinstance(error, RuntimeError):
        return str(error)
    text = str(error)
    code = re.search(r"'code': '([a-z_]+)'", text)
    if code and code.group(1) in ('output_parse_failed', 'tool_use_failed'):
        return 'The model produced a reply that could not be parsed, even after retries. Send the message again.'
    return f'Something went wrong while running the agent ({name}' + (f': {code.group(1)}' if code else '') + ').'


def sse(event: dict) -> str:
    return f'data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n'


@router.get('/api/health')
def health():
    return {'ok': True, 'model': config.LLM_MODEL, 'key_configured': bool(config.GROQ_API_KEY),
            'langfuse': {'tracing': config.LANGFUSE_ENABLED, 'prompt_versions': observability.prompt_versions()}}


@router.get('/api/thread/{thread_id}')
def thread(thread_id: str):
    if not THREAD_ID.match(thread_id):
        return JSONResponse({'error': 'Bad thread id.'}, status_code=400)
    return thread_state(thread_id)


@router.get('/api/searches/{search_id}')
def search_get(search_id: str):
    """One stored search as a screen should see it now: places in the app's model, summary, data coverage, live open/closed."""
    doc = searches.load(search_id)
    return places.view(doc) if doc else JSONResponse({'error': 'Search not found.'}, status_code=404)


@router.get('/api/chats')
def chats_list(request: Request):
    return store.list_chats(access.owner_of(request))


@router.get('/api/chats/{chat_id}')
def chats_get(chat_id: str, request: Request):
    owner = access.owner_of(request)
    chat = store.load_chat(chat_id, owner) if THREAD_ID.match(chat_id) else None
    return chat or JSONResponse({'error': 'Chat not found.'}, status_code=404)


@router.put('/api/chats/{chat_id}')
async def chats_put(chat_id: str, request: Request):
    access.check_origin(request)
    owner = access.owner_of(request)
    try:
        body = await request.json()
        title, updated, log = str(body['title']), float(body['updated']), body['log']
        if not THREAD_ID.match(chat_id) or not isinstance(log, list):
            raise ValueError('bad chat')
        store.save_chat(chat_id, title, updated, log, owner)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({'error': 'Send {title, updated, log} for a valid chat id.'}, status_code=400)
    return {'ok': True}


@router.delete('/api/chats/{chat_id}')
def chats_delete(chat_id: str, request: Request):
    access.check_origin(request)
    owner = access.owner_of(request)
    if not THREAD_ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    if store.delete_chat(chat_id, owner) or owner is None:  # only the chat's owner can wipe its memory
        forget_thread(chat_id)
    return {'ok': True}


@router.post('/api/chat')
async def chat(request: Request):
    access.check_origin(request)
    access.limit_chat(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({'error': 'Send a JSON body.'}, status_code=400)
    thread_id, message = str(body.get('thread_id', '')), str(body.get('message', '')).strip()
    if not THREAD_ID.match(thread_id):
        return JSONResponse({'error': 'thread_id must be 8-64 letters, digits, - or _.'}, status_code=400)
    if not message or len(message) > 1000:
        return JSONResponse({'error': 'Write a message of 1 to 1000 characters.'}, status_code=400)

    def events():
        try:
            for event in stream_events(thread_id, message):
                yield sse(event)
        except Exception as error:  # surface a readable message instead of dropping the stream
            logger.warning('chat turn failed for thread %s: %s: %s', thread_id, type(error).__name__, str(error)[:1500])  # the person sees a short message; the host's log keeps the cause
            yield sse({'type': 'error', 'message': friendly(error)})
            yield sse({'type': 'done', 'waiting': False})

    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@router.get('/{path:path}')
def static_file(path: str):
    route = '/' + path
    if route not in FILES:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    name, mime = FILES[route]
    return Response((STATIC / name).read_bytes(), media_type=f'{mime}; charset=utf-8', headers={'Cache-Control': 'no-store'})


app = create_app()


if __name__ == '__main__':
    import uvicorn
    print(f'Scout place finder: http://{config.HOST}:{config.PORT}')
    # behind a host's proxy the visitor's address is in X-Forwarded-For (the rate limit needs it)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level='warning', proxy_headers=True, forwarded_allow_ips='*')
