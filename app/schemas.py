"""
Shapes of the JSON bodies the frontend sends to the API.

FastAPI checks every request against these classes automatically and
returns a 422 error if a field is missing or has the wrong type.
"""
from typing import Optional

from pydantic import BaseModel


class StartRequest(BaseModel):
    """POST /agent/start - the user's search sentence."""
    user_query: str          # e.g. "best gym in Koramangala, Bangalore"


class NegotiationStartRequest(BaseModel):
    """POST /agent/negotiate/start - user clicked "Negotiate" on a place."""
    thread_id: str
    place_name: str
    target_price: Optional[float] = None
    goal: Optional[str] = None   # e.g. "Get monthly membership under 2500"


class ApprovalRequest(BaseModel):
    """POST /agent/approve - user approved (or edited) the drafted message."""
    thread_id: str
    approved: bool = True
    edited_message: Optional[str] = None


class ManualMessageRequest(BaseModel):
    """POST /agent/send-message - user typed their own message in the chat box."""
    thread_id: str
    message: str


class ThreadRequest(BaseModel):
    """POST /agent/terminate - only needs the conversation id."""
    thread_id: str
