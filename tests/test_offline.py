"""Offline tests: no model calls and no network.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import access, config, maps_pin, observability, server, tools  # noqa: E402
from app.scout_agent import GmapsArgs, Report, finalize  # noqa: E402
from app.prompts import load_prompt  # noqa: E402


def pin(lat, lng, name='somewhere', exact=True):
    return {'lat': lat, 'lng': lng, 'name': name, 'exact': exact}


BANGALORE = pin(12.9716, 77.5946, 'Bengaluru')


def maps(area, city=BANGALORE):
    """Fake the Google Maps lookup: it returns (area pin, city pin) without opening a browser."""
    return mock.patch.object(tools.maps_pin, 'lookup', lambda area_query, city_query: (area, city))


class Geocode(unittest.TestCase):
    def geocode(self, area='Koramangala', city='Bangalore'):
        return tools.geocode.invoke({'area': area, 'city': city})

    def test_found_and_checked_against_the_city(self):
        with maps(pin(12.9352, 77.6245, 'Koramangala')):
            result = self.geocode()
        self.assertEqual((result['lat'], result['lng'], result['display_name']), (12.9352, 77.6245, 'Koramangala'))
        self.assertLess(result['km_from_city_centre'], 10)
        self.assertEqual(set(result), {'lat', 'lng', 'display_name', 'km_from_city_centre'})

    def test_a_name_maps_does_not_recognise_is_reported_not_raised(self):
        with maps(pin(12.97, 77.59, 'Nowhereville, Bangalore', exact=False)):  # Maps only showed a results list
            self.assertIn('error', self.geocode('Nowhereville'))

    def test_a_failed_lookup_is_reported_with_its_reason(self):
        with maps({'error': 'TimeoutError: page took too long'}):
            error = self.geocode()['error']
        self.assertIn('Koramangala', error)
        self.assertIn('TimeoutError', error)

    def test_a_failed_city_lookup_is_reported(self):
        with maps(pin(12.9352, 77.6245), {'error': 'TimeoutError'}):
            self.assertIn('Bangalore', self.geocode()['error'])

    def test_same_name_in_another_city_is_refused(self):
        with maps(pin(17.44, 78.35, 'Gachibowli')):  # Gachibowli is in Hyderabad, 500 km away
            error = self.geocode('Gachibowli', 'Bangalore')['error']
        self.assertIn('km from Bangalore', error)

    def test_asking_for_the_city_itself_is_fine(self):
        with maps(pin(12.9716, 77.5946, 'Bengaluru', exact=False)):
            self.assertEqual(self.geocode('Bangalore', 'Bangalore')['km_from_city_centre'], 0.0)

    def test_every_lookup_goes_to_maps_again(self):
        calls = []
        with mock.patch.object(tools.maps_pin, 'lookup', lambda a, c: calls.append(a) or (pin(12.93, 77.62), BANGALORE)):
            self.geocode()
            self.geocode()
        self.assertEqual(len(calls), 2)


class MapsPin(unittest.TestCase):
    def test_pin_is_read_from_the_page_address(self):
        url = 'https://www.google.com/maps/place/Koramangala/@12.9279,77.6271,14z/data=!4m6!3d12.9352403!4d77.624532'
        self.assertEqual(maps_pin.pin_from_url(url), (12.93524, 77.62453))  # the exact place wins over the map centre

    def test_map_centre_is_used_when_there_is_no_exact_pin(self):
        self.assertEqual(maps_pin.pin_from_url('https://www.google.com/maps/search/x/@16.5,80.6,12z'), (16.5, 80.6))

    def test_no_pin_in_the_address(self):
        self.assertIsNone(maps_pin.pin_from_url('https://www.google.com/maps/search/x'))


class Prompts(unittest.TestCase):
    def test_coordinator_prompt_is_generic_and_routes_by_scout_status(self):
        text = load_prompt('coordinator')
        for needle in ('ask_user', 'scout_agent', 'needs_input', 'ready'):
            self.assertIn(needle, text)
        self.assertNotIn('geocode', text)  # what the search tool needs is the scout agent's knowledge, not the coordinator's

    def test_scout_prompt_forbids_invented_coordinates_and_guessing(self):
        text = load_prompt('scout')
        self.assertIn('geocode', text)
        self.assertIn('NEVER write coordinates from memory', text)
        self.assertIn('NEVER guess', text)


def result(**fields):
    base = dict(status='ready', business_type='gyms', area='Koramangala', city='Bangalore')
    return Report(**{**base, **fields})


GOOD_ARGS = GmapsArgs(query='gyms in Koramangala Bangalore', lat=12.93, lng=77.62)


class Finalize(unittest.TestCase):
    def test_missing_city_forces_needs_input_even_if_model_said_ready(self):
        out = finalize(result(city=None, args=GOOD_ARGS))
        self.assertEqual((out['status'], out['missing'], out['args']), ('needs_input', ['city'], None))
        self.assertTrue(out['question'])

    def test_all_known_with_coordinates_is_ready(self):
        out = finalize(result(args=GOOD_ARGS, problem='stale'))
        self.assertEqual((out['status'], out['missing'], out['problem']), ('ready', [], None))

    def test_spelling_question_after_failed_lookup_is_kept(self):
        out = finalize(result(status='needs_input', question='Another spelling?', missing=['area']))
        self.assertEqual((out['status'], out['question']), ('needs_input', 'Another spelling?'))

    def test_models_own_missing_list_is_kept_when_stated_details_were_left_out_of_the_report(self):
        out = finalize(result(status='needs_input', business_type=None, area=None, city=None, missing=['area'], question='Which area?'))
        self.assertEqual((out['status'], out['missing'], out['question']), ('needs_input', ['area'], 'Which area?'))

    def test_missing_list_naming_a_filled_detail_falls_back_to_the_empty_fields(self):
        out = finalize(result(status='needs_input', city=None, missing=['area']))
        self.assertEqual(out['missing'], ['city'])

    def test_ready_without_coordinates_becomes_failed(self):
        out = finalize(result(args=GmapsArgs(query='gyms in Koramangala Bangalore')))
        self.assertEqual(out['status'], 'failed')
        self.assertTrue(out['problem'])


class FakePrompt:
    def __init__(self, prompt, version):
        self.prompt, self.version = prompt, version


class FakeLangfuse:
    """Stands in for the Langfuse client: keeps prompts in a dict and counts versions."""
    def __init__(self, stored=None):
        self.stored, self.created = dict(stored or {}), []

    def get_prompt(self, name, **kwargs):
        if name not in self.stored:
            raise LookupError('not found')
        return self.stored[name]

    def create_prompt(self, *, name, prompt, **kwargs):
        version = (self.stored[name].version + 1) if name in self.stored else 1
        self.stored[name] = FakePrompt(prompt, version)
        self.created.append((name, kwargs.get('labels')))
        return self.stored[name]


class Observability(unittest.TestCase):
    def setUp(self):
        observability._versions.clear()

    def test_off_without_keys_so_nothing_is_added_to_a_run(self):
        with mock.patch.object(observability, 'get_langfuse', lambda: None):
            turn = observability.start_turn('abcdefgh12345678', 'Turn 1', 'hi')
            self.assertEqual(turn.config, {})
            turn.finish('reply')  # a no-op when tracing is off
            self.assertEqual(observability.sync_prompts(), {})

    def test_prompts_are_created_once_and_versioned_only_when_the_text_changes(self):
        fake = FakeLangfuse()
        with mock.patch.object(observability, 'get_langfuse', lambda: fake):
            first = observability.sync_prompts()
            self.assertEqual(first, {'coordinator': 1, 'scout': 1})
            self.assertEqual(sorted(name for name, _ in fake.created), ['coordinator', 'scout-agent'])
            self.assertTrue(all(labels == ['production'] for _, labels in fake.created))
            self.assertEqual(observability.sync_prompts(), first)  # same text: no new version
            self.assertEqual(len(fake.created), 2)
            fake.stored['scout-agent'].prompt = 'an older text'  # Langfuse holds a different text than the file
            self.assertEqual(observability.sync_prompts()['scout'], 2)
            self.assertEqual(len(fake.created), 3)

    def test_the_saved_text_is_exactly_the_prompt_file(self):
        fake = FakeLangfuse()
        with mock.patch.object(observability, 'get_langfuse', lambda: fake):
            observability.sync_prompts()
        self.assertEqual(fake.stored['coordinator'].prompt, load_prompt('coordinator'))
        self.assertEqual(fake.stored['scout-agent'].prompt, load_prompt('scout'))

    def test_traces_are_filed_under_the_configured_environment(self):
        captured = {}

        class Spy:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def flush(self):
                pass
        with mock.patch.multiple(config, LANGFUSE_ENABLED=True, LANGFUSE_PUBLIC_KEY='pk', LANGFUSE_SECRET_KEY='sk',
                                 LANGFUSE_ENVIRONMENT='production'), \
                mock.patch('langfuse.Langfuse', Spy), mock.patch('atexit.register'):
            observability.get_langfuse.__wrapped__()  # the cached function without its cache
            self.assertEqual(captured['environment'], 'production')
            self.assertEqual(observability.trace_tags(), ['scout', 'production'])
            self.assertEqual(observability.trace_tags(True), ['scout', 'production', 'hitl'])

    def test_the_environment_defaults_to_development_and_rejects_names_langfuse_would_refuse(self):
        import importlib
        for given, expected in [(None, 'development'), ('Production', 'production'), ('langfuse-x', 'development'), ('a b', 'development')]:
            env = {} if given is None else {'LANGFUSE_TRACING_ENVIRONMENT': given}
            with mock.patch.dict('os.environ', env, clear=False):
                if given is None:
                    os.environ.pop('LANGFUSE_TRACING_ENVIRONMENT', None)
                reloaded = importlib.reload(config)
                self.assertEqual(reloaded.LANGFUSE_ENVIRONMENT, expected, given)
        importlib.reload(config)

    def test_a_langfuse_outage_does_not_break_startup(self):
        class Down(FakeLangfuse):
            def create_prompt(self, **kwargs):
                raise OSError('offline')
        with mock.patch.object(observability, 'get_langfuse', lambda: Down()):
            self.assertEqual(observability.sync_prompts(), {})


class AgentMemory(unittest.TestCase):
    def test_conversation_survives_a_new_checkpointer_on_the_same_file(self):
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, MessagesState, StateGraph
        with tempfile.TemporaryDirectory() as folder:
            path, cfg = Path(folder) / 'memory.sqlite', {'configurable': {'thread_id': 't1'}}
            def build():
                graph = StateGraph(MessagesState)
                graph.add_node('echo', lambda state: {'messages': [('ai', 'ok')]})
                graph.add_edge(START, 'echo'); graph.add_edge('echo', END)
                conn = sqlite3.connect(path, check_same_thread=False)
                return graph.compile(checkpointer=SqliteSaver(conn)), conn
            first, conn = build()
            first.invoke({'messages': [('user', 'hello')]}, cfg)
            conn.close()  # simulates the server stopping
            second, conn = build()
            self.assertEqual(len(second.get_state(cfg).values['messages']), 2)
            self.assertFalse(second.get_state({'configurable': {'thread_id': 'other'}}).values)
            conn.close()


class Server(unittest.TestCase):
    """The routes with an account signed in (who may see what is in test_auth.py)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        patch = mock.patch.object(config, 'DATA_DIR', Path(self.folder.name))
        patch.start()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(patch.stop)
        access._hits.clear()
        self.client = TestClient(server.app)
        token = self.client.post('/api/auth/signup', json={'email': 'tester@example.com', 'password': 'correct horse battery'}).json()['token']
        self.client.headers['Authorization'] = 'Bearer ' + token

    def post(self, body, **headers):
        return self.client.post('/api/chat', json=body, headers=headers)

    def test_rejects_bad_input(self):
        self.assertEqual(self.post({'thread_id': 'x', 'message': 'hi'}).status_code, 400)
        self.assertEqual(self.post({'thread_id': 'abcdefgh12345678', 'message': '  '}).status_code, 400)
        self.assertEqual(self.post({'thread_id': 'abcdefgh12345678', 'message': 'a' * 1001}).status_code, 400)

    def test_rejects_cross_origin(self):
        response = self.post({'thread_id': 'abcdefgh12345678', 'message': 'hi'}, origin='http://evil.example')
        self.assertEqual(response.status_code, 403)

    def test_streams_events_as_sse(self):
        def fake(thread, text):
            yield {'type': 'message', 'text': 'hello'}
            yield {'type': 'done', 'waiting': False}
        with mock.patch.object(server, 'stream_events', fake):
            response = self.post({'thread_id': 'abcdefgh12345678', 'message': 'hi'})
        self.assertEqual(response.headers['content-type'].split(';')[0], 'text/event-stream')
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual([e['type'] for e in events], ['message', 'done'])

    def test_model_errors_become_a_readable_event(self):
        def fake(thread, text):
            raise RuntimeError('GROQ_API_KEY is not set.')
            yield  # pragma: no cover
        with mock.patch.object(server, 'stream_events', fake):
            response = self.post({'thread_id': 'abcdefgh12345678', 'message': 'hi'})
        self.assertIn('GROQ_API_KEY is not set.', response.text)
        self.assertNotIn('Traceback', response.text)

    def test_thread_state_endpoint(self):
        self.assertEqual(self.client.get('/api/thread/bad!').status_code, 400)
        with mock.patch.object(server, 'thread_state', lambda tid: {'exists': False, 'waiting': False}):
            self.assertEqual(self.client.get('/api/thread/abcdefgh12345678').json(), {'exists': False, 'waiting': False})

    def test_chat_history_round_trip_on_disk(self):
        cid = 'abcdefgh12345678'
        folder = self.folder.name
        with mock.patch.object(server, 'forget_thread') as forget:
            self.assertEqual(self.client.get('/api/chats').json(), [])
            body = {'title': 'gyms in Koramangala', 'updated': 5, 'log': [{'u': 'hi'}, {'ev': {'type': 'done'}}]}
            self.assertEqual(self.client.put(f'/api/chats/{cid}', json=body).status_code, 200)
            self.assertEqual(len(list((Path(folder) / 'chats').glob(f'*/{cid}.json'))), 1)  # in the account's own folder
            self.assertEqual(self.client.get('/api/chats').json(), [{'id': cid, 'title': 'gyms in Koramangala', 'updated': 5.0}])
            self.assertEqual(self.client.get(f'/api/chats/{cid}').json()['log'], body['log'])
            self.assertEqual(self.client.delete(f'/api/chats/{cid}').status_code, 200)
            forget.assert_called_once_with(cid)
            self.assertEqual(self.client.get(f'/api/chats/{cid}').status_code, 404)

    def test_chat_history_rejects_bad_ids_bodies_and_other_sites(self):
        folder = self.folder.name
        good = {'title': 't', 'updated': 1, 'log': []}
        self.assertEqual(self.client.put('/api/chats/bad!', json=good).status_code, 400)
        self.assertGreaterEqual(self.client.put('/api/chats/..%2F..%2Fevil', json=good).status_code, 400)  # never matches the route
        self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json={'title': 't'}).status_code, 400)
        self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json={**good, 'log': 'x'}).status_code, 400)
        self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json=good, headers={'origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(self.client.delete('/api/chats/abcdefgh12345678', headers={'origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(list((Path(folder) / 'chats').rglob('*.json')), [])

    def test_static_files_and_unknown_paths(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/app.js').status_code, 200)
        self.assertEqual(self.client.get('/../.env').status_code, 404)
        self.assertEqual(self.client.get('/.env').status_code, 404)


if __name__ == '__main__':
    unittest.main()
