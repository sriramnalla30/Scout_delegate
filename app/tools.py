"""Tools the agents can call.

ask_user       human-in-the-loop: pauses the graph and waits for the person's answer
ask_browser    pauses the graph and asks the person's browser for their position; it comes back as one sentence, never the numbers
geocode        turns "Area, City" into a map pin (latitude and longitude) by searching Google Maps
search_places  runs the Google Maps search (the gosom scraper) around that pin and stores the full results
scout_agent    runs the scout sub-agent (geocode, then search_places) and returns its structured result
"""
import math

from langchain_core.tools import tool
from langgraph.config import get_config, get_stream_writer
from langgraph.types import interrupt

from app import location, maps_pin, searches

MAX_KM_FROM_CITY = 50  # the longest real hop in the test set (Whitefield) was 18 km


@tool
def ask_user(question: str) -> str:
    """Ask the person ONE short question and wait for their answer.
    Use it to relay the question the scout agent asked. Returns the person's reply as text."""
    return str(interrupt({'question': question}))


@tool
def ask_browser() -> str:
    """Ask the person's browser for their current position: the person taps Allow and the device answers.
    Use it only when the scout agent says it needs the person's location (missing "location"), and once per request.
    It is not ask_user: ask_user asks the person a question in words, this asks the device.
    The app keeps the position; you never get it. Returns one sentence: "Position received.",
    "Position not available: <reason>." or "The person typed instead: <what they typed>."
    Whatever it returns, call scout_agent again with the original request plus that sentence."""
    return str(interrupt({'kind': 'browser'}))


def _km(a: dict, b: dict) -> float:
    rad = math.pi / 180
    h = math.sin((b['lat'] - a['lat']) * rad / 2) ** 2 + math.cos(a['lat'] * rad) * math.cos(b['lat'] * rad) * math.sin((b['lng'] - a['lng']) * rad / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


@tool
def geocode(area: str, city: str, country: str = 'India') -> dict:
    """Look up the map pin (latitude and longitude) of an area inside a city.
    Returns {"lat", "lng", "display_name", "km_from_city_centre"}, or {"error"} when the
    area is not found or lands far from the city (probably a different place with the same name)."""
    same = area.strip().lower() == city.strip().lower()
    hit, centre = maps_pin.lookup(f'{area}, {city}, {country}', f'{city}, {country}')
    if 'error' in hit:
        return {'error': f'no map location found for "{area}" in {city} ({hit["error"]})'}
    if not hit['exact'] and not same:  # Maps showed only a list of results: it does not know this name
        return {'error': f'no map location found for "{area}" in {city} (the map lookup did not recognise the name)'}
    if 'error' in centre:
        return {'error': f'could not locate {city} to check the result ({centre["error"]})'}
    gap = _km(hit, centre)
    if gap > MAX_KM_FROM_CITY:  # right name, wrong place: refuse rather than send people to another town
        return {'error': f'"{area}" matched a place {gap:.0f} km from {city}, probably a different place with the same name'}
    return {'lat': hit['lat'], 'lng': hit['lng'], 'display_name': hit['name'], 'km_from_city_centre': round(gap, 1)}


@tool(response_format='content_and_artifact')
def search_places(query: str, lat: float, lng: float, radius_m: int | None = None, lang: str = 'en') -> tuple[dict, dict]:
    """Search for the places a list query finds around a map pin, for example "gyms in Koramangala Bangalore".
    lat and lng are the pin that geocode returned. radius_m is set only when the person gave a distance.
    Returns {"search_id", "count", "top"} or {"error"}. The full results are stored and shown to the person by the app;
    you only get the count and the best few."""
    result = searches.perform(query, lat, lng, radius_m, lang)
    if not result['ok']:
        return {'error': result['message']}, result
    return {k: result[k] for k in ('search_id', 'count', 'cached', 'highlights', 'top')}, result


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content if isinstance(b, dict))


def _thread_id() -> str | None:
    try:
        return (get_config().get('configurable') or {}).get('thread_id')
    except RuntimeError:  # called outside a graph run
        return None


@tool
def scout_agent(request: str) -> dict:
    """Hand a place request to the scout agent. It finds the map pin, searches for places and returns
    {"status": "ready" | "needs_input" | "failed", ...}. With ready it also returns what the search found
    (the app shows the full results). With needs_input it returns the question to ask the person.
    Pass everything the person said so far."""
    from app.scout_agent import complete, finalize, get_scout_agent, position_line  # imported here to avoid a circular import

    writer = get_stream_writer()  # lets the UI show what the sub-agent does inside this tool call
    # What the app knows about the person's position: the scout model is told one line about it, never the numbers.
    thread = _thread_id()
    position, device = location.state(thread), location.current(thread)
    line = position_line(position)
    seen, final = 0, {}
    for state in get_scout_agent().stream({'messages': [{'role': 'user', 'content': request + (f'\n\n{line}' if line else '')}]}, stream_mode='values'):
        for message in state['messages'][seen:]:
            if message.type == 'ai' and message.tool_calls:
                for call in message.tool_calls:
                    if call['name'] != 'Report':  # the structured-output pseudo-tool is not a real step
                        writer({'agent': 'scout', 'kind': 'tool_call', 'name': call['name'], 'args': call['args']})
            elif message.type == 'tool' and message.name != 'Report':
                writer({'agent': 'scout', 'kind': 'tool_result', 'name': message.name, 'content': _text(message.content)[:300]})
        seen = len(state['messages'])
        final = state
    structured = final.get('structured_response')
    if structured is None:
        return {'status': 'failed', 'problem': 'the scout agent returned no structured result'}
    # the report is checked against what the tools really did (the pin, the search) before anyone sees it
    result = complete(finalize(structured, position), final['messages'],
                      notify=lambda args: writer({'agent': 'scout', 'kind': 'tool_call', 'name': 'search_places', 'args': args}), device=device)
    writer({'agent': 'scout', 'kind': 'result', 'data': result})
    return result
