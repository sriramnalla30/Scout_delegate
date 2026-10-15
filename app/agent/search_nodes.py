"""
Nodes of the SEARCH path:

    rank_places  ->  (user wants a list?)  -> END
                 ->  fetch_reviews -> pick_winner -> END

A node is just a function: it gets the current state and returns the keys it changes.
Prompts "display-decision" and "pick-winner" come from Langfuse (saved copies in prompts/).
"""
import asyncio
import json
import logging
import re

from pydantic import BaseModel, Field

from app.agent.state import AgentState
from app.llm import get_llm
from app.prompts import ainvoke_prompt
from app.tools.reviews import fetch_reviews

logger = logging.getLogger(__name__)

TOP_CANDIDATES = 3
# Whole words that clearly mean "show me a list": list, all, multiple, top 3, top 10, ...
LIST_WORDS = re.compile(r"\b(list|all|multiple|top ([2-9]|\d{2,}))\b", re.IGNORECASE)


# ------------------------------------------------------------------
# Node 1: rank_places
# ------------------------------------------------------------------

class DisplayDecision(BaseModel):
    show_all: bool = Field(description="True if the user wants a list of places, False if they want the single best one")
    reasoning: str = Field(description="One sentence explaining the decision")


async def rank_places_node(state: AgentState) -> dict:
    """Sort places by rating and decide: show the whole list, or find one winner."""
    places = state.get("serp_results", [])
    user_query = state.get("user_query", "")

    # Highest rating first; ties broken by number of reviews
    ranked = sorted(places, key=lambda p: (p.get("rating") or 0, p.get("reviews_count") or 0), reverse=True)

    # Obvious words like "list" decide on their own; otherwise ask the LLM.
    show_all = bool(LIST_WORDS.search(user_query))
    if not show_all:
        llm = get_llm(temperature=0.3, use_second_key=True).with_structured_output(DisplayDecision)
        try:
            decision = await ainvoke_prompt(llm, "display-decision", user_query=user_query)
            show_all = decision.show_all
            logger.info(f"rank_places: LLM says show_all={show_all} ({decision.reasoning})")
        except Exception as e:
            logger.error(f"rank_places: LLM failed ({e}), picking a single winner")

    return {
        "show_all": show_all,
        # Showing a list -> keep everything. Picking a winner -> keep the top 3 candidates.
        "recommendations": ranked if show_all else ranked[:TOP_CANDIDATES],
    }


def after_ranking(state: AgentState) -> str:
    """Edge: a list needs no review analysis, so stop. Otherwise go read reviews."""
    if state.get("show_all") or not state.get("recommendations"):
        return "done"
    return "fetch_reviews"


# ------------------------------------------------------------------
# Node 2: fetch_reviews
# ------------------------------------------------------------------

async def fetch_reviews_node(state: AgentState) -> dict:
    """Download reviews for each candidate. All candidates are fetched at the same time."""
    candidates = state["recommendations"]

    # fetch_reviews is slow, normal (blocking) code -> run each call in a background thread
    results = await asyncio.gather(*[
        asyncio.to_thread(fetch_reviews, place["name"], place.get("address", ""))
        for place in candidates
    ])

    reviews = {place["name"]: place_reviews for place, place_reviews in zip(candidates, results)}
    logger.info(f"fetch_reviews: got reviews for {list(reviews)}")
    return {"reviews": reviews}


# ------------------------------------------------------------------
# Node 3: pick_winner
# ------------------------------------------------------------------

class WinnerSelection(BaseModel):
    winner_name: str = Field(description="Exact name of the winning place, as written in the input")
    explanation: str = Field(description="Why this place won, based on the reviews")
    key_pros: list[str] = Field(description="3 positive points from the reviews")
    key_cons: list[str] = Field(description="1-2 negative points or warnings, if any")


async def pick_winner_node(state: AgentState) -> dict:
    """Let the LLM read all reviews and choose the best place."""
    candidates = state["recommendations"]
    reviews = state.get("reviews", {})

    candidates_info = [
        {
            "name": place["name"],
            "rating": place.get("rating"),
            "reviews_count": place.get("reviews_count"),
            "reviews": reviews.get(place["name"], []),
        }
        for place in candidates
    ]

    llm = get_llm(temperature=0.2).with_structured_output(WinnerSelection)
    try:
        decision = await ainvoke_prompt(
            llm, "pick-winner",
            user_query=state.get("user_query", ""),
            candidates=json.dumps(candidates_info, indent=2),
        )
    except Exception as e:
        logger.error(f"pick_winner: LLM failed ({e}), falling back to the highest rated place")
        return {
            "recommendations": [candidates[0]],
            "winner_summary": "Could not analyse the reviews, so this is simply the highest rated place.",
        }

    winner = _find_place(candidates, decision.winner_name) or candidates[0]
    summary = (
        f"🏆 WINNER: {winner['name']}\n"
        f"⭐ Rating: {winner.get('rating') or 'N/A'}/5 ({winner.get('reviews_count') or 0:,} reviews)\n"
        f"📍 Address: {winner.get('address') or 'N/A'}\n\n"
        f"Why it won:\n{decision.explanation}\n\n"
        "Highlights:\n" + "\n".join(f"- {pro}" for pro in decision.key_pros) + "\n\n"
        "Things to note:\n" + "\n".join(f"- {con}" for con in decision.key_cons)
    )
    return {"recommendations": [winner], "winner_summary": summary}


def _find_place(places: list[dict], name: str) -> dict | None:
    """Find a place by exact name, or by partial name if the LLM changed it slightly."""
    for place in places:
        if place["name"] == name:
            return place
    for place in places:
        if place["name"] in name or name in place["name"]:
            return place
    return None
