# Scout: an orchestrator agent for place search

A small LangGraph system that shows how one main agent keeps control of a flow using tools and a sub-agent, running on Groq.

```
you ──> Scout (orchestrator, system prompt in prompts/orchestrator.md)
          ├── ask_user(question)     tool · human-in-the-loop: pauses the graph until you answer
          └── parser_agent(request)  sub-agent used as a tool (prompts/parser.md)
                └── geocode(place)   tool · OpenStreetMap Nominatim, no key
```

Scout decides what to call next from its system prompt:

1. It needs three details: **business type, area, city**.
2. If one is missing, it calls `ask_user` and the run **pauses** (LangGraph `interrupt`). Your answer resumes the same run.
3. When all three are known it calls `parser_agent`, which geocodes the area and returns a structured `ScraperRequest` (query, lat, lng, radius, language) for the Google Maps scraper.
4. The scraper itself is **not connected** in this demo, so nothing is searched.

## Run

```powershell
cd D:\AI_Projects\Search_Agent_2\Place_Discover_Negotiating-AGENT
.\.venv\Scripts\python.exe -m app.server      # web UI at http://127.0.0.1:8000
.\.venv\Scripts\python.exe cli.py             # or talk to it in the terminal
```

Only `GROQ_API_KEY` is read from `.env`. The app reads that one key without exporting the rest of the file, so tracing keys stored there (LangSmith, Langfuse) are not switched on by it. Override the model with `LLM_MODEL` (default `openai/gpt-oss-120b`) and `LLM_REASONING_EFFORT` (default `low`).

## Where things are

| File | What it is |
|---|---|
| `prompts/orchestrator.md`, `prompts/parser.md` | The system prompts. Edit these to change behaviour; no code change needed. |
| `app/orchestrator.py` | The main agent, the pause/resume logic and the event stream. |
| `app/tools.py` | `ask_user`, `geocode`, `parser_agent`. |
| `app/parser_agent.py` | The sub-agent and its `ScraperRequest` output schema. |
| `app/server.py`, `static/` | FastAPI server (server-sent events) and the chat UI. |
| `tests/test_offline.py` | Offline unit tests (no model, no network). |
| `tests/live_scenarios.py` | Five scenarios against the real model. |

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v   # offline
.\.venv\Scripts\python.exe tests\live_scenarios.py            # calls Groq
```

## Adding the next agents

- **Search:** add a `search_places` tool that runs the scraper with the parser's output.
- **Reviews, negotiation:** either wrap each as another sub-agent tool (the orchestrator keeps control), or hand off with a `Command(goto=...)` when the conversation should change role, prompt and tools.

## Things to know

- Groq rejects its JSON mode when combined with other tools, so the parser returns its structured result through `ToolStrategy` (a tool call).
- A misspelt or unknown area makes geocoding fail. The parser then returns empty coordinates and a `problem` instead of inventing a location.
- Conversation state is held in memory (`InMemorySaver`), so restarting the server forgets open conversations.
