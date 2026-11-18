"""Offline tests for the model-call safety net: a report the model wrote as plain text is not lost.

    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from groq import BadRequestError  # noqa: E402
from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import ModelResponse  # noqa: E402
from langchain.agents.structured_output import ToolStrategy  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402

from app import llm  # noqa: E402
from app.scout_agent import Report  # noqa: E402

REPORT = {'status': 'ready', 'business_type': 'gyms', 'area': 'Koramangala', 'city': 'Bangalore',
          'args': {'query': 'gyms in Koramangala Bangalore', 'lat': 12.93524, 'lng': 77.62453, 'radius_m': None, 'lang': 'en'}}


def refused(text, code='tool_use_failed'):
    """The 400 Groq sends when the model wrote its answer as text where a tool call was required."""
    body = {'error': {'message': 'Tool choice is required, but model did not call a tool', 'type': 'invalid_request_error', 'code': code, 'failed_generation': text}}
    response = httpx.Response(400, request=httpx.Request('POST', 'https://api.groq.com/openai/v1/chat/completions'))
    return BadRequestError(f'Error code: 400 - {body}', response=response, body=body)


def request(schema=Report):
    return SimpleNamespace(response_format=ToolStrategy(schema) if schema else None)


class Recover(unittest.TestCase):
    def call(self, error, req=None):
        def handler(_request):
            raise error
        return llm.recover_structured_reply().wrap_model_call(req or request(), handler)

    def test_the_report_the_model_wrote_as_text_becomes_the_answer(self):
        out = self.call(refused(json.dumps(REPORT, indent=2)))
        self.assertIsInstance(out, ModelResponse)
        self.assertEqual((out.structured_response.status, out.structured_response.area), ('ready', 'Koramangala'))
        self.assertEqual(out.result[0].tool_calls[0]['name'], 'Report')
        self.assertEqual(out.result[1].tool_call_id, out.result[0].tool_calls[0]['id'])

    def test_a_report_in_a_code_fence_is_read_too(self):
        out = self.call(refused('```json\n' + json.dumps(REPORT) + '\n```'))
        self.assertEqual(out.structured_response.city, 'Bangalore')

    def test_a_fine_answer_passes_through(self):
        sentinel = object()
        self.assertIs(llm.recover_structured_reply().wrap_model_call(request(), lambda _r: sentinel), sentinel)

    def test_anything_else_is_raised_so_the_retry_can_ask_again(self):
        for error, req in ((refused('not json at all'), None), (refused(json.dumps({'status': 'maybe'})), None),   # not a valid report
                           (refused(json.dumps(REPORT), code='other_error'), None),                                 # another kind of 400
                           (refused(json.dumps(REPORT)), request(None))):                                           # the agent had no report to give
            with self.assertRaises(BadRequestError):
                self.call(error, req)

    def test_a_missing_failed_generation_is_raised(self):
        with self.assertRaises(BadRequestError):
            self.call(refused(None))


class Boom(BaseChatModel):
    """A model that always answers the way Groq refused: the report as text."""
    calls: int = 0

    @property
    def _llm_type(self):
        return 'boom'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        raise refused(json.dumps(REPORT))


class InsideTheAgent(unittest.TestCase):
    def build(self, *middleware):
        model = Boom()
        return model, create_agent(model, tools=[], response_format=ToolStrategy(Report), middleware=list(middleware))

    def test_the_agent_finishes_with_the_recovered_report_on_the_first_call(self):
        model, agent = self.build(llm.retry_middleware(), llm.recover_structured_reply())
        done = agent.invoke({'messages': [{'role': 'user', 'content': 'gyms in Koramangala Bangalore'}]})
        self.assertEqual((done['structured_response'].status, done['structured_response'].args.lat), ('ready', 12.93524))
        self.assertEqual(model.calls, 1)  # no retries were needed

    def test_without_the_net_the_same_model_fails_after_its_retries(self):
        model, agent = self.build(llm.retry_middleware())
        with mock.patch('time.sleep'), self.assertRaises(BadRequestError):
            agent.invoke({'messages': [{'role': 'user', 'content': 'gyms in Koramangala Bangalore'}]})
        self.assertEqual(model.calls, 4)  # the first try and three retries, all refused


if __name__ == '__main__':
    unittest.main()
