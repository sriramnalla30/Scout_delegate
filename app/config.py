import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# dotenv_values reads the file without exporting it, so unrelated keys in .env
# (LangSmith, Langfuse, ...) are not switched on by this app.
_env = dotenv_values(ROOT / '.env')


def _get(name, default=''):
    return os.environ.get(name) or _env.get(name) or default


# Everything the app remembers between runs (agent memory + chat history) lives in this one folder.
DATA_DIR = Path(_get('SCOUT_DATA_DIR') or ROOT / 'data')

GROQ_API_KEY = _get('GROQ_API_KEY')
LANGFUSE_PUBLIC_KEY = _get('LANGFUSE_PUBLIC_KEY')
LANGFUSE_SECRET_KEY = _get('LANGFUSE_SECRET_KEY')
LANGFUSE_BASE_URL = _get('LANGFUSE_BASE_URL') or _get('LANGFUSE_HOST') or 'https://cloud.langfuse.com'
# Tracing is on when both keys exist; set LANGFUSE_TRACING=false to switch it off without removing them.
LANGFUSE_ENABLED = bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY) and _get('LANGFUSE_TRACING', 'true').lower() != 'false'
LLM_MODEL = _get('LLM_MODEL', 'openai/gpt-oss-120b')
LLM_REASONING_EFFORT = _get('LLM_REASONING_EFFORT', 'low')

# The Google Maps search tool: the gosom google-maps-scraper binary ("gmaps"), run in fast mode.
GMAPS_BIN = Path(_get('GMAPS_BIN') or ROOT / 'bin' / ('gmaps.exe' if os.name == 'nt' else 'gmaps'))
GMAPS_TIMEOUT = int(_get('GMAPS_TIMEOUT', '90'))              # seconds one scrape may take
GMAPS_PARALLEL = int(_get('GMAPS_PARALLEL', '1'))             # scrapes that may run at once (be gentle with Google)
GMAPS_QUEUE_WAIT = int(_get('GMAPS_QUEUE_WAIT', '120'))       # seconds a search waits for a free slot
# The same search (query, pin, radius, language) inside this many minutes reuses the stored result. 0 switches it off.
SEARCH_CACHE_MINUTES = int(_get('SEARCH_CACHE_MINUTES', '30'))
