"""Offline tests for the "near me" flow from the coordinator down: position holder, ask_browser, the route, and a whole conversation.

Both models are scripted (no Groq, no network, no scraper). What matters most:
  - the three states of the position (held, could not be read, not asked) and that only one request holds it
  - ask_browser pauses the run and comes back as one sentence, never the numbers
  - the numbers never reach a model, an event, the saved conversation memory or the route's response
  - every way out works: allowed, blocked, ignored (typed instead), position sent when nothing was asked

    python -m unittest discover -s tests -v
"""
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app import access, config, coordinator, location, observability, scout_agent, server, tools  # noqa: E402
from app.location import HELD, NOT_ASKED, UNAVAILABLE  # noqa: E402
from app.prompts import load_prompt  # noqa: E402
from app.scout_agent import GmapsArgs, Report  # noqa: E402

THREAD = 'abcdefgh12345678'
SHARED = {'lat': 12.93524, 'lng': 77.62453, 'accuracy_m': 38.4}   # what the browser reports
PIN = {'lat': 12.935, 'lng': 77.625, 'accuracy_m': 38}             # the same, as the app keeps it
NUMBERS = ('12.93524', '77.62453', '12.935', '77.625')             # none of these may reach a model, an event or the saved memory


def near_me(**fields):
    base = dict(status='ready', business_type='gyms', use_device_location=True, args=GmapsArgs(query='gyms near me'))
    return Report(**{**base, **fields})


def found():
    args = {'query': 'gyms', 'lat': PIN['lat'], 'lng': PIN['lng'], 'radius_m': None, 'lang': 'en', 'pin': 'device'}
    return {'ok': True, 'search_id': 's0123456789ab', 'count': 20, 'cached': False, 'elapsed_ms': 5, 'args': args,
            'highlights': {'avg_rating': 4.6, 'best': {'name': 'Zest Fitness', 'rating': 4.9, 'reviews': 10}}, 'top': []}


class Holder(unittest.TestCase):
    def setUp(self):
        self.addCleanup(location.forget, THREAD)
        self.addCleanup(location.forget, 'other-chat')

    def test_a_good_position_is_rounded_to_about_110_m(self):
        self.assertEqual(location.clean(SHARED), PIN)

    def test_accuracy_is_optional(self):
        self.assertEqual(location.clean({'lat': 12.9, 'lng': 77.6}), {'lat': 12.9, 'lng': 77.6, 'accuracy_m': None})

    def test_anything_that_is_not_a_position_is_ignored(self):
        for bad in (None, 'here', 5, [1, 2], {}, {'lat': 'x', 'lng': 1}, {'lat': 91, 'lng': 0.5}, {'lat': 0.5, 'lng': 181},
                    {'lat': float('nan'), 'lng': 1}, {'lat': True, 'lng': 1}, {'lat': 0, 'lng': 0}):
            self.assertIsNone(location.clean(bad), bad)

    def test_the_three_states(self):
        self.assertEqual(location.state(THREAD), NOT_ASKED)
        location.hold(THREAD, PIN)
        self.assertEqual((location.state(THREAD), location.current(THREAD)), (HELD, PIN))
        location.release(THREAD)
        self.assertEqual((location.state(THREAD), location.current(THREAD)), (NOT_ASKED, None))
        location.mark_unavailable(THREAD)
        self.assertEqual((location.state(THREAD), location.current(THREAD)), (UNAVAILABLE, None))

    def test_a_person_who_could_not_be_reached_is_not_asked_again_after_the_request_ends(self):
        location.mark_unavailable(THREAD)
        location.release(THREAD)
        self.assertEqual(location.state(THREAD), UNAVAILABLE)

    def test_a_new_position_replaces_the_flag_and_a_flag_replaces_the_position(self):
        location.mark_unavailable(THREAD)
        location.hold(THREAD, PIN)
        self.assertEqual(location.state(THREAD), HELD)
        location.mark_unavailable(THREAD)
        self.assertEqual((location.state(THREAD), location.current(THREAD)), (UNAVAILABLE, None))

    def test_chats_do_not_share(self):
        location.hold('other-chat', PIN)
        self.assertEqual((location.state(THREAD), location.current(THREAD)), (NOT_ASKED, None))
        self.assertEqual(location.state(None), NOT_ASKED)

    def test_nothing_is_kept_for_ever(self):
        with mock.patch.object(location.time, 'monotonic', return_value=1000.0):
            location.hold(THREAD, PIN)
            location.hold('other-chat', PIN)
            location.mark_unavailable('other-chat')
        with mock.patch.object(location.time, 'monotonic', return_value=1000.0 + location.HELD_SECONDS + 1):
            self.assertEqual((location.state(THREAD), location.state('other-chat')), (NOT_ASKED, NOT_ASKED))

    def test_forgetting_a_chat_clears_both(self):
        location.mark_unavailable(THREAD)
        location.forget(THREAD)
        self.assertEqual(location.state(THREAD), NOT_ASKED)

    def test_reason_words_for_the_codes_the_page_sends(self):
        self.assertIn('blocked', location.reason('denied'))
        self.assertIn('in time', location.reason('timeout'))
        self.assertEqual(location.reason('made up'), location.reason('unavailable'))
        self.assertEqual(location.reason(None), location.reason('unavailable'))
        self.assertEqual(location.reason({'x': 1}), location.reason('unavailable'))


class AskBrowserTool(unittest.TestCase):
    def graph(self):
        def node(state):
            return {'messages': [AIMessage(content=tools.ask_browser.invoke({}))]}
        g = StateGraph(MessagesState)
        g.add_node('ask', node)
        g.add_edge(START, 'ask')
        g.add_edge('ask', END)
        return g.compile(checkpointer=InMemorySaver())

    def test_it_pauses_for_the_browser_and_returns_the_sentence_it_was_resumed_with(self):
        graph, run = self.graph(), {'configurable': {'thread_id': THREAD}}
        paused = graph.invoke({'messages': [('user', 'x')]}, run)
        self.assertEqual(paused['__interrupt__'][0].value, {'kind': 'browser'})
        done = graph.invoke(Command(resume='Position received.'), run)
        self.assertEqual(done['messages'][-1].content, 'Position received.')

    def test_the_description_tells_a_new_colleague_what_it_is_and_what_it_is_not(self):
        text = tools.ask_browser.description
        for needle in ('browser', 'Allow', 'not ask_user', 'you never get it', 'Position received', 'call scout_agent again'):
            self.assertIn(needle, text)


class ScoutToolReadsThePosition(unittest.TestCase):
    """The scout_agent tool run inside a graph with a fake scout model: where the position comes from, and what is told to whom."""

    class FakeScout:
        def __init__(self, report):
            self.report, self.inputs = report, []

        def stream(self, payload, stream_mode=None):
            self.inputs.append(payload['messages'][0]['content'])
            yield {'messages': [], 'structured_response': self.report}

    def setUp(self):
        self.addCleanup(location.forget, THREAD)

    def run_tool(self, report, request='gyms near me'):
        scout, results = self.FakeScout(report), []

        def node(state):
            results.append(tools.scout_agent.invoke({'request': request}))
            return {'messages': [AIMessage(content=json.dumps(results[0]))]}

        graph = StateGraph(MessagesState)
        graph.add_node('scout', node)
        graph.add_edge(START, 'scout')
        graph.add_edge('scout', END)
        with mock.patch.object(scout_agent, 'get_scout_agent', return_value=scout), \
                mock.patch.object(scout_agent.searches, 'perform', return_value=found()) as perform:
            graph.compile().invoke({'messages': [('user', request)]}, {'configurable': {'thread_id': THREAD}})
        return results[0], scout, perform

    def test_held_the_model_is_told_one_line_and_the_search_runs_around_the_pin(self):
        location.hold(THREAD, PIN)
        result, scout, perform = self.run_tool(near_me())
        self.assertEqual(scout.inputs, ['gyms near me\n\nDEVICE LOCATION: available'])
        for number in NUMBERS:
            self.assertNotIn(number, scout.inputs[0])
            self.assertNotIn(number, json.dumps(result))
        self.assertEqual((result['status'], result['search']['count']), ('ready', 20))
        self.assertEqual((perform.call_args.kwargs['lat'], perform.call_args.kwargs['pin']), (PIN['lat'], 'device'))

    def test_not_asked_the_input_is_the_request_and_the_answer_is_missing_location(self):
        report = Report(status='needs_input', business_type='gyms', missing=['location'])
        result, scout, perform = self.run_tool(report)
        self.assertEqual(scout.inputs, ['gyms near me'])
        self.assertEqual((result['status'], result['missing'], result['question']), ('needs_input', ['location'], None))
        perform.assert_not_called()

    def test_not_available_the_scout_is_told_so_and_the_person_is_asked_in_words(self):
        location.mark_unavailable(THREAD)
        result, scout, perform = self.run_tool(near_me())  # even a model that insists on the position is overruled
        self.assertEqual(scout.inputs, ['gyms near me\n\nDEVICE LOCATION: not available'])
        self.assertEqual((result['status'], result['missing']), ('needs_input', ['area', 'city']))
        perform.assert_not_called()

    def test_another_chats_position_is_not_used(self):
        location.hold('other-chat', PIN)
        self.addCleanup(location.forget, 'other-chat')
        result, scout, perform = self.run_tool(near_me())
        self.assertEqual(scout.inputs, ['gyms near me'])
        self.assertEqual(result['missing'], ['location'])
        perform.assert_not_called()


class ScriptedModel(BaseChatModel):
    """A coordinator model that says exactly what the script says: no network, no key."""
    script: list

    @property
    def _llm_type(self) -> str:
        return 'scripted'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


_ids = itertools.count(1)


def call(name, **args):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'call_{next(_ids)}'}])


class WholeConversation(unittest.TestCase):
    """The real coordinator graph and tools, with a scripted coordinator model and a scripted scout model."""

    class FakeScout:
        """Reads the last line of its input the way the real scout prompt tells it to."""
        def __init__(self):
            self.inputs = []

        def stream(self, payload, stream_mode=None):
            text = payload['messages'][0]['content']
            self.inputs.append(text)
            if 'Koramangala Bangalore' in text:  # a named place wins, whatever the last line says
                report = Report(status='ready', business_type='gyms', area='Koramangala', city='Bangalore',
                                args=GmapsArgs(query='gyms in Koramangala Bangalore', lat=12.9352, lng=77.6245))
            elif 'DEVICE LOCATION: available' in text:
                report = near_me()
            elif 'DEVICE LOCATION: not available' in text:
                report = Report(status='needs_input', business_type='gyms', missing=['area', 'city'], question='Which area and city?')
            else:
                report = Report(status='needs_input', business_type='gyms', missing=['location'])
            yield {'messages': [], 'structured_response': report}

    def setUp(self):
        self.scout = self.FakeScout()
        self.saver = InMemorySaver()
        self.thread = f'whole{id(self) % 100000:05d}abc'
        self.addCleanup(location.forget, self.thread)
        for patch in (mock.patch.object(observability, 'get_langfuse', lambda: None),
                      mock.patch.object(scout_agent, 'get_scout_agent', return_value=self.scout),
                      mock.patch.object(scout_agent.searches, 'perform', side_effect=lambda **kw: found())):
            patch.start()
            self.addCleanup(patch.stop)

    def start(self, *script):
        model = ScriptedModel(script=list(script))
        graph = create_agent(model, tools=[tools.ask_user, tools.ask_browser, tools.scout_agent], system_prompt=load_prompt('coordinator'),
                             checkpointer=self.saver, name='Coordinator')
        patch = mock.patch.object(coordinator, 'get_coordinator', return_value=graph)
        patch.start()
        self.addCleanup(patch.stop)

    def events(self, text, **kw):
        return list(coordinator.stream_events(self.thread, text, **kw))

    @staticmethod
    def types(events):
        return [e['type'] for e in events]

    def saved_memory(self) -> str:
        values = coordinator.get_coordinator().get_state({'configurable': {'thread_id': self.thread}}).values
        return json.dumps([m.model_dump() for m in values['messages']], default=str)

    def assert_no_numbers(self, *texts):
        for text in texts:
            for number in NUMBERS:
                self.assertNotIn(number, text)

    def first_turn(self):
        self.start(call('scout_agent', request='gyms near me'), call('ask_browser'),
                   call('scout_agent', request='gyms near me. Position received.'), AIMessage(content='Found 20 gyms near you. The best is Zest Fitness.'))
        return self.events('gyms near me')

    def test_the_first_turn_asks_the_browser_and_pauses(self):
        events = self.first_turn()
        self.assertEqual([(e['type'], e.get('name')) for e in events if e['type'] in ('tool_call', 'tool_result', 'browser', 'interrupt')],
                         [('tool_call', 'scout_agent'), ('tool_result', 'scout_agent'), ('tool_call', 'ask_browser'), ('browser', None)])
        self.assertEqual(events[-1], {'type': 'done', 'waiting': True})
        scouted = next(e for e in events if e['type'] == 'tool_result')
        self.assertEqual((scouted['data']['status'], scouted['data']['missing']), ('needs_input', ['location']))
        self.assertEqual(coordinator.pending_kind(self.thread), 'browser')
        self.assertEqual(coordinator.thread_state(self.thread), {'exists': True, 'waiting': True, 'kind': 'browser'})

    def test_allowed_the_search_runs_around_the_person_and_no_number_is_seen_anywhere(self):
        self.first_turn()
        events = self.events('', position=location.clean(SHARED))
        names = [(e['type'], e.get('name')) for e in events if e['type'] in ('tool_call', 'tool_result', 'message')]
        self.assertEqual(names, [('tool_result', 'ask_browser'), ('tool_call', 'scout_agent'), ('tool_result', 'scout_agent'), ('message', None)])
        self.assertEqual(next(e for e in events if e.get('name') == 'ask_browser' and e['type'] == 'tool_result')['data'], 'Position received.')
        ready = next(e for e in events if e['type'] == 'tool_result' and e['name'] == 'scout_agent')['data']
        self.assertEqual((ready['status'], ready['search']['count'], ready['use_device_location']), ('ready', 20, True))
        self.assertEqual((ready['args']['lat'], ready['args']['lng']), (None, None))
        self.assertEqual(self.scout.inputs[-1], 'gyms near me. Position received.\n\nDEVICE LOCATION: available')
        self.assertEqual(events[-1], {'type': 'done', 'waiting': False})
        self.assert_no_numbers(json.dumps(events), self.saved_memory(), *self.scout.inputs)
        self.assertEqual(location.state(self.thread), NOT_ASKED)  # the request is over: the position is gone

    def test_blocked_the_person_is_asked_for_the_area_and_city_in_words(self):
        self.first_turn()
        self.start(call('scout_agent', request='gyms near me. Position not available.'), call('ask_user', question='Which area and city?'))
        # a new scripted model for the rest of this conversation: it replaces the unused part of the first script
        events = self.events('', position_error='denied')
        results = [e['data'] for e in events if e['type'] == 'tool_result']
        self.assertEqual(results[0], 'Position not available: the person blocked location access.')
        self.assertEqual(self.scout.inputs[-1], 'gyms near me. Position not available.\n\nDEVICE LOCATION: not available')
        self.assertEqual([e for e in events if e['type'] == 'interrupt'], [{'type': 'interrupt', 'question': 'Which area and city?'}])
        self.assertEqual(events[-1], {'type': 'done', 'waiting': True})
        self.assertEqual(coordinator.pending_kind(self.thread), 'question')
        self.assertEqual(location.state(self.thread), UNAVAILABLE)  # not asked again in this chat straight away

    def test_typing_an_answer_instead_is_the_answer_to_the_browser_request(self):
        self.first_turn()
        self.start(call('scout_agent', request='gyms near me. The person typed instead: Koramangala Bangalore.'), AIMessage(content='Found 1.'))
        events = self.events('Koramangala Bangalore')
        results = [e['data'] for e in events if e['type'] == 'tool_result']
        self.assertEqual(results[0], 'The person typed instead: Koramangala Bangalore.')
        self.assertEqual(results[1]['status'], 'ready')
        self.assertFalse(results[1]['use_device_location'])
        self.assertEqual(self.scout.inputs[-1].splitlines()[-1], 'DEVICE LOCATION: not available')  # a named place wins whatever this says

    def test_a_position_sent_when_nothing_was_asked_is_not_held(self):
        self.start(call('scout_agent', request='gyms near me'), call('ask_browser'))
        events = self.events('gyms near me', position=location.clean(SHARED))
        self.assertEqual(self.scout.inputs, ['gyms near me'])  # no DEVICE LOCATION line: the app never asked for it
        self.assertEqual(events[-1], {'type': 'done', 'waiting': True})
        self.assert_no_numbers(json.dumps(events))

    def test_a_named_place_never_asks_the_browser(self):
        self.start(call('scout_agent', request='gyms in Koramangala Bangalore'), AIMessage(content='Found 20 gyms in Koramangala, Bangalore.'))
        events = self.events('gyms in Koramangala Bangalore')
        self.assertNotIn('browser', self.types(events))
        self.assertEqual(self.scout.inputs, ['gyms in Koramangala Bangalore'])

    def test_each_answer_is_a_turn_of_its_own_in_the_trace_names(self):
        self.first_turn()
        names = []
        with mock.patch.object(observability, 'start_turn', side_effect=lambda t, name, *a, **k: names.append(name) or observability.Turn(t, name, *a)):
            self.events('', position=location.clean(SHARED))
        self.assertEqual(names, ['Turn 2 (HITL answer): Position received.'])


class ChatRoute(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        patch = mock.patch.object(config, 'DATA_DIR', Path(folder.name))
        patch.start()
        self.addCleanup(folder.cleanup)
        self.addCleanup(patch.stop)
        access._hits.clear()
        self.client = TestClient(server.app)
        token = self.client.post('/api/auth/signup', json={'email': 'tester@example.com', 'password': 'correct horse battery'}).json()['token']
        self.client.headers['Authorization'] = 'Bearer ' + token

    def chat(self, pending, **body):
        seen = []

        def fake(thread, text, **extra):
            seen.append((text, extra))
            yield {'type': 'done', 'waiting': False}

        with mock.patch.object(server, 'stream_events', fake), mock.patch.object(server, 'pending_kind', lambda thread: pending):
            response = self.client.post('/api/chat', json={'thread_id': THREAD, **body})
        return response, seen

    def test_a_normal_message_is_passed_on_as_it_was(self):
        response, seen = self.chat(None, message='gyms near me')
        self.assertEqual((response.status_code, seen), (200, [('gyms near me', {})]))

    def test_a_position_answers_a_pending_browser_request_without_any_message(self):
        response, seen = self.chat('browser', location=SHARED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [('', {'position': PIN, 'position_error': None})])
        for number in NUMBERS:
            self.assertNotIn(number, response.text)

    def test_a_reason_answers_it_too(self):
        _, seen = self.chat('browser', location_error='denied')
        self.assertEqual(seen, [('', {'position': None, 'position_error': 'denied'})])

    def test_a_bad_position_counts_as_not_found(self):
        for bad in ('here', {'lat': 'x', 'lng': 1}, {'lat': 500, 'lng': 1}, [1, 2], {'lat': 0, 'lng': 0}):
            response, seen = self.chat('browser', location=bad)
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(seen, [('', {'position': None, 'position_error': 'unavailable'})], bad)

    def test_a_long_reason_is_cut(self):
        _, seen = self.chat('browser', location_error='x' * 500)
        self.assertEqual(len(seen[0][1]['position_error']), 20)

    def test_a_position_when_nothing_waits_for_it_is_refused(self):
        for pending in (None, 'question'):
            response, seen = self.chat(pending, location=SHARED)
            self.assertEqual((response.status_code, seen), (409, []))

    def test_an_empty_message_is_still_refused_when_it_answers_nothing(self):
        self.assertEqual(self.chat(None, message='  ')[0].status_code, 400)
        self.assertEqual(self.chat('browser', message='')[0].status_code, 400)
        self.assertEqual(self.chat(None, message='a' * 1001)[0].status_code, 400)

    def test_typing_an_answer_to_a_browser_request_is_an_ordinary_message(self):
        response, seen = self.chat('browser', message='Koramangala Bangalore')
        self.assertEqual((response.status_code, seen), (200, [('Koramangala Bangalore', {})]))


if __name__ == '__main__':
    unittest.main()
