"""
Send and receive chat messages with the shop, over WhatsApp (Twilio).

  send_message(to, message)  -> message a shop from our Twilio WhatsApp number
  get_inbox()                -> messages people sent to our Twilio WhatsApp number (the shop's replies)

Both are normal (blocking) functions; the app calls them with asyncio.to_thread.
Settings: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER (see .env.example).
"""
import logging
import re
from functools import lru_cache

from twilio.base.exceptions import TwilioException
from twilio.rest import Client

from app.config import settings

logger = logging.getLogger(__name__)

INBOX_LIMIT = 50  # newest messages to look at when checking for a reply


@lru_cache(maxsize=1)
def _client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def _our_number() -> str:
    """Our Twilio WhatsApp sender, in the form Twilio expects: 'whatsapp:+14155238886'."""
    return f"whatsapp:{settings.twilio_whatsapp_number}"


def _configured() -> bool:
    return bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_whatsapp_number)


def send_message(to: str, message: str) -> bool:
    """Send one message. Returns True if Twilio accepted it."""
    if not _configured():
        logger.error("Twilio is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_NUMBER) - cannot send")
        return False
    try:
        sent = _client().messages.create(to=f"whatsapp:{to}", from_=_our_number(), body=message)
    except TwilioException as e:
        logger.error(f"Twilio could not send a message to {to}: {e}")
        return False
    logger.info(f"Twilio accepted message {sent.sid} to {to} (status {sent.status})")
    return True


def get_inbox() -> list[dict]:
    """
    Return messages received by our Twilio number (the caller filters out the ones it needs).
    Each item looks like: {"number": "whatsapp:+919876543210", "message": "...", "timestamp_unix": "1700000000"}
    """
    if not _configured():
        return []
    try:
        received = _client().messages.list(to=_our_number(), limit=INBOX_LIMIT)
    except TwilioException as e:
        logger.error(f"Twilio could not read the inbox: {e}")
        return []

    inbox = []
    for incoming in received:
        if incoming.direction != "inbound":
            continue
        sent_at = incoming.date_sent or incoming.date_created
        inbox.append({
            "number": incoming.from_,
            "message": incoming.body or "",
            "timestamp_unix": str(int(sent_at.timestamp())) if sent_at else "0",
        })
    return inbox


def normalize_phone(number: str) -> str:
    """'+91 99885-93333' -> '+919988593333'  (keep digits and a leading +)."""
    number = (number or "").strip()
    digits = re.sub(r"\D", "", number)
    if not digits:
        return ""
    return "+" + digits if number.startswith("+") else digits


def same_phone(a: str, b: str) -> bool:
    """Compare two numbers by their last 10 digits, so '+919988593333' == '9988593333'."""
    a_digits, b_digits = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return bool(a_digits) and bool(b_digits) and a_digits[-10:] == b_digits[-10:]
