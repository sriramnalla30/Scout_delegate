"""Chat history on disk: one JSON file per chat in <DATA_DIR>/chats/<owner>/, where the owner is the account's id.

The file name is the chat's thread id and the owner is an account id (app/auth.py), so callers must pass values that
already matched server.THREAD_ID and came from a signed-in request. Before accounts existed the files sat directly in
<DATA_DIR>/chats/ ("legacy"): import_legacy() copies those into one account's folder.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

from app import config

MAX_BYTES = 2_000_000


def _dir(owner: str | None = None) -> Path:
    path = config.DATA_DIR / 'chats' / (owner or '')
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_chats(owner: str | None = None) -> list[dict]:
    """Summaries (no message log), newest first. Unreadable files are skipped."""
    out = []
    for file in _dir(owner).glob('*.json'):
        try:
            chat = json.loads(file.read_text('utf-8'))
            out.append({'id': file.stem, 'title': str(chat.get('title', 'Chat')), 'updated': float(chat.get('updated', 0))})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda c: c['updated'], reverse=True)


def load_chat(chat_id: str, owner: str | None = None) -> dict | None:
    try:
        chat = json.loads((_dir(owner) / f'{chat_id}.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return {'id': chat_id, 'title': str(chat.get('title', 'Chat')), 'updated': float(chat.get('updated', 0)), 'log': chat.get('log', [])}


def save_chat(chat_id: str, title: str, updated: float, log: list, owner: str | None = None) -> None:
    data = json.dumps({'title': title[:80], 'updated': updated, 'log': log}, ensure_ascii=False)
    if len(data.encode('utf-8')) > MAX_BYTES:
        raise ValueError('chat is too large to save')
    # write to a temp file first so a crash cannot leave a half-written chat behind
    fd, tmp = tempfile.mkstemp(dir=_dir(owner), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(data)
    os.replace(tmp, _dir(owner) / f'{chat_id}.json')


def delete_chat(chat_id: str, owner: str | None = None) -> bool:
    """True when the chat existed (and is gone)."""
    path = _dir(owner) / f'{chat_id}.json'
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def chat_ids(owner: str | None = None) -> list[str]:
    return [file.stem for file in _dir(owner).glob('*.json')]


def import_legacy(owner: str) -> list[str]:
    """Copy the chats saved before accounts existed into this account's history, once (a marker file remembers it).
    The old files are left where they are, so nothing is ever cleared. Returns the chat ids copied."""
    marker = _dir() / '.legacy-imported'
    if marker.exists():
        return []
    copied = []
    for file in sorted(_dir().glob('*.json')):
        target = _dir(owner) / file.name
        if not target.exists():
            shutil.copy2(file, target)
            copied.append(file.stem)
    marker.write_text(owner, encoding='utf-8')
    return copied
