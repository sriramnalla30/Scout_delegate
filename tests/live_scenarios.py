"""Live check against the real model (needs GROQ_API_KEY). Not part of the offline unit tests.

    python tests/live_scenarios.py
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import location  # noqa: E402
from app.coordinator import stream_events  # noqa: E402

DEVICE = {'lat': 12.93524, 'lng': 77.62453, 'accuracy_m': 38.4}  # a position in Bangalore, as the browser would report it


def run(thread, text, **answer):
    """One turn. `answer` (position= / position_error=) is how the page answers an ask_browser pause."""
    events = list(stream_events(thread, text, **answer))
    calls = [e['name'] for e in events if e['type'] == 'tool_call']
    return events, calls


def asked_browser(events):
    return any(e['type'] == 'browser' for e in events)


def questions(events):
    return [e['question'] for e in events if e['type'] == 'interrupt']


def scout_results(events):
    return [e['data'] for e in events if e['type'] == 'tool_result' and e['name'] == 'scout_agent']


def check(label, ok, detail=''):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}")
    return ok


results = []
print('1. Complete request: scout agent says ready, no question')
events, calls = run(uuid.uuid4().hex, 'I want gyms in Koramangala Bangalore')
parsed = scout_results(events)
args = (parsed[0].get('args') or {}) if parsed else {}
results += [check('no ask_user', 'ask_user' not in calls, str(calls)),
            check('scout agent ready', bool(parsed) and parsed[0].get('status') == 'ready'),
            check('coordinates from geocode', args.get('lat') is not None and args.get('lng') is not None, str(args))]

print('2. Missing city: scout agent asks, the coordinator relays, scout agent finishes')
thread = uuid.uuid4().hex
events, calls = run(thread, 'find me cafes in Indiranagar')
parsed = scout_results(events)
results += [check('scout agent reported needs_input', bool(parsed) and parsed[0].get('status') == 'needs_input'),
            check('question relayed to the person', bool(questions(events)), questions(events)[:1])]
events, calls = run(thread, 'Bangalore')
parsed = scout_results(events)
query = ((parsed[0].get('args') or {}).get('query', '') if parsed else '')
results += [check('scout agent called again', 'scout_agent' in calls),
            check('query built from both messages', 'indiranagar' in query.lower() and 'cafe' in query.lower(), query)]

print('3. "Near me" with location blocked: the browser is asked once, then the area and city in words')
thread = uuid.uuid4().hex
events, calls = run(thread, 'best pharmacies near me')
parsed = scout_results(events)
results += [check('scout agent said it needs the location', bool(parsed) and parsed[0].get('missing') == ['location'], str(parsed[:1] and parsed[0].get('missing'))),
            check('the browser was asked, not the person', asked_browser(events) and not questions(events)),
            check('paused for the browser', events[-1] == {'type': 'done', 'waiting': True})]
events, calls = run(thread, '', position_error='denied')
parsed = scout_results(events)
results += [check('the coordinator called the scout agent again', 'scout_agent' in calls),
            check('now the person is asked for the area and city', bool(questions(events)) and 'area' in (parsed[-1].get('missing') or []), questions(events)[:1]),
            check('the browser was not asked again', not asked_browser(events))]

print('4. Nothing known: asks for everything')
events, calls = run(uuid.uuid4().hex, 'I need a place')
results += [check('asked a question', bool(questions(events)), questions(events)[:1])]

print('5. Off-topic: answers without tools')
events, calls = run(uuid.uuid4().hex, 'what is 2 plus 2?')
results += [check('no tools', not calls, str(calls)), check('replied', any(e['type'] == 'message' for e in events))]

print('6. "Near me" with location allowed: the search runs around the position, with no area, no city and no geocode')
thread = uuid.uuid4().hex
events, calls = run(thread, 'gyms near me')
results += [check('the browser was asked', asked_browser(events))]
events, calls = run(thread, '', position=location.clean(DEVICE))
parsed = scout_results(events)
final = parsed[-1] if parsed else {}
subs = [e.get('name') for e in events if e['type'] == 'sub' and e.get('kind') == 'tool_call']
results += [check('scout agent ready', final.get('status') == 'ready', str(final.get('status'))),
            check('a search around the person, not a named place', final.get('use_device_location') is True and not final.get('area') and not final.get('city')),
            check('geocode was never called', 'geocode' not in calls and 'geocode' not in subs, str(subs)),
            check('places were found', ((final.get('search') or {}).get('count') or 0) > 0, str((final.get('search') or {}).get('count'))),
            check('no number of the position in any event', not any(n in str(events) for n in ('12.93524', '77.62453', '12.935', '77.625')))]

print('7. "Near me", the person types the place instead of allowing: the typed place wins')
thread = uuid.uuid4().hex
events, calls = run(thread, 'pharmacies near me')
results += [check('the browser was asked', asked_browser(events))]
events, calls = run(thread, 'Adyar Chennai')
parsed = scout_results(events)
final = parsed[-1] if parsed else {}
results += [check('scout agent ready on the typed place', final.get('status') == 'ready' and not final.get('use_device_location'), str(final.get('status'))),
            check('area and city come from the typed text', 'adyar' in str(final.get('area')).lower() and 'chennai' in str(final.get('city')).lower())]

print('8. A named place never asks the browser')
events, calls = run(uuid.uuid4().hex, 'cafes near Indiranagar metro Bangalore')
results += [check('the browser was not asked', not asked_browser(events))]

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
