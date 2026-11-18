from functools import lru_cache

from app.config import ROOT


@lru_cache
def load_prompt(name: str) -> str:
    """System prompts live in prompts/<name>.md so they can be edited without touching code."""
    return (ROOT / 'prompts' / f'{name}.md').read_text(encoding='utf-8').strip()
