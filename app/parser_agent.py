from functools import lru_cache
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from app.llm import get_llm, retry_middleware
from app.prompts import load_prompt
from app.tools import geocode

REQUIRED = ('business_type', 'area', 'city')


class GmapsArgs(BaseModel):
    """The arguments of the Google Maps search tool."""
    query: str = Field(description='List-search text: "<business type> in <Area> <City>".')
    lat: float | None = Field(default=None, description='Latitude returned by geocode, or null.')
    lng: float | None = Field(default=None, description='Longitude returned by geocode, or null.')
    radius_m: int | None = Field(default=None, description='Search radius in metres, only if the user gave a distance.')
    lang: str = Field(default='en', description='Two-letter language code.')


class ParserResult(BaseModel):
    """What the parser tells the orchestrator: ready, or what it still needs from the person."""
    status: Literal['ready', 'needs_input', 'failed']
    business_type: str | None = Field(default=None, description='Business type found in the text, plural lowercase, or null.')
    area: str | None = Field(default=None, description='Neighbourhood or locality found in the text, or null.')
    city: str | None = Field(default=None, description='City found in the text, or null.')
    missing: list[Literal['business_type', 'area', 'city']] = Field(default_factory=list, description='Details still needed from the person.')
    question: str | None = Field(default=None, description='One short question for the person when status is needs_input.')
    args: GmapsArgs | None = Field(default=None, description='Search tool arguments, only when status is ready.')
    problem: str | None = Field(default=None, description='What went wrong when status is failed.')


def _default_question(missing: list[str]) -> str:
    names = {'business_type': 'what kind of place', 'area': 'which area', 'city': 'which city'}
    return 'Could you tell me ' + ' and '.join(names[m] for m in missing) + '?'


def finalize(result: ParserResult) -> dict:
    """Enforce the rules in code so the status can be trusted whatever the model wrote."""
    out = result.model_copy(deep=True)
    out.missing = [name for name in REQUIRED if not getattr(out, name)]
    if out.missing:
        out.status, out.args, out.problem = 'needs_input', None, None
        out.question = out.question or _default_question(out.missing)
    elif out.status == 'needs_input' and out.question:  # e.g. the map could not find the spelling
        out.args, out.missing = None, out.missing or ['area']
    elif out.args is not None and out.args.lat is not None and out.args.lng is not None:
        out.status, out.question, out.problem, out.missing = 'ready', None, None, []
    else:
        out.status, out.args, out.question = 'failed', None, None
        out.problem = out.problem or 'the map lookup returned no coordinates'
    return out.model_dump()


@lru_cache
def get_parser_agent():
    # ToolStrategy returns the structured result as a tool call. Groq rejects its JSON mode
    # (the automatic choice) when it is combined with other tools such as geocode.
    return create_agent(get_llm(), tools=[geocode], system_prompt=load_prompt('parser'),
                        response_format=ToolStrategy(ParserResult), middleware=[retry_middleware()], name='parser')
