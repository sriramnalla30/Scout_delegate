"""Offline tests for the Google Maps search step: the scraper runner, the place model, stored results and the scout agent's checks.
No model calls, no network, no scraper binary.

    python -m unittest discover -s tests -v
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402

from app import config, gmaps, places, scout_agent, searches, server, tools  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
PIN = {'lat': 12.9352, 'lng': 77.6245}
ARGS = {'query': 'gyms in Koramangala Bangalore', 'lat': 12.9352, 'lng': 77.6245, 'radius_m': None, 'lang': 'en'}


def week(text='5 am–10 pm', **days):
    return {d: [days.get(d, text)] for d in places.DAYS}


def raw_place(title='Gym One', **fields):
    base = {'title': title, 'categories': ['Gym'], 'address': '1 Main Road, Koramangala', 'latitude': 12.9360, 'longtitude': 77.6250,
            'review_rating': 4.5, 'review_count': 100, 'phone': '09845012345', 'web_site': 'https://gym.example',
            'open_hours': week(), 'timezone': 'Asia/Calcutta', 'data_id': f'0x1:0x{abs(hash(title)) % 0xffff:x}', 'plus_code': '7J4VXMXM+2X', 'link': ''}
    return {**base, **fields}


def at(weekday, hour, minute=0):
    """A moment in IST. weekday 0 = Monday. 2026-11-16 is a Monday."""
    return datetime(2026, 11, 16 + weekday, hour, minute, tzinfo=IST)


class Hours(unittest.TestCase):
    def test_ranges(self):
        r = places._range
        self.assertEqual(r('5 am–10 pm'), (300, 1320))
        self.assertEqual(r('6–11 am'), (360, 660))        # the first time borrows "am" from the last
        self.assertEqual(r('9:30 am–6 pm'), (570, 1080))
        self.assertEqual(r('11 am–1 am'), (660, 1500))         # runs past midnight
        self.assertEqual(r('11–1 pm'), (660, 780))             # 11 am to 1 pm, not 11 pm
        self.assertEqual(r('Open 24 hours'), (0, 1440))
        self.assertIsNone(r('by appointment'))

    def test_schedule_needs_all_seven_readable_days(self):
        self.assertEqual(places.parse_schedule(week(Sunday='Closed'))[6], [])
        self.assertEqual(places.parse_schedule(week('12–3 pm, 6–10 pm'))[0], [[720, 900], [1080, 1320]])
        self.assertIsNone(places.parse_schedule({'Monday': ['5 am–10 pm']}))      # days missing
        self.assertIsNone(places.parse_schedule(week(Monday='by appointment')))        # unreadable: never guess
        self.assertIsNone(places.parse_schedule(None))

    def status(self, hours, when):
        return places.live_status({'schedule': places.parse_schedule(hours), 'timezone': 'Asia/Calcutta'}, when)

    def test_open_and_closed_now(self):
        self.assertEqual(self.status(week(), at(0, 12)), {'open': True, 'text': 'Open · closes 10 pm', 'today': '5 am–10 pm'})
        self.assertEqual(self.status(week(), at(0, 23))['text'], 'Closed · opens tomorrow 5 am')
        self.assertEqual(self.status(week(), at(0, 3))['text'], 'Closed · opens 5 am')
        self.assertEqual(self.status(week(Tuesday='Closed'), at(0, 23))['text'], 'Closed · opens Wed 5 am')

    def test_a_range_running_past_midnight_is_open_after_midnight(self):
        hours = week(Friday='6 pm–2 am')
        self.assertTrue(self.status(hours, at(5, 1, 0))['open'])      # Saturday 01:00 is still Friday's range
        self.assertFalse(self.status(hours, at(5, 3, 0))['open'])

    def test_24_hours_and_unknown(self):
        self.assertEqual(self.status(week('Open 24 hours'), at(3, 4))['text'], 'Open 24 hours')
        self.assertIsNone(self.status(None, at(0, 12))['open'])

    def test_the_clock_that_counts_is_the_places_not_the_servers(self):
        utc = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)       # 11:30 in India
        self.assertTrue(self.status(week(), utc)['open'])


class Phone(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(places.phone_info('09845012345'), {'display': '09845012345', 'e164': '+919845012345', 'kind': 'mobile'})
        self.assertEqual(places.phone_info('080 4123 4567')['kind'], 'landline')
        self.assertEqual(places.phone_info('1800 123 4567')['kind'], 'toll-free')
        self.assertIsNone(places.phone_info(''))
        self.assertIsNone(places.phone_info(None))


class Model(unittest.TestCase):
    def doc(self, *raws):
        return places.build(list(raws), ARGS, 1200)

    def test_the_scrapers_odd_spelling_and_missing_values_are_handled(self):
        p = places.normalise({'title': 'Bare', 'latitude': 12.93, 'longtitude': 77.62}, 1, PIN)
        self.assertEqual((p['lat'], p['lng']), (12.93, 77.62))
        self.assertEqual((p['rating'], p['reviews'], p['phone'], p['schedule']), (None, 0, None, None))
        self.assertIn('no_rating', places.build([{'title': 'Bare', 'latitude': 1, 'longitude': 2}], ARGS, 1)['places'][0]['flags'])

    def test_maps_link_comes_from_the_google_id_when_the_scraper_gives_none(self):
        p = places.normalise(raw_place(data_id='0x3bae:0x1f'), 1, PIN)
        self.assertEqual(p['maps_url'], 'https://www.google.com/maps?cid=31')
        self.assertTrue(places.normalise(raw_place(data_id=''), 1, PIN)['maps_url'].startswith('https://www.google.com/maps/search/'))

    def test_distance_is_from_the_pin(self):
        near = places.normalise(raw_place(latitude=12.9352, longtitude=77.6245), 1, PIN)
        far = places.normalise(raw_place(latitude=12.9552, longtitude=77.6245), 1, PIN)
        self.assertEqual(near['distance_m'], 0)
        self.assertTrue(2000 < far['distance_m'] < 2400)

    def test_score_weighs_the_rating_by_how_many_gave_it(self):
        doc = self.doc(raw_place('Few', review_rating=5.0, review_count=3), raw_place('Many', review_rating=4.6, review_count=900), raw_place('Mid', review_rating=4.0, review_count=100))
        by = {p['name']: p for p in doc['places']}
        self.assertGreater(by['Many']['score'], by['Few']['score'])
        self.assertIn('few_reviews', by['Few']['flags'])
        self.assertEqual(doc['summary']['picks']['best'], by['Many']['id'])

    def test_shared_phones_are_flagged_and_duplicates_dropped(self):
        doc = self.doc(raw_place('A', phone='098450 12345'), raw_place('B', phone='09845012345'), raw_place('C', phone='09000000000'), raw_place('A', phone='098450 12345'))
        self.assertEqual([p['name'] for p in doc['places']], ['A', 'B', 'C'])
        self.assertEqual(doc['summary']['shared_phone_places'], 2)
        self.assertIn('shared_phone', doc['places'][0]['flags'])

    def test_summary_and_coverage(self):
        doc = self.doc(raw_place('A', web_site=''), raw_place('B'))
        s = doc['summary']
        self.assertEqual((s['places'], s['with_website'], s['with_phone'], s['total_reviews']), (2, 1, 2, 200))
        self.assertEqual(doc['coverage']['web_site'], 1)
        self.assertEqual(doc['coverage']['link'], 0)       # a field with no value anywhere is still listed, with 0
        self.assertEqual(s['picks']['closest'], doc['places'][0]['id'])

    def test_view_adds_the_live_status_and_the_open_count(self):
        doc = self.doc(raw_place('A'), raw_place('B', open_hours=week('Closed')))
        seen = places.view(doc, at(0, 12))
        self.assertEqual([p['live']['open'] for p in seen['places']], [True, False])
        self.assertEqual(seen['summary']['open_now'], 1)
        self.assertNotIn('live', doc['places'][0])         # the stored document is never changed

    def test_raw_record_is_kept_whole(self):
        raw = raw_place('A', brand_new_field='x')
        self.assertEqual(self.doc(raw)['places'][0]['raw']['brand_new_field'], 'x')


class Runner(unittest.TestCase):
    def test_arguments_are_checked(self):
        self.assertEqual(gmaps.clean_args('gyms in X', '12.9', '77.6', 999999, 'xx-junk!')['radius_m'], 50000)
        self.assertEqual(gmaps.clean_args('gyms in X', 12.9, 77.6, 10)['radius_m'], 100)
        self.assertEqual(gmaps.clean_args('gyms in X', 12.9, 77.6, lang='hi')['lang'], 'hi')
        self.assertEqual(gmaps.clean_args('gyms in X', 12.9, 77.6, lang='not a code')['lang'], 'en')
        for bad in (('', 1, 1), ('a\nb', 1, 1), ('x', 95, 1), ('x', 1, 200), ('x', None, 1)):
            with self.assertRaises(gmaps.SearchError) as caught:
                gmaps.clean_args(*bad)
            self.assertEqual(caught.exception.kind, 'invalid')

    def test_command(self):
        command = gmaps.build_command('gmaps', 'q.txt', 'out.json', {**ARGS, 'radius_m': 2000, 'lang': 'hi'})
        for part in ('-fast-mode', '-json', '12.9352,77.6245', '-radius', '2000', '-lang', 'hi'):
            self.assertIn(part, command)
        self.assertNotIn('-radius', gmaps.build_command('gmaps', 'q', 'o', ARGS))
        self.assertNotIn('-lang', gmaps.build_command('gmaps', 'q', 'o', ARGS))

    def test_output_with_nulls_and_lists(self):
        text = '{"title": "A"}\n\nnull\n[{"title": "B"}, null]\n'
        self.assertEqual([p['title'] for p in gmaps.parse_output(text)], ['A', 'B'])

    def run_with(self, write=None, returncode=0, timeout=False):
        binary = Path(tempfile.mkdtemp()) / 'gmaps.exe'
        binary.write_bytes(b'x')

        def fake(command, cwd, capture_output, timeout):
            if write is not None:
                Path(command[command.index('-results') + 1]).write_text(write, encoding='utf-8')
            return subprocess.CompletedProcess(command, returncode, b'', b'')

        def slow(command, cwd, capture_output, timeout):
            raise subprocess.TimeoutExpired(command, timeout)
        with mock.patch.object(config, 'GMAPS_BIN', binary), mock.patch.object(gmaps.subprocess, 'run', slow if timeout else fake):
            return gmaps.run(ARGS)

    def test_a_run(self):
        out = self.run_with('{"title": "A"}\n{"title": "B"}\n')
        self.assertEqual(len(out['places']), 2)

    def test_nothing_found_is_a_result_not_an_error(self):
        self.assertEqual(self.run_with('null\n')['places'], [])
        self.assertEqual(self.run_with(None)['places'], [])

    def test_failures(self):
        with mock.patch.object(config, 'GMAPS_BIN', Path('does/not/exist.exe')), self.assertRaises(gmaps.SearchError) as caught:
            gmaps.run(ARGS)
        self.assertEqual(caught.exception.kind, 'unavailable')
        for kwargs, kind in (({'timeout': True}, 'timeout'), ({'write': 'not json', 'returncode': 0}, 'parse'), ({'write': None, 'returncode': 2}, 'failed')):
            with self.assertRaises(gmaps.SearchError) as caught:
                self.run_with(**kwargs)
            self.assertEqual(caught.exception.kind, kind)


class Stored(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(config, 'DATA_DIR', Path(self.folder.name)), mock.patch.object(config, 'SEARCH_CACHE_MINUTES', 30)]
        for p in self.patches:
            p.start()
        self.calls = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def scraper(self, raws):
        def run(args):
            self.calls.append(args)
            return {'places': raws, 'elapsed_ms': 900}
        return mock.patch.object(searches.gmaps, 'run', run)

    def test_results_are_stored_by_id_and_reloaded(self):
        with self.scraper([raw_place('A'), raw_place('B')]):
            out = searches.perform(**ARGS)
        self.assertTrue(out['ok'] and out['count'] == 2 and not out['cached'])
        self.assertRegex(out['search_id'], searches.SEARCH_ID)
        doc = searches.load(out['search_id'])
        self.assertEqual((doc['id'], len(doc['places']), doc['args']['query']), (out['search_id'], 2, ARGS['query']))
        self.assertEqual(out['highlights']['best']['name'], out['top'][0]['name'])

    def test_the_outcome_a_model_sees_is_small(self):
        with self.scraper([raw_place(f'Gym {i}') for i in range(20)]):
            out = searches.perform(**ARGS)
        self.assertLess(len(json.dumps({k: out[k] for k in ('search_id', 'count', 'cached', 'highlights', 'top')})), 900)

    def test_an_identical_recent_search_reuses_the_stored_results(self):
        with self.scraper([raw_place('A')]):
            first = searches.perform(**ARGS)
            again = searches.perform(**{**ARGS, 'query': 'GYMS in Koramangala Bangalore'})   # same search, different case
            other = searches.perform(**{**ARGS, 'lat': 13.5})
        self.assertEqual((again['cached'], again['search_id']), (True, first['search_id']))
        self.assertNotEqual(other['search_id'], first['search_id'])
        self.assertEqual(len(self.calls), 2)

    def test_an_old_search_runs_again_and_the_cache_can_be_switched_off(self):
        with self.scraper([raw_place('A')]):
            first = searches.perform(**ARGS)
            path = Path(self.folder.name) / 'searches' / f"{first['search_id']}.json"
            doc = json.loads(path.read_text('utf-8'))
            doc['created_at'] = time.time() - 31 * 60
            path.write_text(json.dumps(doc), 'utf-8')
            self.assertFalse(searches.perform(**ARGS)['cached'])
            with mock.patch.object(config, 'SEARCH_CACHE_MINUTES', 0):
                self.assertFalse(searches.perform(**ARGS)['cached'])

    def test_nothing_found_is_not_stored(self):
        with self.scraper([]):
            out = searches.perform(**ARGS)
            self.assertEqual((out['ok'], out['count'], out['search_id']), (True, 0, None))
            searches.perform(**ARGS)
        self.assertEqual(len(self.calls), 2)       # not cached: the next ask tries again

    def test_failures_come_back_as_outcomes(self):
        out = searches.perform('', 1, 1)
        self.assertEqual((out['ok'], out['kind']), (False, 'invalid'))
        with mock.patch.object(searches.gmaps, 'run', side_effect=gmaps.SearchError('timeout', 'slow')):
            out = searches.perform(**ARGS)
        self.assertEqual((out['ok'], out['kind'], out['message']), (False, 'timeout', 'slow'))
        self.assertEqual(out['args']['query'], ARGS['query'])

    def test_only_real_ids_are_loaded(self):
        for bad in ('', '../chats/x', 's123', 'sZZZZZZZZZZZZ', None):
            self.assertIsNone(searches.load(bad))

    def test_server_endpoint(self):
        client = TestClient(server.app)
        with self.scraper([raw_place('A')]):
            out = searches.perform(**ARGS)
        body = client.get(f"/api/searches/{out['search_id']}").json()
        self.assertEqual(body['places'][0]['name'], 'A')
        self.assertIn('live', body['places'][0])
        self.assertEqual(client.get('/api/searches/s000000000000').status_code, 404)
        self.assertEqual(client.get('/api/searches/..%2Fx').status_code, 404)
        for path in ('/places.js', '/places.css'):
            self.assertEqual(client.get(path).status_code, 200)


class SearchTool(unittest.TestCase):
    def test_the_model_gets_a_short_answer_and_the_app_gets_the_rest(self):
        full = {'ok': True, 'search_id': 's0123456789ab', 'count': 20, 'cached': False, 'elapsed_ms': 1, 'args': ARGS, 'highlights': {}, 'top': [], 'extra': 1}
        with mock.patch.object(tools.searches, 'perform', return_value=full) as perform:
            message = tools.search_places.invoke({'type': 'tool_call', 'id': '1', 'name': 'search_places', 'args': {'query': 'q', 'lat': 1, 'lng': 2}})
        perform.assert_called_once_with('q', 1, 2, None, 'en')
        self.assertEqual(json.loads(message.content)['count'], 20)
        self.assertNotIn('extra', json.loads(message.content))
        self.assertEqual(message.artifact, full)

    def test_a_failure_is_an_error_the_model_can_report(self):
        with mock.patch.object(tools.searches, 'perform', return_value={'ok': False, 'kind': 'timeout', 'message': 'slow', 'args': ARGS}):
            message = tools.search_places.invoke({'type': 'tool_call', 'id': '1', 'name': 'search_places', 'args': {'query': 'q', 'lat': 1, 'lng': 2}})
        self.assertEqual(json.loads(message.content), {'error': 'slow'})


def tool_msg(name, content, artifact=None):
    return ToolMessage(content=json.dumps(content), name=name, tool_call_id=name, artifact=artifact)


def ready(**args):
    return {'status': 'ready', 'business_type': 'gyms', 'area': 'Koramangala', 'city': 'Bangalore', 'missing': [], 'question': None, 'problem': None,
            'args': {**ARGS, **args}}


def ran(**over):
    return {'ok': True, 'search_id': 's0123456789ab', 'count': 20, 'cached': False, 'elapsed_ms': 5, 'args': ARGS,
            'highlights': {'avg_rating': 4.6, 'best': {'name': 'X', 'rating': 4.9, 'reviews': 10}}, 'top': [], **over}


class Complete(unittest.TestCase):
    """The scout agent's report is checked in code against what the tools really did."""
    PIN = tool_msg('geocode', {'lat': 12.9352, 'lng': 77.6245, 'display_name': 'Koramangala', 'km_from_city_centre': 5})

    def test_a_search_the_agent_ran_for_these_arguments_is_used_not_repeated(self):
        found = ran()
        with mock.patch.object(scout_agent.searches, 'perform') as perform:
            out = scout_agent.complete(ready(), [self.PIN, tool_msg('search_places', {}, found)])
        perform.assert_not_called()
        self.assertEqual((out['status'], out['search']['id'], out['search']['count']), ('ready', 's0123456789ab', 20))

    def test_coordinates_the_model_wrote_are_replaced_by_the_geocode_pin(self):
        with mock.patch.object(scout_agent.searches, 'perform', return_value=ran()) as perform:
            out = scout_agent.complete(ready(lat=13.0, lng=77.0), [self.PIN, tool_msg('search_places', {}, ran(args={**ARGS, 'lat': 13.0, 'lng': 77.0}))])
        self.assertEqual((out['args']['lat'], out['args']['lng']), (12.9352, 77.6245))
        perform.assert_called_once()      # the model searched somewhere else, so the right search runs now
        self.assertEqual(perform.call_args.kwargs['lat'], 12.9352)

    def test_a_skipped_search_is_run_here_and_announced(self):
        seen = []
        with mock.patch.object(scout_agent.searches, 'perform', return_value=ran()) as perform:
            out = scout_agent.complete(ready(), [self.PIN], notify=seen.append)
        perform.assert_called_once()
        self.assertEqual(len(seen), 1)
        self.assertEqual(out['status'], 'ready')

    def test_nothing_found_asks_for_a_wider_area(self):
        with mock.patch.object(scout_agent.searches, 'perform', return_value=ran(count=0, search_id=None)):
            out = scout_agent.complete(ready(), [self.PIN])
        self.assertEqual((out['status'], out['missing'], out['args']), ('needs_input', ['area'], None))
        self.assertIn('Koramangala', out['question'])

    def test_a_broken_search_tool_is_a_failure_with_the_reason(self):
        bad = {'ok': False, 'kind': 'unavailable', 'message': 'The Google Maps search tool is not installed on this machine.', 'args': ARGS}
        out = scout_agent.complete(ready(), [self.PIN, tool_msg('search_places', {'error': 'x'}, bad)])
        self.assertEqual((out['status'], out['problem'], out['search']), ('failed', bad['message'], {'error': 'unavailable'}))

    def test_other_statuses_pass_through(self):
        report = {'status': 'needs_input', 'args': None, 'question': 'Which city?', 'missing': ['city']}
        self.assertIs(scout_agent.complete(report, []), report)

    def test_ready_report_with_nonsense_coordinates_fails_cleanly(self):
        out = scout_agent.complete(ready(lat=999), [])
        self.assertEqual(out['status'], 'failed')


class Prompts(unittest.TestCase):
    def test_the_agents_know_about_the_search(self):
        from app.prompts import load_prompt
        scout = load_prompt('scout')
        for needle in ('search_places', 'count', 'failed'):
            self.assertIn(needle, scout)
        self.assertNotIn('search tool will run next', load_prompt('coordinator'))


if __name__ == '__main__':
    unittest.main()
