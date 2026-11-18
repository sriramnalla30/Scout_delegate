# Scout: a coordinator agent for place search

A small LangGraph system that shows how one coordinator agent keeps control of a flow using tools and a scout sub-agent, running on Groq.

```
you ──> Coordinator (system prompt in prompts/coordinator.md)
          ├── ask_user(question)     tool · human-in-the-loop: pauses the graph until you answer
          └── scout_agent(request)   sub-agent used as a tool (prompts/scout.md)
                ├── geocode(place)       tool · searches Google Maps in a headless browser, no key
                └── search_places(...)   tool · the gosom Google Maps scraper (gmaps binary, fast mode)
```

The coordinator decides what to call next from its system prompt:

1. It needs three details: **business type, area, city**.
2. If one is missing, it calls `ask_user` and the run **pauses** (LangGraph `interrupt`). Your answer resumes the same run.
3. When all three are known it calls `scout_agent`, which geocodes the area, then runs the Google Maps search (`search_places`) with that pin.
4. The full results are stored by search id and never pass through a model. The scout agent reports only the count and the best few; the coordinator tells the person in two sentences, and the chat page loads the stored results and shows every place with all its data (see "The search step" below).

## Run

```powershell
cd D:\AI_Projects\Search_Agent_2\Place_Discover_Negotiating-AGENT
.\.venv\Scripts\python.exe -m app.server      # web UI at http://127.0.0.1:8000
.\.venv\Scripts\python.exe cli.py             # or talk to it in the terminal
```

The app reads only the keys it needs from `.env` (`GROQ_API_KEY` and the three `LANGFUSE_*` values) and does not export the rest of the file, so other tracing keys stored there (LangSmith) are not switched on by it. Override the model with `LLM_MODEL` (default `openai/gpt-oss-120b`) and `LLM_REASONING_EFFORT` (default `low`).

## Langfuse

With `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` set (see `.env.example`), every conversation turn is traced to Langfuse:

- **Traces:** one trace per turn, named like "Turn 2 (answer): Indiranagar", with the coordinator's model calls, the `scout_agent` tool, the scout sub-agent nested inside it, and its `geocode` calls, each with model and token usage. Turns of one chat share a Langfuse session (the chat's thread id). A retried model call shows up as an extra generation.
- **Prompts:** at startup `prompts/coordinator.md` and `prompts/scout.md` are saved as `coordinator` and `scout-agent`, label `production`. A new version is created only when the file text changed. The files stay the source of truth. Each trace records the prompt versions in its metadata.
- **Off switch:** `LANGFUSE_TRACING=false`. If Langfuse is unreachable the app runs the same and just skips tracing.

## Where things are

| File | What it is |
|---|---|
| `prompts/coordinator.md`, `prompts/scout.md` | The system prompts. Edit these to change behaviour; no code change needed. |
| `app/coordinator.py` | The coordinator agent, the pause/resume logic and the event stream. |
| `app/tools.py` | `ask_user`, `geocode`, `search_places`, `scout_agent`. |
| `app/scout_agent.py` | The scout sub-agent, its `Report` output schema, and `complete()`, the code check on a ready report. |
| `app/access.py` | Who may call the API: other sites, one chat history per browser, a message limit. |
| `app/gmaps.py` | Runs the scraper binary (the only file that knows about it). |
| `app/places.py` | The place model: scraper record to place, scores, opening hours, phone kinds, summary, data coverage. Pure functions. |
| `app/searches.py` | Stored searches (one JSON file each in `data/searches/`), reuse of recent identical searches, and `perform()`. |
| `static/places.js`, `static/places.css` | The results view: summary, picks, sort and filters, one card per place, CSV and JSON download. |
| `static/config.js` | Where the API is (empty = the same site). Written at build time when the page is hosted elsewhere. |
| `Dockerfile`, `render.yaml`, `netlify.toml`, `scripts/write-config.mjs` | Deployment: the API on Render, the page on Netlify. See `docs/DEPLOYMENT.md`. |
| `app/server.py`, `static/` | FastAPI server (server-sent events) and the chat UI. |
| `tests/test_offline.py`, `tests/test_search.py`, `tests/test_access.py`, `tests/test_llm.py` | Offline unit tests (no model, no network, no scraper binary). |
| `tests/live_scenarios.py` | Five scenarios against the real model. |

## The search step

```
geocode -> pin -> search_places(query, lat, lng, radius_m, lang) -> gmaps binary (fast mode, up to 20 places)
                       |
                       +-> app/places.py builds the stored document -> data/searches/<search id>.json
                       +-> the model gets only {search_id, count, top 3}
chat page: GET /api/searches/<search id>  ->  summary, picks, sort and filters, cards, CSV / JSON
```

- **Ready means searched.** `complete()` in `app/scout_agent.py` checks the model's report against what the tools did: the pin is the one `geocode` returned (not coordinates the model wrote), and a search really ran for exactly these arguments (when the model skipped it, it runs there). Results found: `ready`. Nothing found: `needs_input` (a wider area is asked for). The scraper broke: `failed`, with the reason.
- **One shape for every screen.** `app/places.py` turns the scraper's record into a place (name, categories, rating, reviews, weighted score, distance from the pin, phone with kind and `+91` form, hours as readable minutes, links) and keeps the scraper's own record untouched in `place["raw"]`. The page lists every non-empty raw field, so a field the scraper adds later shows up without a code change. A map or charts later read `GET /api/searches/<id>` the same way.
- **Live facts are computed when read, not stored.** Open or closed now (in the place's own time zone) is added by `places.view()` on every read, so a chat opened tomorrow does not say "open now" from yesterday. A place whose hours cannot be read gets "Hours not listed", never a guess.
- **Weighted score.** A Bayesian average (a rating counts for more the more people gave it), so a 5.0 from 3 people does not beat a 4.9 from 1,000.
- **Data coverage.** For every field Google returned: how many places have it. Fast mode gives about 20 fields per place; photos, price, review text and emails come back empty (the page says so).
- **Limits.** One search at a time by default (`GMAPS_PARALLEL`), 90 s timeout, results stored per search id (old ones are not deleted yet). Fast mode only reads list searches ("gyms in Koramangala Bangalore") and needs the pin.
- **Binary.** `bin/gmaps.exe` (git-ignored). Set `GMAPS_BIN` to use another path.

## Deploying

The API runs on Render and the page on Netlify: `docs/DEPLOYMENT.md` has the layout, the settings and the steps.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v   # offline
.\.venv\Scripts\python.exe tests\live_scenarios.py            # calls Groq
```

## Adding the next agents

- **Reviews, negotiation:** either wrap each as another sub-agent tool (the coordinator keeps control), or hand off with a `Command(goto=...)` when the conversation should change role, prompt and tools.

## Things to know

- Groq rejects its JSON mode when combined with other tools, so the scout agent returns its structured result through `ToolStrategy` (a tool call).
- An area Google Maps does not recognise makes `geocode` fail. The scout agent then asks for another spelling or a landmark instead of inventing a location.
- `geocode` opens Google Maps in one shared headless Chromium (about 8 to 10 seconds and roughly 1 GB of memory per lookup). Run `playwright install chromium` once after installing the requirements. `SCOUT_MAPS_PARALLEL` (default 4) caps how many lookups run at once.
- Conversation state is saved on disk (`SqliteSaver` in `data/`), so open conversations survive a restart.
