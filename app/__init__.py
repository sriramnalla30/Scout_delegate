"""
Place Discover Agent - finds local places, reads their reviews, and negotiates prices by SMS.

    main.py     -> web server (API endpoints)
    config.py   -> settings from .env
    llm.py      -> creates the Groq LLM
    schemas.py  -> shapes of request bodies
    agent/      -> the LangGraph agent
    tools/      -> SerpStack, review scraping, SMS
"""
