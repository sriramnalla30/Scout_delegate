"""
The agent's flowchart, built with LangGraph.

    START
      │  choose_path()
      ├── route == "search" ─────► rank_places
      │                               │  after_ranking()
      │                               ├── list wanted ──────────────► END
      │                               └──► fetch_reviews ──► pick_winner ──► END
      │
      └── route == "negotiate" ──► check_negotiation
                                      │  after_check()
                                      ├── deal / rejected / waiting ─► END
                                      └──► draft_message ──⏸──► send_sms ──► END
                                                 (pause: human approves the SMS)

Nodes live in search_nodes.py and negotiation_nodes.py.
"""
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.negotiation_nodes import after_check, check_negotiation_node, draft_message_node, send_sms_node
from app.agent.search_nodes import after_ranking, fetch_reviews_node, pick_winner_node, rank_places_node
from app.agent.state import AgentState
from app.telemetry import session_id_var


def choose_path(state: AgentState) -> str:
    """Edge from START: the API sets route="negotiate" when the user clicks Negotiate."""
    return state.get("route") or "search"


def build_agent():
    graph = StateGraph(AgentState)

    # 1. Register every node (a name + the function that runs)
    graph.add_node("rank_places", rank_places_node)
    graph.add_node("fetch_reviews", fetch_reviews_node)
    graph.add_node("pick_winner", pick_winner_node)
    graph.add_node("check_negotiation", check_negotiation_node)
    graph.add_node("draft_message", draft_message_node)
    graph.add_node("send_sms", send_sms_node)

    # 2. Connect them. Conditional edges call a function that returns a key of the dict.
    graph.add_conditional_edges(START, choose_path, {"search": "rank_places", "negotiate": "check_negotiation"})

    graph.add_conditional_edges("rank_places", after_ranking, {"fetch_reviews": "fetch_reviews", "done": END})
    graph.add_edge("fetch_reviews", "pick_winner")
    graph.add_edge("pick_winner", END)

    graph.add_conditional_edges("check_negotiation", after_check, {"draft_message": "draft_message", "done": END})
    graph.add_edge("draft_message", "send_sms")
    graph.add_edge("send_sms", END)

    # 3. Compile.
    #    checkpointer: saves the state after every node, per thread_id (in RAM - lost on restart).
    #    interrupt_before: stop right before send_sms, so a human can approve the SMS.
    return graph.compile(checkpointer=MemorySaver(), interrupt_before=["send_sms"])


def thread_config(thread_id: str) -> dict:
    """LangGraph finds a conversation's saved state by its thread_id."""
    config = {"configurable": {"thread_id": thread_id}}
    # Tracing: without this, the LangChain instrumentor would use thread_id as the session id
    if session_id := session_id_var.get():
        config["metadata"] = {"session_id": session_id}
    return config


# One agent for the whole app. Each user search gets its own thread_id.
agent = build_agent()
