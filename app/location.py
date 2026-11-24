"""The person's device position, for one request, kept apart from every message and every model.

The page sends it only when the coordinator asked the browser for it (the `ask_browser` tool) and the person allowed it. It is never
put in a message, so the coordinator, the scout model, the saved conversation and the traces never see the numbers: the code uses
it as the search pin, the same way it uses the pin that `geocode` returned. The models are told one line about it, nothing more.

A chat is in one of three states:

    HELD         the position is held (the search around the person can run)
    UNAVAILABLE  the person was asked and it could not be read (blocked, timeout, typed an answer instead)
    NOT_ASKED    nothing known yet: the scout reports that the location is needed

    clean(value)                   a checked position {"lat", "lng", "accuracy_m"} (rounded to about 110 m), or None
    hold(thread, pin)              keep it while this request runs
    mark_unavailable(thread)       remember that it could not be read, so the scout asks for an area and a city in words
    state(thread) / current(thread)  what the running request may know / the position itself (only the code asks for it)
    release(thread)                drop the position (the request is over)
"""
import math
import threading
import time

HELD, UNAVAILABLE, NOT_ASKED = 'available', 'unavailable', 'none'

ROUND_DECIMALS = 3       # 3 decimals is about 110 m: enough to find the gyms around you, not your front door
HELD_SECONDS = 600       # safety net: a request that never ends does not keep a position for ever
UNAVAILABLE_SECONDS = 600  # a person who blocked or ignored the request is not asked again in this chat straight away
MAX_HELD = 2000
REASONS = {'denied': 'the person blocked location access', 'timeout': 'the device did not answer in time',
           'unavailable': 'the device could not find where the person is', 'unsupported': 'this browser cannot share a location',
           'typed': 'the person typed an answer instead'}

_held: dict[str, tuple[dict, float]] = {}
_unavailable: dict[str, float] = {}
_lock = threading.Lock()


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def clean(value) -> dict | None:
    """The position from what the page sent, or None when it is not a usable position. Never raises."""
    if not isinstance(value, dict):
        return None
    lat, lng = _number(value.get('lat')), _number(value.get('lng'))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if lat == 0 and lng == 0:  # "null island": what a broken device reports
        return None
    accuracy = _number(value.get('accuracy_m'))
    return {'lat': round(lat, ROUND_DECIMALS), 'lng': round(lng, ROUND_DECIMALS),
            'accuracy_m': round(accuracy) if accuracy is not None and accuracy >= 0 else None}


def reason(code) -> str:
    """The words for a reason code the page sent. Anything else counts as 'could not find the person'."""
    return REASONS.get(code if isinstance(code, str) else '', REASONS['unavailable'])


def _expire(now: float) -> None:
    for key in [k for k, (_, until) in _held.items() if until <= now]:
        del _held[key]
    for key in [k for k, until in _unavailable.items() if until <= now]:
        del _unavailable[key]


def hold(thread_id: str, pin: dict | None) -> None:
    with _lock:
        now = time.monotonic()
        _expire(now)
        _unavailable.pop(thread_id, None)
        if pin is None:
            _held.pop(thread_id, None)
            return
        if len(_held) >= MAX_HELD:
            _held.pop(next(iter(_held)))
        _held[thread_id] = (pin, now + HELD_SECONDS)


def mark_unavailable(thread_id: str) -> None:
    with _lock:
        now = time.monotonic()
        _expire(now)
        _held.pop(thread_id, None)
        if len(_unavailable) >= MAX_HELD:
            _unavailable.pop(next(iter(_unavailable)))
        _unavailable[thread_id] = now + UNAVAILABLE_SECONDS


def current(thread_id: str | None) -> dict | None:
    with _lock:
        _expire(time.monotonic())
        item = _held.get(thread_id or '')
        return dict(item[0]) if item else None


def state(thread_id: str | None) -> str:
    with _lock:
        _expire(time.monotonic())
        if (thread_id or '') in _held:
            return HELD
        return UNAVAILABLE if (thread_id or '') in _unavailable else NOT_ASKED


def release(thread_id: str) -> None:
    """The request is over: the position goes. (That the person could not be reached is only a flag, and expires by itself.)"""
    with _lock:
        _held.pop(thread_id, None)


def forget(thread_id: str) -> None:
    """The chat is deleted: nothing about it stays."""
    with _lock:
        _held.pop(thread_id, None)
        _unavailable.pop(thread_id, None)
