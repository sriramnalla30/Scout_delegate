"""
Send and receive SMS.

No SMS provider is connected right now (SMSMobileAPI was removed; Twilio is planned).
The functions below keep the interface the rest of the app uses, so the Twilio
implementation only has to fill in these two bodies.
"""
import logging
import re

logger = logging.getLogger(__name__)


def send_sms(to: str, message: str) -> bool:
    """Send one SMS. Returns True if the provider accepted it."""
    # TODO(twilio): send with the Twilio client
    logger.error("No SMS provider is configured - cannot send SMS")
    return False


def get_inbox() -> list[dict]:
    """
    Return received SMS (the caller filters out the ones it needs).
    Each item must look like: {"number": "919876543210", "message": "...", "timestamp_unix": "1700000000"}
    """
    # TODO(twilio): fetch inbound messages and map them to the shape above
    return []


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
