"""The Google Maps search tool: the gosom google-maps-scraper binary ("gmaps") in fast mode.

This is the only module that knows about the binary. Everything else asks for `run(...)` and gets plain
dicts back, so the scraper can later be swapped (a paid API, another scraper mode) without touching the rest.

Fast mode reads one list search ("gyms in Koramangala Bangalore") around a map pin and returns up to 20 places.
It needs the pin: a query whose pin is far from the named city comes back empty.
"""
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app import config

LANG = re.compile(r'[a-z]{2}(-[A-Za-z]{2,4})?')
RADIUS_MIN, RADIUS_MAX = 100, 50_000

_slots = threading.BoundedSemaphore(max(1, config.GMAPS_PARALLEL))


class SearchError(Exception):
    """A search that could not run. `kind` says why: invalid, unavailable, busy, timeout, failed or parse."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def clean_args(query, lat, lng, radius_m=None, lang='en') -> dict:
    """The arguments as the scraper takes them, checked. Raises SearchError('invalid') for anything unusable."""
    query = str(query or '').strip()
    if not query or len(query) > 200 or '\n' in query or '\r' in query:
        raise SearchError('invalid', 'The search text must be one line of 1 to 200 characters.')
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise SearchError('invalid', 'The search needs a latitude and a longitude.')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise SearchError('invalid', 'The latitude or longitude is outside the map.')
    if radius_m is not None:
        try:
            radius_m = min(RADIUS_MAX, max(RADIUS_MIN, int(radius_m)))
        except (TypeError, ValueError):
            radius_m = None
    lang = str(lang or 'en').strip()
    return {'query': query, 'lat': round(lat, 6), 'lng': round(lng, 6), 'radius_m': radius_m, 'lang': lang if LANG.fullmatch(lang) else 'en'}


def build_command(binary, query_file, out_file, args: dict) -> list[str]:
    command = [str(binary), '-input', str(query_file), '-results', str(out_file), '-json', '-fast-mode', '-c', '1', '-depth', '1',
               '-geo', f"{args['lat']},{args['lng']}", '-exit-on-inactivity', '15s']
    if args.get('radius_m'):
        command += ['-radius', str(args['radius_m'])]
    if args.get('lang') and args['lang'] != 'en':
        command += ['-lang', args['lang']]
    return command


def parse_output(text: str) -> list[dict]:
    """The scraper writes one JSON value per line. A pin far from the named city can give `null` or a list holding nulls."""
    places = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            data = json.loads(line)
            places.extend(p for p in (data if isinstance(data, list) else [data]) if isinstance(p, dict))
    return places


def _tail(log: str) -> str:
    """The last meaningful scraper log lines, without banners and progress bars."""
    lines = [l.strip() for l in log.splitlines() if l.strip() and l[0] not in '|╔║╚']
    errors = [l for l in lines if 'error' in l.lower() or 'required' in l.lower()]
    out = []
    for line in (errors or lines)[-3:]:
        try:
            obj = json.loads(line)
            out.append(str(obj.get('error') or obj.get('message') or line))
        except (json.JSONDecodeError, AttributeError):
            out.append(line)
    return ' | '.join(out)[:400]


def run(args: dict) -> dict:
    """Run one search. `args` comes from clean_args. Returns {"places": [raw dicts], "elapsed_ms": int}."""
    binary = Path(config.GMAPS_BIN)
    if not binary.is_file():
        raise SearchError('unavailable', 'The Google Maps search tool is not installed on this machine (set GMAPS_BIN to the gmaps binary).')
    if not _slots.acquire(timeout=config.GMAPS_QUEUE_WAIT):
        raise SearchError('busy', 'Other searches are still running. Try again in a minute.')
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='gmaps_') as work:
            work = Path(work)
            query_file, out_file = work / 'q.txt', work / 'out.json'
            query_file.write_text(args['query'] + '\n', encoding='utf-8')
            try:
                proc = subprocess.run(build_command(binary, query_file, out_file, args), cwd=work, capture_output=True, timeout=config.GMAPS_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise SearchError('timeout', f'Google Maps did not answer within {config.GMAPS_TIMEOUT} seconds.')
            log = (proc.stdout + proc.stderr).decode('utf-8', errors='replace')
            raw = out_file.read_text(encoding='utf-8-sig', errors='replace') if out_file.exists() else ''
    finally:
        _slots.release()
    try:
        places = parse_output(raw)
    except json.JSONDecodeError:
        raise SearchError('parse', 'The search tool wrote output that could not be read.')
    if proc.returncode != 0 and not places:
        raise SearchError('failed', 'The search tool stopped with an error: ' + (_tail(log) or f'exit code {proc.returncode}'))
    return {'places': places, 'elapsed_ms': round((time.monotonic() - started) * 1000)}
