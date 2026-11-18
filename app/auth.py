"""Accounts and sign-in: who a request belongs to.

People sign up or in with an email and a password, or with Google. Either way the server hands back a session token,
which the page sends as `Authorization: Bearer <token>` (a token in a header, not a cookie, so a page hosted on another
site such as Netlify works in every browser). Everything a person keeps (their chats, their agent conversations) is filed
under their account id, so no account can read another's history.

Accounts, sessions and thread ownership live in one SQLite file, <DATA_DIR>/users.sqlite. Keep DATA_DIR on a disk that
survives restarts, or accounts and chats are lost with it (see docs/DEPLOYMENT.md).

Passwords are stored as salted scrypt hashes, session tokens as SHA-256 hashes, so a copy of the database cannot be used to
sign in. Nothing here knows about agents or places.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from app import config, store

logger = logging.getLogger('scout.auth')

EMAIL = re.compile(r'^[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s.]{2,}$')
MIN_PASSWORD, MAX_PASSWORD = 8, 128
SCRYPT = (2 ** 14, 8, 1)  # cost, block size, parallelism: about 16 MB and a few tens of milliseconds per hash
DAY = 86400

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,                 -- random hex; also the name of the person's chat folder
    email TEXT NOT NULL UNIQUE,          -- lower case
    name TEXT NOT NULL,
    password_hash TEXT,                  -- empty for an account that only ever signed in with Google
    google_sub TEXT UNIQUE,              -- Google's stable id for the person
    email_verified INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,         -- sha256 of the token the browser holds
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS threads (     -- who owns each conversation with the agent
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL
);
'''


class AuthError(Exception):
    """Something the person can fix (or must be told): the server turns it into a JSON error with this status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


_ready: set[str] = set()
_setup = threading.Lock()


def _db() -> sqlite3.Connection:
    """A connection to the accounts database (one per call: cheap, and safe across the server's threads)."""
    path = config.DATA_DIR / 'users.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    with _setup:
        if str(path) not in _ready:
            conn.executescript(SCHEMA)
            _ready.add(str(path))
    return conn


# ---- passwords -------------------------------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    n, r, p = SCRYPT
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f'scrypt${n}${r}${p}${salt.hex()}${digest.hex()}'


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        if kind != 'scrypt':
            return False
        probe = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(probe, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


_dummy: list[str] = []


def _dummy_hash() -> str:
    """Checked when the email is unknown, so a wrong email and a wrong password take the same time."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    return _dummy[0]


# ---- validation ------------------------------------------------------------------------------------------------------
def normalize_email(raw) -> str:
    email = str(raw or '').strip().lower()
    if not EMAIL.match(email) or len(email) > 254:
        raise AuthError(400, 'Enter a valid email address.')
    return email


def _name_for(email: str, name) -> str:
    name = ' '.join(str(name or '').split())[:60]
    return name or email.split('@')[0][:60]


def _check_new_password(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(400, f'Choose a password of at least {MIN_PASSWORD} characters.')
    if len(password) > MAX_PASSWORD:
        raise AuthError(400, f'Use a password of at most {MAX_PASSWORD} characters.')
    return password


def public(user: dict) -> dict:
    """What the page may know about a person. Never the id, the hash or Google's id."""
    methods = (['password'] if user['password_hash'] else []) + (['google'] if user['google_sub'] else [])
    return {'email': user['email'], 'name': user['name'], 'methods': methods, 'created': user['created']}


# ---- accounts --------------------------------------------------------------------------------------------------------
def _row(db, where: str, value) -> dict | None:
    found = db.execute(f'SELECT * FROM users WHERE {where} = ?', (value,)).fetchone()
    return dict(found) if found else None


def get_user(user_id: str) -> dict | None:
    with closing(_db()) as db:
        return _row(db, 'id', user_id)


def find_by_email(email: str) -> dict | None:
    with closing(_db()) as db:
        return _row(db, 'email', email.strip().lower())


def _insert(db, email: str, name: str, password_hash: str | None, google_sub: str | None, verified: bool) -> dict:
    user = {'id': uuid.uuid4().hex, 'email': email, 'name': name, 'password_hash': password_hash, 'google_sub': google_sub,
            'email_verified': int(verified), 'created': time.time()}
    db.execute('INSERT INTO users (id, email, name, password_hash, google_sub, email_verified, created) '
               'VALUES (:id, :email, :name, :password_hash, :google_sub, :email_verified, :created)', user)
    return user


def signup(email, password, name=None) -> dict:
    email, password = normalize_email(email), _check_new_password(password)
    hashed = hash_password(password)
    try:
        with closing(_db()) as db, db:
            return _insert(db, email, _name_for(email, name), hashed, None, False)
    except sqlite3.IntegrityError:
        raise AuthError(409, 'An account with this email already exists. Sign in instead.') from None


def login(email, password) -> dict:
    wrong = AuthError(401, 'Incorrect email or password. If you signed up with Google, use "Continue with Google".')
    try:
        user = find_by_email(normalize_email(email))
    except AuthError:
        user = None
    stored = (user or {}).get('password_hash')
    matches = check_password(str(password or '')[:MAX_PASSWORD], stored or _dummy_hash())
    if not (user and stored and matches):
        raise wrong
    return user


def verify_google(credential: str) -> dict:
    """Google's ID token, checked against Google's public keys, this app's client id and the expiry time."""
    if not config.GOOGLE_CLIENT_ID:
        raise AuthError(503, 'Google sign-in is not set up on this server yet. Use your email and password.')
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), config.GOOGLE_CLIENT_ID)
    except ValueError:
        raise AuthError(401, 'Google could not confirm who you are. Please try again.') from None


def google_login(credential) -> dict:
    info = verify_google(str(credential or ''))
    sub, email = str(info.get('sub') or ''), str(info.get('email') or '').strip().lower()
    if not sub or not EMAIL.match(email) or not info.get('email_verified'):
        raise AuthError(401, 'Google has not verified this email address.')
    with closing(_db()) as db, db:
        user = _row(db, 'google_sub', sub)
        if user:
            return user
        user = _row(db, 'email', email)
        if not user:
            return _insert(db, email, _name_for(email, info.get('name')), None, sub, True)
        if user['google_sub']:
            raise AuthError(409, 'This email is already linked to a different Google account.')
        # Google has proved this person owns the email. A password set up earlier by someone who never proved it is
        # dropped, so a stranger who signed up with the address first cannot keep a way into the account.
        keep_password = user['password_hash'] if user['email_verified'] else None
        db.execute('UPDATE users SET google_sub = ?, email_verified = 1, password_hash = ? WHERE id = ?', (sub, keep_password, user['id']))
        return _row(db, 'id', user['id'])


# ---- sessions --------------------------------------------------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: str) -> str:
    token, now = secrets.token_urlsafe(32), time.time()
    with closing(_db()) as db, db:
        db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        db.execute('INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?, ?, ?, ?)',
                   (_digest(token), user_id, now, now + config.SESSION_DAYS * DAY))
    return token


def user_for_token(token: str | None) -> dict | None:
    if not token or len(token) > 200:
        return None
    with closing(_db()) as db:
        found = db.execute('SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires > ?',
                           (_digest(token), time.time())).fetchone()
    return dict(found) if found else None


def end_session(token: str | None) -> None:
    if token:
        with closing(_db()) as db, db:
            db.execute('DELETE FROM sessions WHERE token_hash = ?', (_digest(token),))


# ---- who owns which conversation -------------------------------------------------------------------------------------
def claim_thread(thread_id: str, user_id: str) -> bool:
    """True when this person owns the conversation: it was theirs already, or nobody had used the id yet."""
    with closing(_db()) as db, db:
        db.execute('INSERT OR IGNORE INTO threads (thread_id, user_id, created) VALUES (?, ?, ?)', (thread_id, user_id, time.time()))
        owner = db.execute('SELECT user_id FROM threads WHERE thread_id = ?', (thread_id,)).fetchone()
    return bool(owner) and owner['user_id'] == user_id


def owns_thread(thread_id: str, user_id: str) -> bool:
    with closing(_db()) as db:
        owner = db.execute('SELECT user_id FROM threads WHERE thread_id = ?', (thread_id,)).fetchone()
    return bool(owner) and owner['user_id'] == user_id


def release_thread(thread_id: str, user_id: str) -> None:
    with closing(_db()) as db, db:
        db.execute('DELETE FROM threads WHERE thread_id = ? AND user_id = ?', (thread_id, user_id))


# ---- startup: accounts for a laptop, and the history from before accounts existed -------------------------------------
def seed_users() -> None:
    """Create (or fix the password of) every account listed in SEED_USERS, so a laptop always has its sign-ins."""
    for pair in filter(None, (p.strip() for p in config.SEED_USERS.split(';'))):
        email, _, password = pair.partition(':')
        try:
            email = normalize_email(email)
        except AuthError:
            logger.warning('SEED_USERS: skipped %r (not an email address)', email)
            continue
        if not password:
            continue
        user = find_by_email(email)
        with closing(_db()) as db, db:
            if not user:
                _insert(db, email, _name_for(email, None), hash_password(password), None, True)
            elif not (user['password_hash'] and check_password(password, user['password_hash'])):
                db.execute('UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?', (hash_password(password), user['id']))


def adopt_legacy_chats() -> None:
    """Chats saved before accounts existed go to LEGACY_CHATS_OWNER: copied once, the old files stay where they are."""
    owner = find_by_email(config.LEGACY_CHATS_OWNER) if config.LEGACY_CHATS_OWNER else None
    if config.LEGACY_CHATS_OWNER and not owner:
        logger.warning('LEGACY_CHATS_OWNER %s has no account: add it to SEED_USERS', config.LEGACY_CHATS_OWNER)
    if owner:
        store.import_legacy(owner['id'])
        for chat_id in store.chat_ids(owner['id']):
            claim_thread(chat_id, owner['id'])


def startup() -> None:
    seed_users()
    adopt_legacy_chats()
