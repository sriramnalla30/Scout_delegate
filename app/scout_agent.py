import json
from functools import lru_cache
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from app import gmaps, searches
from app.llm import get_llm, recover_structured_reply, retry_middleware
from app.location import HELD, NOT_ASKED, UNAVAILABLE
from app.prompts import load_prompt
from app.tools import geocode, search_places

REQUIRED = ('business_type', 'area', 'city')


class GmapsArgs(BaseModel):
    """The arguments of the place search tool."""
    query: str = Field(description='List-search text: "<business type> in <Area> <City>". For a near-me request: the business type alone.')
    lat: float | None = Field(default=None, description='Latitude returned by geocode, or null. Always null for a near-me request: the app adds the position.')
    lng: float | None = Field(default=None, description='Longitude returned by geocode, or null. Always null for a near-me request: the app adds the position.')
    radius_m: int | None = Field(default=None, description='Search radius in metres, only if the user gave a distance.')
    lang: str = Field(default='en', description='Two-letter language code.')


class Report(BaseModel):
    """What the scout agent tells the coordinator: ready, or what it still needs from the person."""
    status: Literal['ready', 'needs_input', 'failed']
    business_type: str | None = Field(default=None, description='Business type found in the text, plural lowercase, or null.')
    area: str | None = Field(default=None, description='Neighbourhood or locality found in the text, or null.')
    city: str | None = Field(default=None, description='City found in the text, or null.')
    missing: list[Literal['business_type', 'area', 'city', 'location']] = Field(
        default_factory=list,
        description='Details still needed from the person. "location" means a near-me request whose device position has not been asked for yet (no DEVICE LOCATION line in the input).')
    question: str | None = Field(default=None, description='One short question for the person when status is needs_input. Null when missing is only ["location"].')
    args: GmapsArgs | None = Field(default=None, description='Search tool arguments, only when status is ready.')
    problem: str | None = Field(default=None, description='What went wrong when status is failed.')
    use_device_location: bool = Field(default=False, description='True only for a near-me request when the input ends with the line DEVICE LOCATION: available. Then status is ready and the app searches around the position. False otherwise.')


# What the app knows about the device position while the scout runs (the scout is told one line, never the numbers).
# The states live in app/location.py, next to the position itself.
POSITION_LINES = {HELD: 'DEVICE LOCATION: available', UNAVAILABLE: 'DEVICE LOCATION: not available', NOT_ASKED: ''}


def position_line(position: str, city: str | None = None) -> str:
    """The last line of the scout's input for this position state (empty when the position has not been asked for).
    `city` is the city the position is in, when the app knows it; the scout compares it with a city the person named."""
    line = POSITION_LINES.get(position, '')
    return f'{line} (city: {city})' if line and position == HELD and city else line


def _default_question(missing: list[str]) -> str:
    names = {'business_type': 'what kind of place', 'area': 'which area', 'city': 'which city', 'location': 'where you are'}
    return 'Could you tell me ' + ' and '.join(names[m] for m in missing) + '?'


def _near_me(out: Report, position: str, radius: int | None, lang: str) -> dict | None:
    """A near-me request (the model said so by use_device_location true or missing ["location"], and the person named no area).
    The answer depends on what the app really holds, not on what the model believed: the position is a fact only the code knows.
    Returns the finished report, or None when the request goes the normal way (a named place, or a position that could not be read)."""
    if out.area or not (out.use_device_location or 'location' in out.missing):
        return None
    out.args, out.problem = None, None
    # A model that only reports the missing location may leave the kind of place out of its report (the same slip the
    # normal route allows for): trust it then, the position is asked first and the kind of place is checked once it is held.
    trusted = position == NOT_ASKED and out.missing == ['location']
    if not out.business_type and not trusted:  # the kind of place is still needed first
        out.use_device_location, out.status, out.missing = False, 'needs_input', ['business_type']
        out.question = out.question or _default_question(out.missing)
    elif position == HELD:  # the type alone: "gyms" finds the places around the pin. complete() puts the position in.
        out.use_device_location, out.status, out.missing, out.question = True, 'ready', [], None
        out.args = GmapsArgs(query=out.business_type, lat=None, lng=None, radius_m=radius, lang=lang)
    elif position == NOT_ASKED:  # the coordinator asks the browser; the position cannot be guessed
        out.use_device_location, out.status, out.missing, out.question = False, 'needs_input', ['location'], None
    else:  # asked and not readable: ask in words, like any request with no place named. The code words that question,
        out.question = None  # so a model that asks the person to share their location again cannot send them round in a circle
        return None
    return out.model_dump()


def finalize(result: Report, position: str = NOT_ASKED) -> dict:
    """Enforce the rules in code so the status can be trusted whatever the model wrote.
    `position` is HELD, UNAVAILABLE or NOT_ASKED: what the app knows about the device position for this request."""
    out = result.model_copy(deep=True)
    radius = out.args.radius_m if out.args else None
    lang = out.args.lang if out.args else 'en'
    near = _near_me(out, position, radius, lang)
    if near is not None:
        return near
    out.use_device_location = False  # a named place, or no position: "near me" alone is never an area
    empty = [name for name in REQUIRED if not getattr(out, name)]
    # The model may leave a stated detail out of the report but still say correctly what is missing.
    # Keep its list when every item on it really is empty; otherwise fall back to the empty fields.
    claimed = [name for name in out.missing if name in empty] if out.status == 'needs_input' else []
    out.missing = claimed or empty
    if out.missing:
        out.status, out.args, out.problem = 'needs_input', None, None
        out.question = out.question or _default_question(out.missing)
    elif out.status == 'needs_input' and out.question:  # e.g. the map could not find the spelling
        out.args, out.missing = None, out.missing or ['area']
    elif out.args is not None and out.args.lat is not None and out.args.lng is not None:
        out.status, out.question, out.problem, out.missing = 'ready', None, None, []
    else:
        out.status, out.args, out.question = 'failed', None, None
        out.problem = out.problem or 'the map lookup returned no coordinates'
    return out.model_dump()


def _results(messages, name: str) -> list[tuple]:
    """(content, artifact) of every message the named tool returned, oldest first."""
    out = []
    for m in messages:
        if getattr(m, 'type', None) == 'tool' and m.name == name:
            try:
                content = json.loads(m.content) if isinstance(m.content, str) else m.content
            except ValueError:
                content = m.content
            out.append((content, getattr(m, 'artifact', None)))
    return out


def _same_search(a: dict, b: dict) -> bool:
    return (a['query'].lower() == b['query'].lower() and abs(a['lat'] - b['lat']) < 1e-4 and abs(a['lng'] - b['lng']) < 1e-4
            and a.get('radius_m') == b.get('radius_m') and a.get('lang', 'en') == b.get('lang', 'en'))


def _hidden(args: dict) -> dict:
    """The arguments as the coordinator and the page get them: for a search around the person's own position, no coordinates."""
    return {**args, 'lat': None, 'lng': None} if args.get('pin') == 'device' else args


def complete(report: dict, messages, notify=None, device: dict | None = None) -> dict:
    """The last check on a ready report, in code. The model's words are not trusted for the two things the search depends on:

    - the pin is the one geocode returned (or the position the person shared), whatever coordinates the model wrote down
    - a search was really run for exactly these arguments (when the model skipped it or searched something else, it runs here)

    The status then follows from the outcome: results -> ready, with `search` (id, count, highlights, top);
    nothing found -> needs_input, asking for a wider area; the search tool broke -> failed.
    A search around the shared position runs here, and the position stays out of everything the models and the page get back."""
    if report['status'] != 'ready':
        return report
    out = dict(report)
    args = dict(out['args'])
    on_device = bool(out.get('use_device_location') and device)
    if on_device:
        args.update(query=out.get('business_type') or args['query'], lat=device['lat'], lng=device['lng'], pin='device')
    else:
        pins = [c for c, _ in _results(messages, 'geocode') if isinstance(c, dict) and 'lat' in c]
        if pins:
            args['lat'], args['lng'] = pins[-1]['lat'], pins[-1]['lng']
    try:
        args = gmaps.clean_args(args['query'], args['lat'], args['lng'], args.get('radius_m'), args.get('lang'), args.get('pin'))
    except gmaps.SearchError as error:
        return {**out, 'status': 'failed', 'problem': str(error), 'search': {'error': error.kind}}
    # a search already run for these exact arguments counts, even a failed one: a failure is not retried here
    ran = next((a for _, a in reversed(_results(messages, 'search_places')) if isinstance(a, dict) and a.get('args') and _same_search(a['args'], args)), None)
    if ran is None:
        if notify:
            notify(_hidden(args))
        ran = searches.perform(**args)
    out['args'] = _hidden(args)
    if not ran['ok']:
        return {**out, 'status': 'failed', 'problem': ran['message'], 'search': {'error': ran['kind']}}
    out['search'] = {'id': ran['search_id'], 'count': ran['count'], 'cached': ran['cached'], 'elapsed_ms': ran['elapsed_ms'],
                     'highlights': ran['highlights'], 'top': ran['top']}
    if ran['count'] == 0:  # not a failure: the person can widen the search
        kind = out.get('business_type') or 'places'
        if on_device:  # no area was named, so ask for one, the way the app does without a location
            return {**out, 'status': 'needs_input', 'args': None, 'use_device_location': False, 'missing': ['area', 'city'],
                    'question': f'I could not find any {kind} around your location. Could you tell me an area and city instead?'}
        where = ', '.join(x for x in (out.get('area'), out.get('city')) if x)
        return {**out, 'status': 'needs_input', 'args': None, 'missing': ['area'],
                'question': f'I could not find any {kind} around {where}. Could you name a bigger or nearby area?'}
    return out


@lru_cache
def get_scout_agent():
    # ToolStrategy returns the structured result as a tool call. Groq rejects its JSON mode
    # (the automatic choice) when it is combined with other tools such as geocode and search_places.
    return create_agent(get_llm('Scout agent: model call'), tools=[geocode, search_places], system_prompt=load_prompt('scout'),
                        response_format=ToolStrategy(Report), middleware=[retry_middleware(), recover_structured_reply()], name='Scout agent')  # the second sits inside the first
