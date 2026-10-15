"""
One place to create the LLM, so every part of the app uses the same model.
"""
from langchain_groq import ChatGroq

from app.config import settings


def get_llm(temperature: float = 0.3, use_second_key: bool = False) -> ChatGroq:
    """
    Return a Groq chat model (GPT-OSS 120B by default, see settings.llm_model).

    temperature: 0 = predictable answers, 1 = more creative answers.
    use_second_key: use GROQ_API_KEY_2 if it is set (falls back to GROQ_API_KEY).
    """
    api_key = (settings.groq_api_key_2 if use_second_key else "") or settings.groq_api_key
    return ChatGroq(model=settings.llm_model, temperature=temperature, api_key=api_key)
