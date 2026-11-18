"""Who may call the API: which sites, whose chat history, and how often.

Every check raises Denied; the server turns it into a JSON error. Nothing here knows about agents or places.
"""
import collections
import re
import threading
import time

from fastapi import Request

from app import config

CLIENT_ID = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
HOUR = 3600


class Denied(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def check_origin(request: Request) -> None:
    """A browser page from another site may not use this API (it could spend the model quota or touch the history).
    The site that serves the page, and the sites in ALLOWED_ORIGINS, may."""
    origin = (request.headers.get('origin') or '').rstrip('/')
    host = request.headers.get('host')
    if origin and origin not in (f'http://{host}', f'https://{host}') and origin not in config.ALLOWED_ORIGINS:
        raise Denied(403, 'Cross-origin request rejected.')


def owner_of(request: Request) -> str | None:
    """Whose chats this request may see: None when the history is shared, else the id the browser sends in X-Client-Id."""
    if config.CHAT_SCOPE != 'per_browser':
        return None
    client = request.headers.get('x-client-id', '')
    if not CLIENT_ID.match(client):
        raise Denied(400, 'Send your browser id in the X-Client-Id header.')
    return client


_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_lock = threading.Lock()


def limit_chat(request: Request) -> None:
    """At most CHAT_RATE_PER_HOUR messages an hour from one address (0 switches the limit off)."""
    cap = config.CHAT_RATE_PER_HOUR
    if cap <= 0:
        return
    who, now = request.client.host if request.client else 'unknown', time.time()
    with _lock:
        recent = _hits[who]
        while recent and now - recent[0] > HOUR:
            recent.popleft()
        if len(recent) >= cap:
            raise Denied(429, 'That is a lot of messages for one hour. Please try again a little later.')
        recent.append(now)
        if len(_hits) > 5000:  # forget addresses that have been quiet for an hour
            for key in [k for k, q in _hits.items() if not q or now - q[-1] > HOUR]:
                del _hits[key]
