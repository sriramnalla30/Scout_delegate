# Code review: what was planned, what was built, and what was cleaned up

## 1. What the project was trying to build

The plan (see `docs/images/data_flow.png`, titled "YOUR DESIGN FLOW"):

1. The user types a natural-language request ("gyms in Koramangala, best reviewed")
2. An LLM extracts city / area / place type
3. SerpStack finds ~10 places with phone numbers
4. The agent shows the best one plus all results, each with a **Negotiate** button
5. On Negotiate: use reviews as leverage and draft an opening message
6. **Human-in-the-loop**: show the plan, let the user approve or edit
7. Send the message, wait for the reply, let the agent analyse it, **loop back** to drafting
8. End with "deal reached" or "refused"

The README also claimed a "Reflexion" pattern (self-critique loops, max 3 iterations), SQLite persistence, and token streaming.

## 2. What had actually been built

| Planned | Reality before the cleanup |
| --- | --- |
| Steps 1–4: parse → search → rank → reviews → winner | ✅ Worked |
| First SMS with approval (steps 5–6) | ✅ Worked through the LangGraph interrupt |
| Reply → analyse → draft loop (step 7) | ⚠️ **Not in the graph.** The graph's reply branch checked for `role == "shop"`, but replies were saved as `role == "shopkeeper"`, so that branch could never run. A second, separate implementation lived in `main.py` (`generate_negotiation_response` + `/agent/send-chat`) and bypassed the graph |
| End with deal / refused | ⚠️ The LLM's `should_accept` / `should_reject` were computed and then ignored |
| Reflexion / self-critique | ❌ Never existed. The node called "revisor" was a router whose path decision was hard-coded to `path_a` |
| SQLite persistence | ❌ In-memory `MemorySaver` only (SQLite packages installed but unused) |
| Tavily reviews, shop simulation, price comparison, Twilio | ❌ Written but never called (and partly unable to run) |

## 3. Bugs found and fixed

| # | Problem | Effect | Fix |
| --- | --- | --- | --- |
| 1 | Reply filter compared against `twilio_target_number`, which was always `""` | `"" in number` is always true, so **any** SMS from any numeric sender (friends, OTPs) was treated as the shop's reply | Replies must come from the number we texted (`same_phone()`, last 10 digits) |
| 2 | Negotiation loop: `"shop"` vs `"shopkeeper"` role mismatch | The graph's reply analysis never ran | Rebuilt the loop inside the graph (`check_negotiation` → `draft_message` → ⏸ → `send_sms`) |
| 3 | "Show all results" still ran review scraping for **every** place (up to 10 WebScraping.AI calls), then threw the list away for one winner | Wasted API quota, wrong output | Conditional edge `after_ranking()` ends the run for lists |
| 4 | Keyword `"all"` matched inside words ("m**all**", "sm**all**", "h**all**") | "cafe in a mall" forced list mode | Whole-word regex |
| 5 | Sorting with `rating = None` next to numbers | `TypeError` crash for some SerpStack responses | Ratings are normalised to `float or None`, and the sort key uses `or 0` |
| 6 | SerpStack failures returned fake places (`{"error": ...}`, "No results found") | Error text could show up as a "place" | `search_places()` returns `[]` and logs the error |
| 7 | Results showed `place.reviews` but the field is `reviews_count` | Review count always "N/A" | Uses `reviews_count` |
| 8 | Place names / SMS texts inserted into the page with `innerHTML` unescaped | A crafted SMS reply or place name could run JavaScript in your page (XSS) | `escapeHtml()` everywhere |
| 9 | The `/agent/start` route was defined twice | The second (`quick_start_agent`) was unreachable | Removed |
| 10 | LLM calls used the blocking `.invoke()` inside `async` functions | The whole server froze during every LLM call | `await ...ainvoke()`; blocking HTTP calls run in `asyncio.to_thread` |
| 11 | Reviews for the top 3 places were fetched one after another (up to 60 s each) | Slow | Fetched in parallel with `asyncio.gather` |
| 12 | `.env.example` used `SERPSTACK_API_KEY`, the code expects `SERP_API_KEY` | Fresh setup failed at startup | Fixed `.env.example` |
| 13 | Removing unused settings would crash on extra keys in `.env` | – | `extra="ignore"` in `Settings` |
| 14 | Frontend hard-coded `http://localhost:8000` | Broke on any deployment | Relative URLs; the server now serves the page itself at `/` |
| 15 | Google search URL for scraping wasn't URL-encoded | Place names with `&` or `#` broke the scrape | `quote_plus()` |
| 16 | LangSmith settings in `.env` had no effect | `pydantic-settings` reads `.env` into Python but doesn't set real environment variables, which is what LangSmith looks at | Unused settings removed (see note below) |
| 17 | Terminate showed the literal text `&#x274C;` in the chat header | Cosmetic | Fixed |
| 18 | `requirements.txt` listed 6 unused packages and an old `smsmobileapi` version | Slower installs | Trimmed; `smsmobileapi==2.0.1` matches what is installed |

## 4. What was deleted, and why

| Deleted | Why |
| --- | --- |
| `app/services/twilio_service.py` | Never imported; `twilio` wasn't even in requirements |
| `app/messaging/` (base, service factory, provider) | A "provider factory" with exactly one provider. Replaced by `app/tools/sms.py` (two functions) |
| `app/models.py` (11 models) | 9 of them were never used; the useful ones moved to `app/schemas.py` |
| `tools.py`: `search_reviews`, `get_tavily_tool` | Tavily was never called |
| `tools.py`: `contact_shop_simulation` | Never called; imported `langchain_google_genai` (not installed) and `settings.google_api_key` (doesn't exist), so it would have crashed |
| `tools.py`: `compare_prices` | Never called |
| `nodes.py`: `shop_simulation_node` | Not in the graph ("removed - we use real SMS") |
| `graph.py`: `visualize_graph()` | Showed a workflow (Router → Fetch → HITL → Analyze → Negotiate → Refine) that didn't match the real graph |
| `graph.py`: `stream_agent_response()` | Never called |
| `main.py`: `/search`, `/agent/activate`, `/agent/chat`, `/agent/history`, `/agent/reset`, 2nd `/agent/start` | Not used by the frontend; `/agent/reset` did nothing |
| `main.py`: stream branches for `analyze_node`, `negotiate_node`, `reflexion_node` | Those nodes never existed |
| `main.py`: `generate_negotiation_response`, `/agent/send-chat`, `/agent/send-manual` | Replaced by the graph loop + one `/agent/send-message` |
| State keys `tavily_reviews`, `initial_analysis`, `shop_responses`, `refined_analysis`, `iteration`, `shop_persona`, `budget`, `human_notes`, `human_approved`, `negotiation_active`, `current_step`, `thread_id`, `is_complete`, `messages` | Never read, or only used for logging |
| Config: Tavily, Twilio (5 keys), `database_path`, `max_iterations`, `max_tokens`, `llm_temperature`, `recursion_limit`, `environment`, `messaging_provider`, `default_sender_number`, LangSmith keys | Unused |
| `netlify.toml` | Pointed to `index.html`, which didn't exist; the page called `localhost` anyway |
| `DEPLOYMENT.md` | Described another folder (`Search_Agent`) and a non-existent `frontend/index.html` flow; `render.yaml` + README cover deployment now |
| Frontend: commented-out "matrix rain", unused helpers (`showThinkingState`, `showStreamingText`), dead `router_decision` / `no_target_phone` handlers, unused CSS rules, WebGL mouse tracking that the shader never used | Dead code |

## 5. New structure

```
BEFORE                                   AFTER
app/main.py         1468 lines           app/main.py              327 lines (8 endpoints)
app/agent/nodes.py   790 lines           app/agent/search_nodes.py, negotiation_nodes.py, query_parser.py
app/agent/tools.py   464 lines           app/tools/serpstack.py, reviews.py, sms.py
app/models.py, messaging/, services/     app/schemas.py, app/llm.py
frontend/dashboard.html 1436 lines       frontend/index.html + app.js + style.css + background.js
```

## 6. Things to know (not changed)

- **LangSmith tracing:** your `.env` turns it on, but it never worked (bug 16). To enable it, add `from dotenv import load_dotenv; load_dotenv()` at the top of `app/config.py` and `pip install python-dotenv`. Note this sends your prompts and results to LangSmith.
- **Your `.env` contains a commented-out spare API key and two personal phone numbers.** It's git-ignored, so it isn't committed, but don't share the file.
- **Installed package versions don't match the old `requirements.txt`** (e.g. `langchain-core` 1.4.1 installed vs 0.3.15 pinned). The app was tested with what is installed. A fresh `pip install -r requirements.txt` uses the pinned versions.
- `data/checkpoints.db` is a leftover file; nothing uses it.
