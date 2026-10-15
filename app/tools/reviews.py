"""
Read real Google reviews for a place.

How it works:
  1. WebScraping.AI opens a Google search page "reviews for <place>" in a real browser
     and gives us the HTML.
  2. BeautifulSoup turns the HTML into plain text.
  3. The LLM reads that (messy) text and picks out the actual reviews.
"""
import json
import logging
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from app.config import settings
from app.llm import get_llm
from app.prompts import invoke_prompt

logger = logging.getLogger(__name__)

WEBSCRAPING_AI_URL = "https://api.webscraping.ai/html"
MAX_PAGE_CHARS = 20_000  # keep the LLM prompt small enough


def fetch_reviews(place_name: str, address: str = "", limit: int = 3) -> list[str]:
    """Return up to `limit` review texts for a place (or one error text)."""
    if not settings.webscraping_ai_api_key:
        return ["Reviews skipped: WEBSCRAPING_AI_API_KEY is not set."]

    place = f"{place_name} {address}".strip()

    # Step 1: download the Google results page
    google_url = f"https://www.google.com/search?q={quote_plus('reviews for ' + place)}"
    try:
        response = requests.get(
            WEBSCRAPING_AI_URL,
            params={
                "api_key": settings.webscraping_ai_api_key,
                "url": google_url,
                "device": "desktop",
                "proxy": "residential",
                "js": "true",  # Google needs JavaScript to render reviews
            },
            timeout=60,
        )
    except requests.RequestException as e:
        logger.error(f"WebScraping.AI request failed for '{place_name}': {e}")
        return [f"Could not fetch reviews: {e}"]

    if response.status_code != 200:
        logger.error(f"WebScraping.AI error {response.status_code}: {response.text[:200]}")
        return [f"Could not fetch reviews (HTTP {response.status_code})."]

    # Step 2: HTML -> plain text
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    page_text = soup.get_text(separator=" ", strip=True)[:MAX_PAGE_CHARS]

    # Step 3: let the LLM find the reviews in the text (prompt "review-extractor" in Langfuse)
    try:
        answer = invoke_prompt(get_llm(temperature=0), "review-extractor", place=place, limit=limit, page_text=page_text).content
    except Exception as e:
        logger.error(f"LLM review extraction failed for '{place_name}': {e}")
        return [f"Could not read reviews: {e}"]

    return _parse_json_list(answer)[:limit]


def _parse_json_list(text: str) -> list[str]:
    """The LLM sometimes wraps JSON in ```json ... ``` - strip that, then parse."""
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if isinstance(data, list):
        return [str(item) for item in data]
    return [str(data)]
