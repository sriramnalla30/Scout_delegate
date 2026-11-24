"""Searches as stored results: one JSON file per search in <DATA_DIR>/searches/, named by its search id.

The full results never pass through a language model. The search tool stores them here and hands back only the
search id plus a few facts. Screens, exports and later steps (a map, charts, calling) read the stored document by id.

`perform()` is the one entry point: it checks the stored results of an identical recent search, otherwise runs the
scraper, stores what came back and returns a short outcome.
"""
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from app import config, gmaps, places

SEARCH_ID = re.compile(r'^s[0-9a-f]{12}$')
MAX_BYTES = 8_000_000


def _dir(*sub) -> Path:
    path = config.DATA_DIR.joinpath('searches', *sub)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')  # a crash cannot leave a half-written file behind
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(text)
    os.replace(tmp, path)


def cache_key(args: dict) -> str:
    """The same search asked again: same text, same pin (to about 10 m), radius and language."""
    parts = [args['query'].lower(), f"{args['lat']:.4f}", f"{args['lng']:.4f}", str(args.get('radius_m')), args.get('lang', 'en')]
    if args.get('pin'):  # a search around the person's own position is stored apart, so its page never prints coordinates
        parts.append(args['pin'])
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:20]


def load(search_id: str) -> dict | None:
    if not SEARCH_ID.match(search_id or ''):
        return None
    try:
        return json.loads((_dir() / f'{search_id}.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None


def save(doc: dict, key: str) -> None:
    """Store a search document under its own id and point the next identical search at it."""
    text = json.dumps(doc, ensure_ascii=False)
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('search results are too large to store')
    _write(_dir() / f"{doc['id']}.json", text)
    _write(_dir('by_key') / key, doc['id'])


def find_recent(key: str) -> dict | None:
    if config.SEARCH_CACHE_MINUTES <= 0:
        return None
    try:
        search_id = (_dir('by_key') / key).read_text('utf-8').strip()
    except OSError:
        return None
    doc = load(search_id)
    return doc if doc and time.time() - doc.get('created_at', 0) <= config.SEARCH_CACHE_MINUTES * 60 else None


def outcome(doc: dict, cached: bool) -> dict:
    """What the search tool reports: small enough to put in front of a model."""
    top = sorted((p for p in doc['places'] if p['rating']), key=lambda p: (p['score'], p['reviews']), reverse=True)[:3]
    return {'ok': True, 'search_id': doc['id'], 'count': len(doc['places']), 'cached': cached, 'elapsed_ms': doc['elapsed_ms'],
            'args': doc['args'], 'highlights': places.highlights(doc),
            'top': [{'name': p['name'], 'rating': p['rating'], 'reviews': p['reviews']} for p in top]}


def perform(query, lat, lng, radius_m=None, lang='en', pin=None) -> dict:
    """Run one search, or reuse an identical recent one. Never raises: a failure comes back as
    {"ok": False, "kind", "message", "args"}. A search that finds nothing is ok with count 0 and no search id."""
    args = None
    try:
        args = gmaps.clean_args(query, lat, lng, radius_m, lang, pin)
        key = cache_key(args)
        doc = find_recent(key)
        if doc:
            return outcome(doc, cached=True)
        found = gmaps.run(args)
        if not found['places']:
            return {'ok': True, 'search_id': None, 'count': 0, 'cached': False, 'elapsed_ms': found['elapsed_ms'], 'args': args, 'highlights': {}, 'top': []}
        doc = {'id': 's' + uuid.uuid4().hex[:12], 'created_at': time.time(), **places.build(found['places'], args, found['elapsed_ms'])}
        save(doc, key)
        return outcome(doc, cached=False)
    except gmaps.SearchError as error:
        return {'ok': False, 'kind': error.kind, 'message': str(error), 'args': args}
    except (OSError, ValueError) as error:  # the results could not be stored
        return {'ok': False, 'kind': 'storage', 'args': args, 'message': f'The search ran but its results could not be saved ({type(error).__name__}).'}
