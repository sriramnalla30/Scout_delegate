"""
Step 1 of every search: turn the user's free text into structured fields.

    "I need a gym in Koramangala, Bangalore"
        -> city="Bangalore", area="Koramangala", place_type="gym", intent="find a gym"

This runs BEFORE the graph starts, because we need the place type and
location to call SerpStack.

Prompt: "query-parser" in Langfuse (saved copy: prompts/query-parser.yaml).
"""
from pydantic import BaseModel, Field

from app.llm import get_llm
from app.prompts import ainvoke_prompt


class ParsedQuery(BaseModel):
    city: str = Field(description="City name from the query")
    area: str = Field(description="Area/locality inside the city; the city name if none is mentioned")
    place_type: str = Field(description="Type of place: gym, cafe, restaurant, hotel, ...")
    intent: str = Field(description="What the user wants: best reviewed, cheapest, negotiate price, ...")


async def parse_user_query(query: str) -> ParsedQuery:
    # with_structured_output makes the LLM answer as a ParsedQuery object instead of free text
    llm = get_llm(temperature=0.3).with_structured_output(ParsedQuery)
    return await ainvoke_prompt(llm, "query-parser", query=query)


def build_search_text(parsed: ParsedQuery) -> str:
    """ParsedQuery -> the text we send to Google, e.g. "gym in Koramangala Bangalore"."""
    if parsed.area and parsed.area.lower() != parsed.city.lower():
        location = f"{parsed.area} {parsed.city}"
    else:
        location = parsed.city
    return f"{parsed.place_type} in {location}"
