"""Finds the map pin of "Area, City" by searching Google Maps in a headless browser.

One Chromium process stays up for the whole server; every lookup gets its own throw-away
context (no shared cookies or storage, nothing cached between lookups). A lookup opens the area
and the city side by side and returns both pins, so the caller can check they are near each other.
"""
import asyncio
import atexit
import os
import re
import threading
import urllib.parse
from concurrent.futures import TimeoutError as FutureTimeout

from playwright.async_api import async_playwright

MAX_PARALLEL = int(os.getenv('SCOUT_MAPS_PARALLEL', '4'))  # lookups running at the same time
PAGE_TIMEOUT_S = 25
TOTAL_TIMEOUT_S = 45
EXACT_WAIT_S = 4  # after the map centre appears, how long to wait for the exact place pin

_PLACE_PIN = re.compile(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)')  # the exact spot of a place page
_VIEW_PIN = re.compile(r'@(-?\d+\.\d+),(-?\d+\.\d+)')       # the map centre, used when the search shows a list
_ANY_PIN = re.compile(r'.*(@-?\d+\.\d+,-?\d+\.\d+|!3d-?\d+\.\d+!4d-?\d+\.\d+).*')
_EXACT_PIN = re.compile(r'.*!3d-?\d+\.\d+!4d-?\d+\.\d+.*')

_state = {'loop': None, 'thread': None, 'browser': None, 'pw': None, 'gate': None}
_start_lock = threading.Lock()


def pin_from_url(url: str) -> tuple[float, float] | None:
    match = _PLACE_PIN.search(url) or _VIEW_PIN.search(url)
    return (round(float(match.group(1)), 5), round(float(match.group(2)), 5)) if match else None


def _loop() -> asyncio.AbstractEventLoop:
    with _start_lock:
        if _state['loop'] is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name='maps-browser', daemon=True)
            thread.start()
            _state.update(loop=loop, thread=thread)
            _state['gate'] = asyncio.run_coroutine_threadsafe(_make_gate(), loop).result()
        return _state['loop']


async def _make_gate() -> asyncio.Semaphore:
    return asyncio.Semaphore(MAX_PARALLEL)


async def _browser():
    browser = _state['browser']
    if browser is None or not browser.is_connected():  # first use, or Chromium died: start a fresh one
        if _state['pw'] is None:
            _state['pw'] = await async_playwright().start()
        browser = _state['browser'] = await _state['pw'].chromium.launch(headless=True, args=['--disable-dev-shm-usage', '--no-sandbox'])
    return browser


async def _pin_of(context, query: str) -> dict:
    page = await context.new_page()
    try:
        await page.goto('https://www.google.com/maps/search/' + urllib.parse.quote(query), timeout=PAGE_TIMEOUT_S * 1000, wait_until='domcontentloaded')
        if 'consent.' in page.url:  # cookie wall (shown in some regions): accept and carry on
            for label in ('Accept all', 'Reject all'):
                button = page.get_by_role('button', name=label)
                if await button.count():
                    await button.first.click()
                    break
        await page.wait_for_url(_ANY_PIN, timeout=PAGE_TIMEOUT_S * 1000)
        try:  # a real place gets its exact pin a moment later; a name Maps does not know only ever gets a results list
            await page.wait_for_url(_EXACT_PIN, timeout=EXACT_WAIT_S * 1000)
            exact = True
        except Exception:
            exact = False
        pin = pin_from_url(page.url)
        if pin is None:
            return {'error': 'no pin in the page address'}
        title = (await page.title() or '').removesuffix(' - Google Maps').strip()
        return {'lat': pin[0], 'lng': pin[1], 'name': title or query, 'exact': exact}
    except Exception as error:
        return {'error': f'{type(error).__name__}: {str(error).splitlines()[0][:120]}'}
    finally:
        await page.close()


async def _lookup(area_query: str, city_query: str) -> tuple[dict, dict]:
    async with _state['gate']:
        context = await (await _browser()).new_context(locale='en-IN', viewport={'width': 1100, 'height': 700})
        try:
            await context.route('**/*', lambda route: route.abort() if route.request.resource_type in ('image', 'media', 'font') else route.continue_())
            return tuple(await asyncio.gather(_pin_of(context, area_query), _pin_of(context, city_query)))
        finally:
            await context.close()


def lookup(area_query: str, city_query: str) -> tuple[dict, dict]:
    """(area pin, city pin); each is {"lat", "lng", "name", "exact"} or {"error"}. Safe to call from many threads."""
    future = asyncio.run_coroutine_threadsafe(_lookup(area_query, city_query), _loop())
    try:
        return future.result(timeout=TOTAL_TIMEOUT_S)
    except FutureTimeout:
        future.cancel()
        error = {'error': f'maps lookup timed out after {TOTAL_TIMEOUT_S}s'}
        return error, error
    except Exception as error:
        failed = {'error': f'maps lookup failed ({type(error).__name__}: {str(error).splitlines()[0][:120]})'}
        return failed, failed


def close() -> None:
    loop = _state['loop']
    if loop is None:
        return

    async def shut():
        if _state['browser']:
            await _state['browser'].close()
        if _state['pw']:
            await _state['pw'].stop()

    try:
        asyncio.run_coroutine_threadsafe(shut(), loop).result(timeout=10)
    except Exception:
        pass


atexit.register(close)
