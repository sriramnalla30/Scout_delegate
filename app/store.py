"""Chat history on disk: one JSON file per chat in <DATA_DIR>/chats/.

The file name is the chat's thread id, so callers must pass ids that already matched server.THREAD_ID.
"""
import json
import os
import tempfile
from pathlib import Path

from app import config

MAX_BYTES = 2_000_000


def _dir() -> Path:
    path = config.DATA_DIR / 'chats'
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_chats() -> list[dict]:
    """Summaries (no message log), newest first. Unreadable files are skipped."""
    out = []
    for file in _dir().glob('*.json'):
        try:
            chat = json.loads(file.read_text('utf-8'))
            out.append({'id': file.stem, 'title': str(chat.get('title', 'Chat')), 'updated': float(chat.get('updated', 0))})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda c: c['updated'], reverse=True)


def load_chat(chat_id: str) -> dict | None:
    try:
        chat = json.loads((_dir() / f'{chat_id}.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None
    return {'id': chat_id, 'title': str(chat.get('title', 'Chat')), 'updated': float(chat.get('updated', 0)), 'log': chat.get('log', [])}


def save_chat(chat_id: str, title: str, updated: float, log: list) -> None:
    data = json.dumps({'title': title[:80], 'updated': updated, 'log': log}, ensure_ascii=False)
    if len(data.encode('utf-8')) > MAX_BYTES:
        raise ValueError('chat is too large to save')
    # write to a temp file first so a crash cannot leave a half-written chat behind
    fd, tmp = tempfile.mkstemp(dir=_dir(), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(data)
    os.replace(tmp, _dir() / f'{chat_id}.json')


def delete_chat(chat_id: str) -> None:
    (_dir() / f'{chat_id}.json').unlink(missing_ok=True)
