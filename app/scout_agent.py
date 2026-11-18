import json
from functools import lru_cache
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from app import gmaps, searches
from app.llm import get_llm, retry_middleware
from app.prompts import load_prompt
from app.tools import geocode, search_places

REQUIRED = ('business_type', 'area', 'city')


class GmapsArgs(BaseModel):
    """The arguments of the Google Maps search tool."""
    query: str = Field(description='List-search text: "<business type> in <Area> <City>".')
    lat: float | None = Field(default=None, description='Latitude returned by geocode, or null.')
    lng: float | None = Field(default=None, description='Longitude returned by geocode, or null.')
    radius_m: int | None = Field(default=None, description='Search radius in metres, only if the user gave a distance.')
    lang: str = Field(default='en', description='Two-letter language code.')


class Report(BaseModel):
    """What the scout agent tells the coordinator: ready, or what it still needs from the person."""
    status: Literal['ready', 'needs_input', 'failed']
    business_type: str | None = Field(default=None, description='Business type found in the text, plural lowercase, or null.')
    area: str | None = Field(default=None, description='Neighbourhood or locality found in the text, or null.')
    city: str | None = Field(default=None, description='City found in the text, or null.')
    missing: list[Literal['business_type', 'area', 'city']] = Field(default_factory=list, description='Details still needed from the person.')
    question: str | None = Field(default=None, description='One short question for the person when status is needs_input.')
    args: GmapsArgs | None = Field(default=None, description='Search tool arguments, only when status is ready.')
    problem: str | None = Field(default=None, description='What went wrong when status is failed.')


def _default_question(missing: list[str]) -> str:
    names = {'business_type': 'what kind of place', 'area': 'which area', 'city': 'which city'}
    return 'Could you tell me ' + ' and '.join(names[m] for m in missing) + '?'


def finalize(result: Report) -> dict:
    """Enforce the rules in code so the status can be trusted whatever the model wrote."""
    out = result.model_copy(deep=True)
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


def complete(report: dict, messages, notify=None) -> dict:
    """The last check on a ready report, in code. The model's words are not trusted for the two things the search depends on:

    - the pin is the one geocode returned, whatever coordinates the model wrote down
    - a search was really run for exactly these arguments (when the model skipped it or searched something else, it runs here)

    The status then follows from the outcome: results -> ready, with `search` (id, count, highlights, top);
    nothing found -> needs_input, asking for a wider area; the search tool broke -> failed."""
    if report['status'] != 'ready':
        return report
    out = dict(report)
    args = dict(out['args'])
    pins = [c for c, _ in _results(messages, 'geocode') if isinstance(c, dict) and 'lat' in c]
    if pins:
        args['lat'], args['lng'] = pins[-1]['lat'], pins[-1]['lng']
    try:
        args = gmaps.clean_args(args['query'], args['lat'], args['lng'], args.get('radius_m'), args.get('lang'))
    except gmaps.SearchError as error:
        return {**out, 'status': 'failed', 'problem': str(error), 'search': {'error': error.kind}}
    # a search already run for these exact arguments counts, even a failed one: a failure is not retried here
    ran = next((a for _, a in reversed(_results(messages, 'search_places')) if isinstance(a, dict) and a.get('args') and _same_search(a['args'], args)), None)
    if ran is None:
        if notify:
            notify(args)
        ran = searches.perform(**args)
    out['args'] = args
    if not ran['ok']:
        return {**out, 'status': 'failed', 'problem': ran['message'], 'search': {'error': ran['kind']}}
    out['search'] = {'id': ran['search_id'], 'count': ran['count'], 'cached': ran['cached'], 'elapsed_ms': ran['elapsed_ms'],
                     'highlights': ran['highlights'], 'top': ran['top']}
    if ran['count'] == 0:  # not a failure: the person can widen the search
        where = ', '.join(x for x in (out.get('area'), out.get('city')) if x)
        return {**out, 'status': 'needs_input', 'args': None, 'missing': ['area'],
                'question': f'I could not find any {out.get("business_type") or "places"} around {where}. Could you name a bigger or nearby area?'}
    return out


@lru_cache
def get_scout_agent():
    # ToolStrategy returns the structured result as a tool call. Groq rejects its JSON mode
    # (the automatic choice) when it is combined with other tools such as geocode and search_places.
    return create_agent(get_llm('Scout agent: model call'), tools=[geocode, search_places], system_prompt=load_prompt('scout'),
                        response_format=ToolStrategy(Report), middleware=[retry_middleware()], name='Scout agent')
