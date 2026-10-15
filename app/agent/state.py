"""
The agent's "memory" for one conversation.

LangGraph passes this dictionary from node to node. A node reads what it needs
and returns ONLY the keys it wants to change; LangGraph merges them in.
The checkpointer (see graph.py) saves it after every node, per thread_id.
"""
from typing import TypedDict


class AgentState(TypedDict, total=False):
    # ---------- Filled by POST /agent/start ----------
    user_query: str            # what the user typed
    city: str
    place_type: str            # "gym", "cafe", ...
    user_intent: str           # "best reviewed", "cheapest", ...
    serp_results: list[dict]   # all places found by SerpStack

    # ---------- Router ----------
    route: str                 # "search" or "negotiate"

    # ---------- Search path ----------
    show_all: bool             # True = list every place, False = pick one winner
    recommendations: list[dict]          # places to show, best first
    reviews: dict[str, list[str]]        # {place name: [review, review, ...]}
    winner_summary: str                  # LLM's explanation of why the winner won

    # ---------- Negotiation path ----------
    target_price: float | None
    negotiation_history: list[dict]      # [{"role": "agent" | "shopkeeper", "content": "...", "timestamp": int}]
    negotiation_status: str              # see NegotiationStatus in negotiation_nodes.py
    negotiation_start_time: int          # unix time; older SMS in the inbox are ignored
    planned_message: str                 # SMS drafted by the LLM, waiting for human approval
    strategy_reasoning: str              # why the LLM wrote that SMS
