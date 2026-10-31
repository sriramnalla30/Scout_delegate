"""Live check against the real model (needs GROQ_API_KEY). Not part of the offline unit tests.

    python tests/live_scenarios.py
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.orchestrator import stream_events  # noqa: E402


def run(thread, text):
    events = list(stream_events(thread, text))
    calls = [e['name'] for e in events if e['type'] == 'tool_call']
    return events, calls


def questions(events):
    return [e['question'] for e in events if e['type'] == 'interrupt']


def parser_results(events):
    return [e['data'] for e in events if e['type'] == 'tool_result' and e['name'] == 'parser_agent']


def check(label, ok, detail=''):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label} {detail}")
    return ok


results = []
print('1. Complete request: parser says ready, no question')
events, calls = run(uuid.uuid4().hex, 'I want gyms in Koramangala Bangalore')
parsed = parser_results(events)
args = (parsed[0].get('args') or {}) if parsed else {}
results += [check('no ask_user', 'ask_user' not in calls, str(calls)),
            check('parser ready', bool(parsed) and parsed[0].get('status') == 'ready'),
            check('coordinates from geocode', args.get('lat') is not None and args.get('lng') is not None, str(args))]

print('2. Missing city: parser asks, Scout relays, parser finishes')
thread = uuid.uuid4().hex
events, calls = run(thread, 'find me cafes in Indiranagar')
parsed = parser_results(events)
results += [check('parser reported needs_input', bool(parsed) and parsed[0].get('status') == 'needs_input'),
            check('question relayed to the person', bool(questions(events)), questions(events)[:1])]
events, calls = run(thread, 'Bangalore')
parsed = parser_results(events)
query = ((parsed[0].get('args') or {}).get('query', '') if parsed else '')
results += [check('parser called again', 'parser_agent' in calls),
            check('query built from both messages', 'indiranagar' in query.lower() and 'cafe' in query.lower(), query)]

print('3. Missing area ("near me"): needs_input')
events, calls = run(uuid.uuid4().hex, 'best pharmacies near me in Hyderabad')
parsed = parser_results(events)
results += [check('asked a question', bool(questions(events)), questions(events)[:1]),
            check('parser said area missing', bool(parsed) and 'area' in parsed[0].get('missing', []))]

print('4. Nothing known: asks for everything')
events, calls = run(uuid.uuid4().hex, 'I need a place')
results += [check('asked a question', bool(questions(events)), questions(events)[:1])]

print('5. Off-topic: answers without tools')
events, calls = run(uuid.uuid4().hex, 'what is 2 plus 2?')
results += [check('no tools', not calls, str(calls)), check('replied', any(e['type'] == 'message' for e in events))]

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
