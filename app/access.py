"""Who may call the API: which sites, which account, and how often.

Every check raises Denied; the server turns it into a JSON error. Nothing here knows about agents or places.
"""
import collections
import threading
import time

from fastapi import Request

from app import auth, config

HOUR = 3600
FAIL_WINDOW, FAIL_CAP = 900, 8       # sign-in tries: this many wrong passwords from one address for one email in 15 minutes
TRY_CAP, SIGNUP_CAP = 40, 10         # one address: sign-in attempts per 15 minutes, new accounts per hour


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


def bearer(request: Request) -> str | None:
    """The session token the page sends as `Authorization: Bearer <token>`."""
    scheme, _, token = (request.headers.get('authorization') or '').partition(' ')
    return token.strip() if scheme.lower() == 'bearer' and token.strip() else None


def current_user(request: Request) -> dict:
    """The signed-in account behind this request. Every route that touches chats or the agent starts here."""
    user = auth.user_for_token(bearer(request))
    if not user:
        raise Denied(401, 'Sign in to continue.')
    return user


def owner_of(request: Request) -> str:
    """Whose chats this request may see: the id of the signed-in account."""
    return current_user(request)['id']


_hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
_lock = threading.Lock()


def _hit(key: str, cap: int, window: int, message: str, record: bool = True) -> None:
    """Allow at most `cap` hits for this key in the last `window` seconds (record=False only checks)."""
    now = time.time()
    with _lock:
        recent = _hits[key]
        while recent and now - recent[0] > window:
            recent.popleft()
        if len(recent) >= cap:
            raise Denied(429, message)
        if record:
            recent.append(now)
        if len(_hits) > 5000:  # forget keys that have been quiet for an hour
            for stale in [k for k, q in _hits.items() if not q or now - q[-1] > HOUR]:
                del _hits[stale]


def _address(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def limit_chat(user_id: str) -> None:
    """At most CHAT_RATE_PER_HOUR messages an hour from one account (0 switches the limit off)."""
    if config.CHAT_RATE_PER_HOUR > 0:
        _hit('chat:' + user_id, config.CHAT_RATE_PER_HOUR, HOUR, 'That is a lot of messages for one hour. Please try again a little later.')


def limit_signup(request: Request) -> None:
    _hit('signup:' + _address(request), SIGNUP_CAP, HOUR, 'Too many new accounts from this address. Please try again later.')


def limit_login(request: Request, email: str) -> None:
    """Called before a sign-in attempt: stops guessing passwords. Wrong tries are counted by login_failed."""
    _hit('try:' + _address(request), TRY_CAP, FAIL_WINDOW, 'Too many sign-in attempts. Please wait a few minutes.')
    _hit(_fail_key(request, email), FAIL_CAP, FAIL_WINDOW,
         'Too many wrong passwords for this email. Please wait a few minutes and try again.', record=False)


def _fail_key(request: Request, email: str) -> str:
    return f'fail:{_address(request)}|{email.strip().lower()[:254]}'


def login_failed(request: Request, email: str) -> None:
    with _lock:
        _hits[_fail_key(request, email)].append(time.time())


def login_ok(request: Request, email: str) -> None:
    with _lock:
        _hits.pop(_fail_key(request, email), None)
