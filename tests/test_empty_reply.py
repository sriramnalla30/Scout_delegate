"""Offline tests for the empty-reply safety net: a model turn with no tool call and no text must be asked again, and when it stays empty
the person must be told instead of looking at a blank screen. No model calls, no network.

    python -m unittest discover -s tests -v
"""
import itertools
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import ModelResponse  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app import coordinator, llm, observability, scout_agent, tools  # noqa: E402
from app.prompts import load_prompt  # noqa: E402

_ids = itertools.count(1)


def call(name, **args):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'call_{next(_ids)}'}])


EMPTY = lambda: AIMessage(content='', response_metadata={'finish_reason': 'stop'})  # noqa: E731


class Scripted(BaseChatModel):
    """Says exactly what the script says; `reasoning_effort` is recorded like ChatGroq's, so the retries can be seen asking for more."""
    script: list
    reasoning_effort: str | None = 'low'
    efforts: list = []

    @property
    def _llm_type(self) -> str:
        return 'scripted'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.efforts.append(self.reasoning_effort)
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


class Recognising(unittest.TestCase):
    def test_nothing_to_run_and_nothing_to_show_is_empty(self):
        self.assertTrue(llm.is_empty_reply(ModelResponse(result=[EMPTY()])))
        self.assertTrue(llm.is_empty_reply(AIMessage(content='  \n')))
        self.assertTrue(llm.is_empty_reply(AIMessage(content=[{'type': 'text', 'text': ' '}])))

    def test_a_tool_call_or_some_text_is_not_empty(self):
        self.assertFalse(llm.is_empty_reply(ModelResponse(result=[call('ask_browser')])))
        self.assertFalse(llm.is_empty_reply(AIMessage(content='Found 20 gyms.')))
        self.assertFalse(llm.is_empty_reply(AIMessage(content=[{'type': 'text', 'text': 'hi'}])))

    def test_a_response_without_a_model_message_is_not_called_empty(self):
        self.assertFalse(llm.is_empty_reply(ModelResponse(result=[])))

    def test_each_retry_thinks_a_step_harder(self):
        low = Scripted(script=[], reasoning_effort='low')
        self.assertEqual(llm._stronger(low).reasoning_effort, 'medium')
        self.assertEqual(llm._stronger(llm._stronger(low)).reasoning_effort, 'high')
        self.assertEqual(llm._stronger(Scripted(script=[], reasoning_effort='high')).reasoning_effort, 'high')
        plain = types.SimpleNamespace()  # a model without the setting is used as it is
        self.assertIs(llm._stronger(plain), plain)
        self.assertEqual(low.reasoning_effort, 'low')  # the original is not changed


class Middleware(unittest.TestCase):
    def run_it(self, responses, retries=3):
        asked = []

        class Request:
            def __init__(self, model):
                self.model = model

            def override(self, **kw):
                return Request(kw['model'])
        original = Scripted(script=[], reasoning_effort='low')

        def handler(request):
            asked.append(request.model.reasoning_effort)
            return responses[len(asked) - 1]
        out = llm.retry_empty_reply(retries).wrap_model_call(Request(original), handler)
        return out, asked

    def test_a_good_first_reply_is_left_alone(self):
        good = ModelResponse(result=[call('ask_browser')])
        out, asked = self.run_it([good])
        self.assertIs(out, good)
        self.assertEqual(asked, ['low'])

    def test_an_empty_reply_is_asked_again_with_more_thinking(self):
        good = ModelResponse(result=[call('ask_browser')])
        out, asked = self.run_it([ModelResponse(result=[EMPTY()]), good])
        self.assertIs(out, good)
        self.assertEqual(asked, ['low', 'medium'])

    def test_it_gives_up_after_the_retries_and_returns_the_last_reply(self):
        empties = [ModelResponse(result=[EMPTY()]) for _ in range(4)]
        out, asked = self.run_it(empties)
        self.assertIs(out, empties[3])
        self.assertEqual(asked, ['low', 'medium', 'high', 'high'])


def build(script):
    model = Scripted(script=script, efforts=[])
    graph = create_agent(model, tools=[tools.ask_user, tools.ask_browser, tools.scout_agent], system_prompt=load_prompt('coordinator'),
                         checkpointer=InMemorySaver(), middleware=[llm.retry_empty_reply()], name='Coordinator')
    return model, graph


class InAnAgent(unittest.TestCase):
    def test_the_failing_decision_goes_through_after_one_empty_turn(self):
        """The real failure: after the scout said it needs the location the model ended its turn with nothing, so no card appeared."""
        report = {'status': 'needs_input', 'business_type': 'function halls', 'missing': ['location'], 'use_device_location': False}
        model, graph = build([call('scout_agent', request='function halls near me'), EMPTY(), call('ask_browser')])
        with mock.patch.object(scout_agent, 'get_scout_agent', return_value=mock.Mock(stream=lambda *a, **k: iter([{'messages': [], 'structured_response': scout_agent.Report(**{**report})}]))):
            state = graph.invoke({'messages': [('user', 'function halls near me')]}, {'configurable': {'thread_id': 'thread-empty-1'}})
        self.assertEqual(state['__interrupt__'][0].value, {'kind': 'browser'})  # the browser was asked: the person gets the card
        self.assertEqual(model.efforts, ['low', 'low', 'medium'])  # the third call is the retry, thinking harder


class WholeTurn(unittest.TestCase):
    """stream_events with a coordinator whose model stays empty: the person is told, not left with a blank screen."""

    def setUp(self):
        for patch in (mock.patch.object(observability, 'get_langfuse', lambda: None),):
            patch.start()
            self.addCleanup(patch.stop)

    def events(self, script, text='function halls near me'):
        model, graph = build(script)
        with mock.patch.object(coordinator, 'get_coordinator', return_value=graph):
            return model, list(coordinator.stream_events(f'thread-silent-{next(_ids)}', text))

    def test_a_reply_that_stays_empty_after_the_retries_shows_a_notice(self):
        model, events = self.events([EMPTY() for _ in range(4)])
        self.assertEqual([e['type'] for e in events], ['error', 'done'])
        self.assertEqual(events[0]['message'], coordinator.SILENT_NOTICE)
        self.assertEqual(events[-1], {'type': 'done', 'waiting': False})
        self.assertEqual(model.efforts, ['low', 'medium', 'high', 'high'])

    def test_a_normal_reply_shows_no_notice(self):
        _, events = self.events([AIMessage(content='I find places for you.')])
        self.assertEqual([e['type'] for e in events], ['message', 'done'])
        self.assertFalse(any(e['type'] == 'error' for e in events))

    def test_a_turn_that_pauses_for_the_browser_shows_no_notice(self):
        report = scout_agent.Report(status='needs_input', business_type='gyms', missing=['location'])
        scout = mock.Mock(stream=lambda *a, **k: iter([{'messages': [], 'structured_response': report}]))
        with mock.patch.object(scout_agent, 'get_scout_agent', return_value=scout):
            _, events = self.events([call('scout_agent', request='gyms near me'), call('ask_browser')], 'gyms near me')
        self.assertEqual(events[-1], {'type': 'done', 'waiting': True})
        self.assertFalse(any(e['type'] == 'error' for e in events))
        self.assertTrue(any(e['type'] == 'browser' for e in events))


if __name__ == '__main__':
    unittest.main()
