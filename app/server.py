"""Local web server: serves the chat UI and streams the orchestrator's steps as server-sent events."""
import json
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import config, store
from app.orchestrator import forget_thread, stream_events, thread_state

STATIC = Path(__file__).resolve().parent.parent / 'static'
FILES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
THREAD_ID = re.compile(r'^[A-Za-z0-9_-]{8,64}$')

app = FastAPI(title='Scout place finder', docs_url=None, redoc_url=None)


def friendly(error: Exception) -> str:
    name = type(error).__name__
    if name == 'RateLimitError':
        return 'The model is rate-limited right now. Wait a moment and try again.'
    if name in ('AuthenticationError', 'PermissionDeniedError'):
        return 'The Groq key was rejected. Check GROQ_API_KEY in .env.'
    if isinstance(error, RuntimeError):
        return str(error)
    text = str(error)
    code = re.search(r"'code': '([a-z_]+)'", text)
    if code and code.group(1) in ('output_parse_failed', 'tool_use_failed'):
        return 'The model produced a reply that could not be parsed, even after retries. Send the message again.'
    return f'Something went wrong while running the agent ({name}' + (f': {code.group(1)}' if code else '') + ').'


def sse(event: dict) -> str:
    return f'data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n'


@app.get('/api/health')
def health():
    return {'ok': True, 'model': config.LLM_MODEL, 'key_configured': bool(config.GROQ_API_KEY)}


def cross_origin(request: Request) -> JSONResponse | None:
    """Reject pages from other sites so they cannot spend this machine's model quota or touch its history."""
    origin = request.headers.get('origin')
    if origin and origin != f'http://{request.headers.get("host")}':
        return JSONResponse({'error': 'Cross-origin request rejected.'}, status_code=403)
    return None


@app.get('/api/thread/{thread_id}')
def thread(thread_id: str):
    if not THREAD_ID.match(thread_id):
        return JSONResponse({'error': 'Bad thread id.'}, status_code=400)
    return thread_state(thread_id)


@app.get('/api/chats')
def chats_list():
    return store.list_chats()


@app.get('/api/chats/{chat_id}')
def chats_get(chat_id: str):
    chat = store.load_chat(chat_id) if THREAD_ID.match(chat_id) else None
    return chat or JSONResponse({'error': 'Chat not found.'}, status_code=404)


@app.put('/api/chats/{chat_id}')
async def chats_put(chat_id: str, request: Request):
    if (blocked := cross_origin(request)) is not None:
        return blocked
    try:
        body = await request.json()
        title, updated, log = str(body['title']), float(body['updated']), body['log']
        if not THREAD_ID.match(chat_id) or not isinstance(log, list):
            raise ValueError('bad chat')
        store.save_chat(chat_id, title, updated, log)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({'error': 'Send {title, updated, log} for a valid chat id.'}, status_code=400)
    return {'ok': True}


@app.delete('/api/chats/{chat_id}')
def chats_delete(chat_id: str, request: Request):
    if (blocked := cross_origin(request)) is not None:
        return blocked
    if not THREAD_ID.match(chat_id):
        return JSONResponse({'error': 'Bad chat id.'}, status_code=400)
    store.delete_chat(chat_id)
    forget_thread(chat_id)
    return {'ok': True}


@app.post('/api/chat')
async def chat(request: Request):
    if (blocked := cross_origin(request)) is not None:
        return blocked
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
            yield sse({'type': 'error', 'message': friendly(error)})
            yield sse({'type': 'done', 'waiting': False})

    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@app.get('/{path:path}')
def static_file(path: str):
    route = '/' + path
    if route not in FILES:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    name, mime = FILES[route]
    return Response((STATIC / name).read_bytes(), media_type=f'{mime}; charset=utf-8', headers={'Cache-Control': 'no-store'})


if __name__ == '__main__':
    import uvicorn
    print('Scout place finder: http://127.0.0.1:8000')
    uvicorn.run(app, host='127.0.0.1', port=8000, log_level='warning')
