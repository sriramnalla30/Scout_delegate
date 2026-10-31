"""Offline tests: no model calls and no network.

    python -m unittest discover -s tests -v
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, server, tools  # noqa: E402
from app.parser_agent import GmapsArgs, ParserResult, finalize  # noqa: E402
from app.prompts import load_prompt  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def nominatim(rows):
    return mock.patch.object(tools.urllib.request, 'urlopen', lambda *a, **k: FakeResponse(json.dumps(rows).encode()))


class Geocode(unittest.TestCase):
    def setUp(self):
        tools._GEOCODE_CACHE.clear()
        tools._LAST_GEOCODE = 0.0

    def test_found(self):
        with nominatim([{'lat': '12.9352', 'lon': '77.6245', 'display_name': 'Koramangala, Bengaluru'}]):
            result = tools.geocode.invoke({'place': 'Koramangala, Bangalore, India'})
        self.assertEqual((result['lat'], result['lng']), (12.9352, 77.6245))

    def test_not_found_is_reported_not_raised(self):
        with nominatim([]):
            result = tools.geocode.invoke({'place': 'Nowhereville'})
        self.assertIn('error', result)

    def test_network_failure_is_reported_not_raised(self):
        def boom(*a, **k):
            raise OSError('offline')
        with mock.patch.object(tools.urllib.request, 'urlopen', boom):
            self.assertIn('error', tools.geocode.invoke({'place': 'Anywhere'}))

    def test_cached(self):
        with nominatim([{'lat': '1', 'lon': '2', 'display_name': 'x'}]):
            first = tools.geocode.invoke({'place': 'Same Place'})
        with mock.patch.object(tools.urllib.request, 'urlopen', side_effect=AssertionError('should not be called')):
            self.assertEqual(tools.geocode.invoke({'place': ' same place '}), first)


class Prompts(unittest.TestCase):
    def test_orchestrator_prompt_is_generic_and_routes_by_parser_status(self):
        text = load_prompt('orchestrator')
        for needle in ('ask_user', 'parser_agent', 'needs_input', 'ready'):
            self.assertIn(needle, text)
        self.assertNotIn('geocode', text)  # what the search tool needs is the parser's knowledge, not Scout's

    def test_parser_prompt_forbids_invented_coordinates_and_guessing(self):
        text = load_prompt('parser')
        self.assertIn('geocode', text)
        self.assertIn('NEVER write coordinates from memory', text)
        self.assertIn('NEVER guess', text)


def result(**fields):
    base = dict(status='ready', business_type='gyms', area='Koramangala', city='Bangalore')
    return ParserResult(**{**base, **fields})


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

    def test_ready_without_coordinates_becomes_failed(self):
        out = finalize(result(args=GmapsArgs(query='gyms in Koramangala Bangalore')))
        self.assertEqual(out['status'], 'failed')
        self.assertTrue(out['problem'])


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
    def setUp(self):
        self.client = TestClient(server.app)

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
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(config, 'DATA_DIR', Path(folder)), \
                mock.patch.object(server, 'forget_thread') as forget:
            self.assertEqual(self.client.get('/api/chats').json(), [])
            body = {'title': 'gyms in Koramangala', 'updated': 5, 'log': [{'u': 'hi'}, {'ev': {'type': 'done'}}]}
            self.assertEqual(self.client.put(f'/api/chats/{cid}', json=body).status_code, 200)
            self.assertTrue((Path(folder) / 'chats' / f'{cid}.json').is_file())
            self.assertEqual(self.client.get('/api/chats').json(), [{'id': cid, 'title': 'gyms in Koramangala', 'updated': 5.0}])
            self.assertEqual(self.client.get(f'/api/chats/{cid}').json()['log'], body['log'])
            self.assertEqual(self.client.delete(f'/api/chats/{cid}').status_code, 200)
            forget.assert_called_once_with(cid)
            self.assertEqual(self.client.get(f'/api/chats/{cid}').status_code, 404)

    def test_chat_history_rejects_bad_ids_bodies_and_other_sites(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(config, 'DATA_DIR', Path(folder)):
            good = {'title': 't', 'updated': 1, 'log': []}
            self.assertEqual(self.client.put('/api/chats/bad!', json=good).status_code, 400)
            self.assertGreaterEqual(self.client.put('/api/chats/..%2F..%2Fevil', json=good).status_code, 400)  # never matches the route
            self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json={'title': 't'}).status_code, 400)
            self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json={**good, 'log': 'x'}).status_code, 400)
            self.assertEqual(self.client.put('/api/chats/abcdefgh12345678', json=good, headers={'origin': 'http://evil.example'}).status_code, 403)
            self.assertEqual(self.client.delete('/api/chats/abcdefgh12345678', headers={'origin': 'http://evil.example'}).status_code, 403)
            self.assertEqual(list((Path(folder) / 'chats').glob('*')), [])

    def test_static_files_and_unknown_paths(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/app.js').status_code, 200)
        self.assertEqual(self.client.get('/../.env').status_code, 404)
        self.assertEqual(self.client.get('/.env').status_code, 404)


if __name__ == '__main__':
    unittest.main()
