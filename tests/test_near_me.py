"""Offline tests for "near me": the code half of the scout (the checks that do not trust the model).

No model calls and no network. The scout model decides WHAT the person means (near me or a named place); the code decides what the
app can really do about it, because only the code knows whether a device position is held:

  HELD         the person shared the position -> a near-me request is ready, no area, no city, no geocode
  NOT_ASKED    nothing asked yet               -> needs_input with missing ["location"] (the coordinator asks the browser)
  UNAVAILABLE  asked, could not be read        -> ask for area and city in words, like any request with no place named

    python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import ToolMessage  # noqa: E402

from app import config, gmaps, scout_agent, searches  # noqa: E402
from app.prompts import load_prompt  # noqa: E402
from app.scout_agent import HELD, NOT_ASKED, UNAVAILABLE, GmapsArgs, Report, finalize, position_line  # noqa: E402

PIN = {'lat': 12.935, 'lng': 77.625, 'accuracy_m': 38}   # the position as the app keeps it
NUMBERS = ('12.93524', '77.62453', '12.935', '77.625')   # none of these may reach a model or the page


def near_me(**fields):
    """What the scout model writes for "gyms near me" when it was told DEVICE LOCATION: available."""
    base = dict(status='ready', business_type='gyms', use_device_location=True, args=GmapsArgs(query='gyms near me', radius_m=None, lang='en'))
    return Report(**{**base, **fields})


def asks_for_location(**fields):
    """What the scout model writes for "gyms near me" when there is no DEVICE LOCATION line."""
    base = dict(status='needs_input', business_type='gyms', missing=['location'], question=None)
    return Report(**{**base, **fields})


def found(**over):
    args = {'query': 'gyms', 'lat': PIN['lat'], 'lng': PIN['lng'], 'radius_m': None, 'lang': 'en', 'pin': 'device'}
    return {'ok': True, 'search_id': 's0123456789ab', 'count': 20, 'cached': False, 'elapsed_ms': 5, 'args': args,
            'highlights': {'avg_rating': 4.6, 'best': {'name': 'Zest Fitness', 'rating': 4.9, 'reviews': 10}}, 'top': [], **over}


class FinalizeHeld(unittest.TestCase):
    def test_near_me_is_ready_without_area_or_city(self):
        out = finalize(near_me(), HELD)
        self.assertEqual((out['status'], out['missing'], out['question']), ('ready', [], None))
        self.assertEqual((out['args']['query'], out['args']['lat'], out['args']['lng']), ('gyms', None, None))  # the type alone; complete() adds the pin
        self.assertTrue(out['use_device_location'])

    def test_radius_and_language_the_person_asked_for_are_kept(self):
        out = finalize(near_me(args=GmapsArgs(query='gyms near me', radius_m=2000, lang='hi')), HELD)
        self.assertEqual((out['args']['radius_m'], out['args']['lang']), (2000, 'hi'))

    def test_a_model_that_still_asked_for_the_location_is_corrected_by_the_code(self):
        out = finalize(asks_for_location(), HELD)  # the position IS held: the code knows better than the model
        self.assertEqual((out['status'], out['missing'], out['use_device_location']), ('ready', [], True))

    def test_near_me_with_a_city_the_scout_found_to_match_is_still_a_search_around_the_person(self):
        out = finalize(near_me(city='Hyderabad'), HELD)
        self.assertEqual((out['status'], out['use_device_location'], out['args']['query']), ('ready', True, 'gyms'))

    def test_the_scouts_question_about_a_position_in_another_city_is_passed_on(self):
        report = Report(status='needs_input', business_type='gyms', city='Hyderabad', missing=['area'],
                        question='Your position is not in Hyderabad. Do you want places around you, or in an area of Hyderabad?')
        out = finalize(report, HELD)
        self.assertEqual((out['status'], out['missing'], out['use_device_location'], out['question']), ('needs_input', ['area'], False, report.question))

    def test_a_named_area_wins_over_the_position(self):
        out = finalize(near_me(area='Koramangala', city=None), HELD)
        self.assertEqual((out['status'], out['missing'], out['use_device_location']), ('needs_input', ['city'], False))

    def test_a_named_place_with_city_is_the_normal_search(self):
        args = GmapsArgs(query='gyms in Koramangala Bangalore', lat=12.9352, lng=77.6245)
        out = finalize(near_me(area='Koramangala', city='Bangalore', args=args), HELD)
        self.assertEqual((out['status'], out['use_device_location'], out['args']['lat']), ('ready', False, 12.9352))

    def test_the_kind_of_place_is_still_needed_first(self):
        out = finalize(near_me(business_type=None), HELD)
        self.assertEqual((out['status'], out['missing'], out['use_device_location'], out['args']), ('needs_input', ['business_type'], False, None))
        self.assertTrue(out['question'])


class FinalizeNotAskedYet(unittest.TestCase):
    def test_near_me_asks_for_the_location_with_no_question_in_words(self):
        out = finalize(asks_for_location(), NOT_ASKED)
        self.assertEqual((out['status'], out['missing'], out['question'], out['args']), ('needs_input', ['location'], None, None))

    def test_a_model_that_believed_the_position_was_there_is_not_trusted(self):
        out = finalize(near_me(), NOT_ASKED)
        self.assertEqual((out['status'], out['missing'], out['args'], out['use_device_location']), ('needs_input', ['location'], None, False))

    def test_the_default_state_is_not_asked(self):
        self.assertEqual(finalize(near_me()), finalize(near_me(), NOT_ASKED))

    def test_the_kind_of_place_is_asked_before_the_location_when_the_scout_says_it_is_missing(self):
        out = finalize(asks_for_location(business_type=None, missing=['business_type', 'location']), NOT_ASKED)
        self.assertEqual((out['status'], out['missing']), ('needs_input', ['business_type']))

    def test_a_scout_that_only_reports_the_location_is_trusted_about_the_kind_of_place(self):
        out = finalize(asks_for_location(business_type=None), NOT_ASKED)  # it stated "gyms" but left the field empty
        self.assertEqual((out['status'], out['missing'], out['question']), ('needs_input', ['location'], None))

    def test_location_is_never_asked_for_when_a_place_is_named(self):
        out = finalize(asks_for_location(area='Koramangala', city=None), NOT_ASKED)
        self.assertEqual((out['status'], out['missing'], out['question'] is not None), ('needs_input', ['city'], True))


class FinalizeUnavailable(unittest.TestCase):
    def test_an_unreadable_position_means_ask_for_area_and_city_in_words(self):
        out = finalize(asks_for_location(), UNAVAILABLE)  # the scout must not ask for the location again
        self.assertEqual((out['status'], out['missing'], out['args'], out['use_device_location']), ('needs_input', ['area', 'city'], None, False))
        self.assertIn('which area and which city', out['question'])

    def test_a_model_that_claims_the_position_is_there_is_not_trusted_either(self):
        out = finalize(near_me(), UNAVAILABLE)
        self.assertEqual((out['status'], out['missing'], out['use_device_location']), ('needs_input', ['area', 'city'], False))

    def test_near_me_with_a_city_asks_for_the_area_only(self):
        out = finalize(near_me(city='Hyderabad'), UNAVAILABLE)
        self.assertEqual((out['status'], out['missing']), ('needs_input', ['area']))

    def test_the_question_is_the_codes_so_the_person_is_never_asked_for_the_location_again(self):
        asked_again = 'Could you share your current location so I can find nearby gyms?'
        out = finalize(near_me(status='needs_input', missing=['location'], question=asked_again, args=None), UNAVAILABLE)
        self.assertEqual((out['missing'], out['question']), (['area', 'city'], 'Could you tell me which area and which city?'))

    def test_a_named_place_keeps_the_scouts_own_question(self):
        report = Report(status='needs_input', business_type='gyms', area='Koramangala', missing=['city'], question='Which city is Koramangala in?')
        self.assertEqual(finalize(report, UNAVAILABLE)['question'], 'Which city is Koramangala in?')


class FinalizeNamedPlaces(unittest.TestCase):
    """A request that never mentions "near me" must come out the same whatever the app knows about the position."""
    def test_no_state_changes_a_named_place(self):
        reports = [
            Report(status='needs_input', business_type='gyms', city='Chennai', missing=['area'], question='Which area?'),
            Report(status='ready', business_type='gyms', area='Koramangala', city='Bangalore', args=GmapsArgs(query='gyms in Koramangala Bangalore', lat=12.93, lng=77.62)),
            Report(status='needs_input', business_type=None, missing=['business_type', 'area', 'city']),
        ]
        for report in reports:
            self.assertEqual(finalize(report, HELD), finalize(report, NOT_ASKED))
            self.assertEqual(finalize(report, HELD), finalize(report, UNAVAILABLE))

    def test_the_report_format_allows_location_and_describes_the_new_field(self):
        schema = Report.model_json_schema()
        self.assertIn('location', schema['properties']['missing']['items']['enum'])
        self.assertIn('DEVICE LOCATION', schema['properties']['use_device_location']['description'])


class Complete(unittest.TestCase):
    def ready_report(self, **fields):
        return finalize(near_me(**fields), HELD)

    def test_the_search_runs_around_the_shared_position_and_the_numbers_stay_out_of_the_result(self):
        seen = []
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found()) as perform:
            out = scout_agent.complete(self.ready_report(), [], notify=seen.append, device=PIN)
        kwargs = perform.call_args.kwargs
        self.assertEqual((kwargs['query'], kwargs['lat'], kwargs['lng'], kwargs['pin']), ('gyms', PIN['lat'], PIN['lng'], 'device'))
        self.assertEqual((out['status'], out['search']['id'], out['search']['count']), ('ready', 's0123456789ab', 20))
        self.assertEqual((out['args']['lat'], out['args']['lng'], out['args']['pin']), (None, None, 'device'))
        for text in (json.dumps(out), json.dumps(seen)):
            for number in NUMBERS:
                self.assertNotIn(number, text)
        self.assertEqual(len(seen), 1)  # the page is told a search runs, without the numbers

    def test_geocode_is_not_needed_and_not_used(self):
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found()):
            out = scout_agent.complete(self.ready_report(), [], device=PIN)
        self.assertEqual(out['status'], 'ready')

    def test_coordinates_the_model_wrote_are_never_used(self):
        report = self.ready_report()
        report['args']['lat'], report['args']['lng'] = 13.0, 77.0
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found()) as perform:
            scout_agent.complete(report, [], device=PIN)
        self.assertEqual((perform.call_args.kwargs['lat'], perform.call_args.kwargs['lng']), (PIN['lat'], PIN['lng']))

    def test_a_geocode_pin_in_the_history_is_not_used_for_a_near_me_search(self):
        old_pin = ToolMessage(content=json.dumps({'lat': 17.44, 'lng': 78.35}), name='geocode', tool_call_id='1')
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found()) as perform:
            scout_agent.complete(self.ready_report(), [old_pin], device=PIN)
        self.assertEqual(perform.call_args.kwargs['lat'], PIN['lat'])

    def test_nothing_found_asks_for_an_area_and_city_instead(self):
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found(count=0, search_id=None)):
            out = scout_agent.complete(self.ready_report(), [], device=PIN)
        self.assertEqual((out['status'], out['missing'], out['args'], out['use_device_location']), ('needs_input', ['area', 'city'], None, False))
        self.assertIn('your location', out['question'])
        for number in NUMBERS:
            self.assertNotIn(number, json.dumps(out))

    def test_a_broken_search_tool_is_a_failure_and_the_numbers_stay_out_of_it(self):
        bad = {'ok': False, 'kind': 'timeout', 'message': 'Google Maps did not answer within 90 seconds.', 'args': {}}
        with mock.patch.object(scout_agent.searches, 'perform', return_value=bad):
            out = scout_agent.complete(self.ready_report(), [], device=PIN)
        self.assertEqual((out['status'], out['search']), ('failed', {'error': 'timeout'}))
        for number in NUMBERS:
            self.assertNotIn(number, json.dumps(out))

    def test_a_search_for_a_named_area_is_unchanged(self):
        pin = {'lat': 12.9352, 'lng': 77.6245}
        geocoded = ToolMessage(content=json.dumps(pin), name='geocode', tool_call_id='1')
        report = finalize(Report(status='ready', business_type='gyms', area='Koramangala', city='Bangalore',
                                 args=GmapsArgs(query='gyms in Koramangala Bangalore', **pin)), HELD)
        ran = found(args={'query': 'gyms in Koramangala Bangalore', **pin, 'radius_m': None, 'lang': 'en'})
        with mock.patch.object(scout_agent.searches, 'perform', return_value=ran) as perform:
            out = scout_agent.complete(report, [geocoded], device=PIN)
        self.assertEqual((perform.call_args.kwargs['lat'], perform.call_args.kwargs.get('pin')), (12.9352, None))
        self.assertEqual((out['args']['lat'], out['args'].get('pin')), (12.9352, None))  # a named area keeps its pin in the result, as before

    def test_a_zero_result_for_a_named_area_still_asks_for_a_bigger_area(self):
        pin = {'lat': 12.9352, 'lng': 77.6245}
        geocoded = ToolMessage(content=json.dumps(pin), name='geocode', tool_call_id='1')
        report = finalize(Report(status='ready', business_type='gyms', area='Koramangala', city='Bangalore',
                                 args=GmapsArgs(query='gyms in Koramangala Bangalore', **pin)), HELD)
        with mock.patch.object(scout_agent.searches, 'perform', return_value=found(count=0, search_id=None)):
            out = scout_agent.complete(report, [geocoded], device=PIN)
        self.assertEqual((out['status'], out['missing']), ('needs_input', ['area']))
        self.assertIn('Koramangala, Bangalore', out['question'])


class StoredSearch(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        patch = mock.patch.object(config, 'DATA_DIR', Path(folder.name))
        patch.start()
        self.addCleanup(folder.cleanup)
        self.addCleanup(patch.stop)

    @staticmethod
    def scraper(args):
        return {'places': [{'title': 'Gym One', 'categories': ['Gym'], 'address': 'Koramangala', 'latitude': 12.9360, 'longtitude': 77.6250,
                            'review_rating': 4.5, 'review_count': 100, 'link': ''}], 'elapsed_ms': 5}

    def test_a_search_around_the_person_is_marked_and_kept_apart_from_a_named_one(self):
        with mock.patch.object(searches.gmaps, 'run', side_effect=self.scraper) as run:
            device = searches.perform('gyms', PIN['lat'], PIN['lng'], pin='device')
            named = searches.perform('gyms', PIN['lat'], PIN['lng'])
            again = searches.perform('gyms', PIN['lat'], PIN['lng'], pin='device')
        self.assertEqual(run.call_count, 2)  # the named search does not reuse the device one
        self.assertTrue(again['cached'] and again['search_id'] == device['search_id'])
        self.assertNotEqual(device['search_id'], named['search_id'])
        self.assertEqual(searches.load(device['search_id'])['args']['pin'], 'device')
        self.assertNotIn('pin', searches.load(named['search_id'])['args'])

    def test_the_scraper_gets_the_pin_and_no_extra_arguments(self):
        args = gmaps.clean_args('gyms', 12.9352, 77.6245, None, 'en', 'device')
        command = gmaps.build_command('gmaps', 'q.txt', 'out.json', args)
        self.assertIn('12.9352,77.6245', command)
        self.assertNotIn('device', ' '.join(command))
        self.assertNotIn('pin', gmaps.clean_args('gyms', 12.9352, 77.6245))
        self.assertNotIn('pin', gmaps.clean_args('gyms', 12.9352, 77.6245, None, 'en', 'anything else'))


class PositionLine(unittest.TestCase):
    def test_each_state_has_its_line(self):
        self.assertEqual(position_line(HELD), 'DEVICE LOCATION: available')
        self.assertEqual(position_line(UNAVAILABLE), 'DEVICE LOCATION: not available')
        self.assertEqual(position_line(NOT_ASKED), '')

    def test_the_city_is_added_only_to_a_held_position(self):
        self.assertEqual(position_line(HELD, 'Pune'), 'DEVICE LOCATION: available (city: Pune)')
        self.assertEqual(position_line(UNAVAILABLE, 'Pune'), 'DEVICE LOCATION: not available')
        self.assertEqual(position_line(NOT_ASKED, 'Pune'), '')

    def test_the_lines_carry_no_numbers(self):
        for state in (HELD, UNAVAILABLE, NOT_ASKED):
            self.assertFalse(any(ch.isdigit() for ch in position_line(state)))

    def test_the_scout_prompt_knows_every_line_the_code_can_send(self):
        text = load_prompt('scout')
        for state in (HELD, UNAVAILABLE):
            self.assertIn(f'"{position_line(state)}', text)


class Prompts(unittest.TestCase):
    def test_the_scout_prompt_has_the_near_me_block_and_keeps_the_old_safety_rules(self):
        text = load_prompt('scout')
        for needle in ('Near-me requests', 'use_device_location', 'missing ["location"]', 'Do NOT call geocode', 'NEVER write coordinates from memory',
                       'If you are unsure, treat it as not near-me', 'never an area'):
            self.assertIn(needle, text)

    def test_the_scout_prompt_has_no_word_list_for_near_me(self):
        text = load_prompt('scout')
        for needle in ('closest', 'around here', 'mere paas', 'daggara'):
            self.assertNotIn(needle, text)

    def test_the_coordinator_knows_ask_browser_and_still_knows_nothing_about_geocode(self):
        text = load_prompt('coordinator')
        for needle in ('ask_browser', 'missing ["location"]', 'never ask for it in words', 'at most once'):
            self.assertIn(needle, text)
        self.assertNotIn('geocode', text)
        self.assertIn('Only scout_agent decides that', text)


if __name__ == '__main__':
    unittest.main()
