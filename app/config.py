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
LLM_MODEL = _get('LLM_MODEL', 'openai/gpt-oss-120b')
LLM_REASONING_EFFORT = _get('LLM_REASONING_EFFORT', 'low')
