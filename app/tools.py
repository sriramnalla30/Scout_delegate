"""Tools the agents can call.

ask_user       human-in-the-loop: pauses the graph and waits for the person's answer
geocode        turns "Area, City" into coordinates (OpenStreetMap Nominatim, no key)
parser_agent   runs the parser sub-agent and returns its structured result
"""
import json
import time
import urllib.parse
import urllib.request

from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

_GEOCODE_CACHE: dict[str, dict] = {}
_LAST_GEOCODE = 0.0


@tool
def ask_user(question: str) -> str:
    """Ask the person ONE short question and wait for their answer.
    Use it to relay the question the parser agent asked. Returns the person's reply as text."""
    return str(interrupt({'question': question}))


@tool
def geocode(place: str) -> dict:
    """Look up the latitude and longitude of a place, written as "Area, City, Country".
    Returns {"lat", "lng", "display_name"} or {"error"} when nothing is found."""
    global _LAST_GEOCODE
    key = place.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    wait = 1.1 - (time.monotonic() - _LAST_GEOCODE)  # Nominatim allows one request per second
    if wait > 0:
        time.sleep(wait)
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({'q': place, 'format': 'jsonv2', 'limit': 1})
    request = urllib.request.Request(url, headers={'User-Agent': 'scout-place-finder-demo/0.1 (local development)'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            rows = json.loads(response.read().decode('utf-8'))
    except Exception as error:  # network or HTTP failure: report it to the agent, do not crash the run
        return {'error': f'geocoding request failed: {type(error).__name__}'}
    finally:
        _LAST_GEOCODE = time.monotonic()
    if not rows:
        return {'error': f'no location found for "{place}"'}
    result = {'lat': round(float(rows[0]['lat']), 5), 'lng': round(float(rows[0]['lon']), 5), 'display_name': rows[0]['display_name']}
    _GEOCODE_CACHE[key] = result
    return result


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return ''.join(b.get('text', '') for b in content if isinstance(b, dict))


@tool
def parser_agent(request: str) -> dict:
    """Hand a place request to the parser agent. It prepares the arguments for the Google Maps
    search tool and returns {"status": "ready" | "needs_input" | "failed", ...}. With needs_input
    it also returns the question to ask the person. Pass everything the person said so far."""
    from app.parser_agent import finalize, get_parser_agent  # imported here to avoid a circular import

    writer = get_stream_writer()  # lets the UI show what the sub-agent does inside this tool call
    seen, final = 0, {}
    for state in get_parser_agent().stream({'messages': [{'role': 'user', 'content': request}]}, stream_mode='values'):
        for message in state['messages'][seen:]:
            if message.type == 'ai' and message.tool_calls:
                for call in message.tool_calls:
                    if call['name'] != 'ParserResult':  # the structured-output pseudo-tool is not a real step
                        writer({'agent': 'parser', 'kind': 'tool_call', 'name': call['name'], 'args': call['args']})
            elif message.type == 'tool' and message.name != 'ParserResult':
                writer({'agent': 'parser', 'kind': 'tool_result', 'name': message.name, 'content': _text(message.content)[:300]})
        seen = len(state['messages'])
        final = state
    structured = final.get('structured_response')
    if structured is None:
        return {'status': 'failed', 'problem': 'the parser agent returned no structured result'}
    result = finalize(structured)
    writer({'agent': 'parser', 'kind': 'result', 'data': result})
    return result
