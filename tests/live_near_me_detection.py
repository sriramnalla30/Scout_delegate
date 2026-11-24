"""Run the near-me detection sentences on the REAL scout model (Groq), with the map tools faked. Not part of the offline unit tests.

Only the model call is real. geocode and search_places are faked so nothing scrapes. Tracing must be off, or the prompts would be
saved in Langfuse as a new production version.

    LANGFUSE_TRACING=false python tests/live_near_me_detection.py <dev|held|city|all> <out.json>
    ONLY_STATES=none,unavailable ...    runs only the cases for those position states (none = not asked yet)

dev    the sentences the prompt was written against      held   sentences kept apart, to be run once, never used to tune the prompt
city   "near me in <city>" with the position in the same or another city, and the coordinator's wording
raw_ok = what the scout model wrote, final_ok = after finalize() (the code checks that do not trust the model)
"""
import json
import os
import sys
import time
from unittest import mock

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from app import config, scout_agent  # noqa: E402
from app.location import HELD, NOT_ASKED, UNAVAILABLE  # noqa: E402
from app.scout_agent import finalize, get_scout_agent, position_line  # noqa: E402

assert not config.LANGFUSE_ENABLED, 'run with LANGFUSE_TRACING=false'

YES, NO = 'near me', 'named place'
DEV = [
    ('d1', 'gyms near me', YES), ('d2', 'Best pharmacies near me', YES), ('d3', 'closest dentist', YES),
    ('d4', 'any cafes around here?', YES), ('d5', 'coffee shops in my area', YES), ('d6', 'mere paas gym batao', YES),
    ('d7', 'naa daggara pharmacy ekkada undi', YES), ('d8', 'hospitals close to where I am', YES),
    ('d9', 'gyms in Koramangala Bangalore', NO), ('d10', 'gyms near Koramangala', NO), ('d11', 'cafes near Indiranagar metro', NO),
    ('d12', 'hotels near the airport', NO), ('d13', 'near my office in HSR Layout, a salon', NO),
    ('d14', 'not near me, I travel to Mumbai: dentists in Andheri', NO),
]
HELDOUT = [
    ('h1', 'gym nera me', YES), ('h2', 'restaraunts nearby', YES), ('h3', 'plumber near by', YES),
    ('h4', 'open pharmacy right now close to me', YES), ('h5', 'where can I eat around here', YES), ('h6', 'mere aas paas ka hospital', YES),
    ('h7', 'dentists in Benz Circle Vijayawada', NO), ('h8', 'salons near Jubilee Hills check post', NO), ('h9', 'best biryani in Charminar', NO),
]


def found(*a, **k):
    return {'ok': True, 'search_id': 's0123456789ab', 'count': 20, 'cached': False, 'elapsed_ms': 5, 'args': {},
            'highlights': {'avg_rating': 4.5, 'best': {'name': 'X', 'rating': 4.8, 'reviews': 10}}, 'top': []}


def fake_lookup(area_query, city_query):
    return {'lat': 12.93, 'lng': 77.62, 'name': area_query, 'exact': True}, {'lat': 12.97, 'lng': 77.59}


def run(text, state, city=None):
    line = position_line(state, city)
    content = text + (f'\n\n{line}' if line else '')
    final = {}
    for attempt in range(4):
        try:
            for st in get_scout_agent().stream({'messages': [{'role': 'user', 'content': content}]}, stream_mode='values'):
                final = st
            break
        except Exception as error:  # a rate limit or a flaky structured reply: wait and try again
            print('   retry', type(error).__name__, str(error)[:100], flush=True)
            time.sleep(20 * (attempt + 1))
    raw = final.get('structured_response')
    if raw is None:
        return None, None, content
    return raw, finalize(raw, state), content


def check(expect, raw, out):
    """(raw_ok, final_ok, why)"""
    r = raw.model_dump()
    if expect == 'device':
        return (bool(r['use_device_location']) and r['status'] == 'ready' and not r['area'],
                out['status'] == 'ready' and out['use_device_location'] and out['args']['lat'] is None, '')
    if expect == 'ask_location':
        return r['missing'] == ['location'] and r['status'] == 'needs_input' and not r['use_device_location'], out['missing'] == ['location'] and out['question'] is None, ''
    if expect == 'ask_area_city':
        ok = out['status'] == 'needs_input' and 'area' in out['missing'] and 'location' not in out['missing'] and not out['use_device_location']
        return 'location' not in r['missing'] and not r['use_device_location'] and r['status'] == 'needs_input', ok, ''
    if expect == 'not_device':
        ok = not r['use_device_location'] and 'location' not in r['missing']
        return ok, ok and not out['use_device_location'] and 'location' not in out['missing'], ''
    if expect == 'mismatch':  # a position in another city: ask, naming the city the person wrote
        ok = out['status'] == 'needs_input' and 'area' in out['missing'] and not out['use_device_location'] and bool(out['question'])
        return not r['use_device_location'] and ok, ok, ''
    raise ValueError(expect)


def cases(which):
    rows = []
    if which in ('dev', 'all'):
        for cid, text, kind in DEV:
            if kind == YES:
                rows += [(cid, text, NOT_ASKED, None, 'ask_location'), (cid, text, HELD, None, 'device'), (cid, text, UNAVAILABLE, None, 'ask_area_city')]
            else:
                rows += [(cid, text, NOT_ASKED, None, 'not_device'), (cid, text, HELD, None, 'not_device')]
    if which in ('held', 'all'):
        for cid, text, kind in HELDOUT:
            if kind == YES:
                rows += [(cid, text, NOT_ASKED, None, 'ask_location'), (cid, text, HELD, None, 'device')]
            else:
                rows += [(cid, text, NOT_ASKED, None, 'not_device'), (cid, text, HELD, None, 'not_device')]
    if which in ('city', 'all'):
        rows += [('c1', 'gyms near me in Hyderabad', HELD, 'Hyderabad', 'device'), ('c2', 'gyms near me in Hyderabad', HELD, 'Pune', 'mismatch'),
                 ('c3', 'gyms near me in Bangalore', HELD, 'Bengaluru', 'device'), ('c4', 'pharmacies near me in Chennai', HELD, 'Mumbai', 'mismatch'),
                 ('c5', 'gyms near me in Hyderabad', HELD, None, 'device'),
                 ('c6', 'gyms near me. Position received.', HELD, None, 'device'),
                 ('c7', 'gyms near me. Position not available: the person blocked location access.', UNAVAILABLE, None, 'ask_area_city'),
                 ('c8', 'gyms near me in Hyderabad', NOT_ASKED, None, 'ask_location'),
                 ('c9', 'gyms near me in Hyderabad', UNAVAILABLE, None, 'ask_area_city')]
    only = os.environ.get('ONLY_STATES')
    if only:
        rows = [r for r in rows if r[2] in only.split(',')]
    return rows


if __name__ == '__main__':
    which, out_path = sys.argv[1], sys.argv[2]
    results = []
    with mock.patch.object(scout_agent.searches, 'perform', found), mock.patch('app.tools.searches.perform', found), \
            mock.patch('app.tools.maps_pin.lookup', fake_lookup):
        for cid, text, state, city, expect in cases(which):
            started = time.time()
            raw, out, content = run(text, state, city)
            if raw is None:
                row = {'id': cid, 'text': text, 'state': state, 'city': city, 'expect': expect, 'error': 'no structured reply'}
            else:
                raw_ok, final_ok, why = check(expect, raw, out)
                row = {'id': cid, 'text': text, 'state': state, 'city': city, 'expect': expect, 'raw_ok': raw_ok, 'final_ok': final_ok,
                       'raw': {k: v for k, v in raw.model_dump().items() if k in ('status', 'business_type', 'area', 'city', 'missing', 'question', 'use_device_location')},
                       'final': {k: out[k] for k in ('status', 'missing', 'question', 'use_device_location')}}
            results.append(row)
            print(f"{cid:4} {state:11} {expect:13} raw={row.get('raw_ok')} final={row.get('final_ok')}  ({time.time() - started:.0f}s)  {text[:50]}", flush=True)
            json.dump(results, open(out_path, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    ok_raw = sum(1 for r in results if r.get('raw_ok'))
    ok_final = sum(1 for r in results if r.get('final_ok'))
    print(f'DONE {which}: raw {ok_raw}/{len(results)}, final {ok_final}/{len(results)}', flush=True)
