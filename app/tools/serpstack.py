"""
Find places (gyms, cafes, ...) with the SerpStack API, which returns Google search results as JSON.

Every place is returned in the same simple shape:
    {"name": str, "address": str, "phone": str, "rating": float | None, "reviews_count": int}
"""
import logging
import re

import requests

from app.config import settings

logger = logging.getLogger(__name__)

MAX_PLACES = 10

# Indian phone numbers like "+91 99885 93333" or "99885 93333"
PHONE_PATTERN = re.compile(r"(\+91\s*\d{5}\s*\d{5}|\d{5}\s*\d{5})")


def search_places(query: str) -> list[dict]:
    """
    Search Google for `query` (e.g. "gym in Koramangala Bangalore").
    Returns a list of places, or an empty list if anything goes wrong.
    """
    params = {
        "access_key": settings.serp_api_key,
        "query": query,
        "type": "web",
        "num": 20,
        "auto_location": 1,
        "google_domain": "google.co.in",  # Google India gives better local results
        "gl": "in",
        "hl": "en",
    }

    try:
        response = requests.get(settings.serp_api_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"SerpStack request failed: {e}")
        return []

    # SerpStack reports problems (bad key, quota used up) inside the JSON
    if "error" in data or data.get("request", {}).get("success") is False:
        logger.error(f"SerpStack error: {data.get('error')}")
        return []

    # Google shows places in different blocks. We use the best one that exists:
    #   1. local_results  -> the map "pack" (best data)
    #   2. related_places -> places described in one text line
    #   3. organic_results -> normal search results (no rating / phone)
    if data.get("local_results"):
        places = _parse_local_results(data)
    elif data.get("related_places"):
        places = _parse_related_places(data["related_places"])
    else:
        places = _parse_organic_results(data.get("organic_results", []))

    logger.info(f"SerpStack: found {len(places)} places for '{query}'")
    return places


def _parse_local_results(data: dict) -> list[dict]:
    # local_results usually lack phone numbers, but related_places often have them in text.
    # Build {place name -> phone} from related_places and use it to fill the gaps.
    phone_by_name = {}
    for item in data.get("related_places", []):
        phone = _find_phone(item.get("places", ""))
        if phone:
            phone_by_name[item.get("title", "").lower().strip()] = phone

    places = []
    for item in data["local_results"][:MAX_PLACES]:
        name = item.get("title", "Unknown")
        phone = (
            phone_by_name.get(name.lower().strip())
            or _find_phone(item.get("type", ""))
            or _find_phone(item.get("address", ""))
        )
        places.append({
            "name": name,
            "address": item.get("address", ""),
            "phone": phone,
            "rating": _to_float(item.get("rating")),
            "reviews_count": _to_int(item.get("reviews")),
        })
    return places


def _parse_related_places(related_places: list[dict]) -> list[dict]:
    places = []
    for item in related_places[:MAX_PLACES]:
        text = item.get("places", "")

        # Rating looks like "4.8(407)" -> rating 4.8, 407 reviews
        rating_match = re.search(r"(\d+\.\d+)\((\d+)\)", text)
        # Address sits between "business ·" and "Open"/"Closed"
        address_match = re.search(r"business\s*·\s*([^·]+?)(?:Closed|Open)", text)

        places.append({
            "name": item.get("title", "Unknown"),
            "address": address_match.group(1).strip() if address_match else "",
            "phone": _find_phone(text),
            "rating": float(rating_match.group(1)) if rating_match else None,
            "reviews_count": int(rating_match.group(2)) if rating_match else 0,
        })
    return places


def _parse_organic_results(organic_results: list[dict]) -> list[dict]:
    return [
        {
            "name": item.get("title", "Unknown"),
            "address": item.get("snippet", "")[:100],
            "phone": "",
            "rating": None,
            "reviews_count": 0,
        }
        for item in organic_results[:MAX_PLACES]
    ]


def _find_phone(text: str) -> str:
    match = PHONE_PATTERN.search(text or "")
    return match.group(1) if match else ""


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0
