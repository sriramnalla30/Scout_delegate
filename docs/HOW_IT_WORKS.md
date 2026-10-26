# How the project works, end to end

This guide explains every part of the code, in the order things happen when someone uses the app. Keep the code open next to it. Each section names the file and function to look at.

**Contents**

1. [The big picture](#1-the-big-picture)
2. [Five concepts you need first](#2-five-concepts-you-need-first)
3. [Journey part 1: searching](#3-journey-part-1-searching)
4. [Journey part 2: negotiating by message](#4-journey-part-2-negotiating-by-message)
5. [The graph in one picture](#5-the-graph-in-one-picture)
6. [The state: what the agent remembers](#6-the-state-what-the-agent-remembers)
7. [File-by-file reference](#7-file-by-file-reference)
8. [API reference](#8-api-reference)
9. [Limits you should know about](#9-limits-you-should-know-about)
10. [Exercises to learn the code](#10-exercises-to-learn-the-code)

---

## 1. The big picture

There are three layers:

```
 BROWSER (frontend/)                SERVER (app/)                         OUTSIDE SERVICES
 ┌───────────────────┐   HTTP    ┌──────────────────────────┐          ┌──────────────────┐
 │ index.html        │ ───────►  │ main.py  (API endpoints) │ ───────► │ Groq (LLM)       │
 │ app.js            │ ◄───────  │    │                     │          │ SerpStack        │
 │ style.css         │  JSON/SSE │    ▼                     │          │ WebScraping.AI   │
 └───────────────────┘           │ agent/   (LangGraph)     │          │ WhatsApp (Twilio)│
                                 │ tools/   (API wrappers)  │          └──────────────────┘
                                 └──────────────────────────┘
```

- **frontend/** is what the user sees. It has no AI in it. It only calls the server and shows the answers.
- **app/main.py** receives the browser's requests and decides what to run.
- **app/agent/** is the "brain": a LangGraph flowchart of steps (nodes). Some steps ask the LLM to decide something.
- **app/tools/** are plain functions that call the outside services.

---

## 2. Five concepts you need first

### 2.1 FastAPI endpoint
A Python function that runs when the browser calls a URL:

```python
@app.post("/agent/start")              # "when someone POSTs to /agent/start ..."
async def start(request: StartRequest): # "... check the body looks like StartRequest ..."
    ...
    return {"thread_id": thread_id}     # "... and send this dict back as JSON"
```

`StartRequest` lives in `app/schemas.py`. If the browser sends a wrong body, FastAPI answers with error 422 automatically.

### 2.2 LLM with structured output
Normally an LLM returns free text. `with_structured_output(SomeClass)` makes it return a filled-in Python object instead:

```python
class ParsedQuery(BaseModel):
    city: str
    place_type: str

llm = get_llm().with_structured_output(ParsedQuery)
parsed = await llm.ainvoke("best gym in Bangalore")
parsed.city        # "Bangalore"
parsed.place_type  # "gym"
```

Every LLM decision in this project works this way. Search for `with_structured_output` to find all of them (5 places).

### 2.3 LangGraph: state, nodes, edges
A **graph** is a flowchart.

- **State** (`app/agent/state.py`) is a dictionary that travels through the flowchart.
- **Nodes** are the boxes. Each one is a function `node(state) -> dict of changes`. It returns **only the keys it changes**, and LangGraph merges them into the state.
- **Edges** are the arrows. A normal edge always goes to the same next node. A **conditional edge** calls a small function that returns where to go next.

```python
graph.add_edge("fetch_reviews", "pick_winner")              # always
graph.add_conditional_edges("rank_places", after_ranking,   # after_ranking() returns
                            {"fetch_reviews": "fetch_reviews", "done": END})  # one of these keys
```

### 2.4 Checkpointer and thread_id (memory)
`graph.compile(checkpointer=MemorySaver())` makes LangGraph **save the state after every node**. Each saved conversation is identified by a `thread_id`, a random id created in `/agent/start`. The browser keeps that id and sends it with every request, so the server can find the right conversation again.

Three ways the code uses the saved state:

| Code | Meaning |
| --- | --- |
| `await agent.aget_state(config)` | read the saved state (`.values`) and which node runs next (`.next`) |
| `await agent.aupdate_state(config, {...})` | change some keys of the saved state without running anything |
| `await agent.ainvoke(input, config)` | run the graph. `input=None` = continue where it paused; `input={...}` = start a new run from START with these changes |

### 2.5 Interrupt = human-in-the-loop
```python
graph.compile(..., interrupt_before=["send_message"])
```
The graph **stops right before** `send_message`. The state is saved and `.next` becomes `("send_message",)`. Nothing happens until someone calls `agent.ainvoke(None, config)`. That call is in `/agent/approve`, which runs when you click Approve. This is how the agent waits for your approval before every message.

### Bonus: Server-Sent Events (SSE)
`/agent/stream` keeps the HTTP connection open and sends lines like `data: {...json...}` as each step finishes. In the browser, `new EventSource(url)` receives them one by one (`streamSearch()` in `app.js`). That's how the log fills in live.

---

## 3. Journey part 1: searching

### Step 1: the user clicks "Start Scouting"
**`frontend/app.js` → `startSearch()`**
Shows the log panel and calls `POST /agent/start` with `{"user_query": "best gym in Koramangala, Bangalore"}`.

### Step 2: understand the sentence, search Google, save the state
**`app/main.py` → `start()`**

1. `parse_user_query()` (`app/agent/query_parser.py`) asks the LLM for `ParsedQuery` → city, area, place_type, intent.
2. `build_search_text()` builds `"gym in Koramangala Bangalore"`.
3. `search_places()` (`app/tools/serpstack.py`) calls SerpStack. Google results come in three possible blocks: `local_results` (best), `related_places`, `organic_results`. The function uses the best one available and turns every place into the same shape:
   ```python
   {"name": "...", "address": "...", "phone": "...", "rating": 4.6, "reviews_count": 812}
   ```
   It runs inside `asyncio.to_thread(...)` because `requests` is "blocking" code. In a thread, the server can keep serving other requests meanwhile.
4. A new `thread_id` is created, and `agent.aupdate_state(...)` saves the first state (`route: "search"`, `serp_results`, ...). **The graph does not run yet.**
5. Returns `thread_id`, the parsed fields, and the number of places found.

### Step 3: run the graph and watch it live
**`app.js` → `streamSearch()`** opens `GET /agent/stream?thread_id=...`.

**`app/main.py` → `stream()`** runs `agent.astream(None, config, stream_mode="updates")`. `None` means "run from the saved state". After each node finishes, it gets `{node_name: what_the_node_returned}`. `describe_step()` turns that into a small event for the browser. At the end it sends a `complete` event with the results.

Inside the graph (`app/agent/graph.py`):

**`choose_path()`**: the edge out of START reads `state["route"]`. It's `"search"`, so the next node is `rank_places`.

**`rank_places_node()`** (`search_nodes.py`)
- Sorts the places by rating, then number of reviews.
- Decides `show_all`: does the user want a **list** or **one winner**?
  - Whole words like "list", "all", or "top 5" → list (no LLM needed)
  - Otherwise the LLM decides (`DisplayDecision`)
- Returns `show_all` and `recommendations` (all places for a list, the top 3 for a winner).

**`after_ranking()`**: if it's a list, the work is done → END. Otherwise → `fetch_reviews`.

**`fetch_reviews_node()`**
Calls `fetch_reviews()` (`app/tools/reviews.py`) for all 3 candidates **at the same time** (`asyncio.gather`). For each place:
1. WebScraping.AI downloads the Google page "reviews for <place>" (a real browser, so Google's JavaScript runs)
2. BeautifulSoup strips the HTML down to plain text
3. The LLM picks the actual reviews out of that messy text and returns a JSON list

Returns `reviews = {place name: [review, ...]}`.

**`pick_winner_node()`**
Sends all candidates and their reviews to the LLM (`WinnerSelection`: winner name, explanation, pros, cons). `_find_place()` matches the name back to the place dictionary. The LLM sometimes changes a name slightly, so it also tries a partial match. Returns `recommendations = [winner]` and a readable `winner_summary`.

### Step 4: show the results
**`app.js` → `handleStreamEvent()`** writes each step into the log. On `complete` → **`showResults()`** draws the winner card plus all other places, each with Maps, Call, and **Negotiate** buttons. `placeCard()` builds one card. Every outside text goes through `escapeHtml()`, so a place name can't inject HTML into the page.

---

## 4. Journey part 2: negotiating by message

### Step 5: the user clicks "Negotiate" and fills the form
**`app.js` → `openNegotiationModal()` → `launchNegotiation()`** calls `POST /agent/negotiate/start` with the place name, goal, and target price.

### Step 6: the LLM drafts the first message, then the graph pauses
**`app/main.py` → `negotiate_start()`**
Finds the place in `serp_results` and calls
```python
await agent.ainvoke({"route": "negotiate", "recommendations": [place], "negotiation_history": [], ...}, config)
```
A **new run** starts at START. This time `choose_path()` sees `route == "negotiate"`:

- **`check_negotiation_node()`** (`negotiation_nodes.py`): the history is empty → status `continue`
- **`after_check()`**: `continue` → `draft_message`
- **`draft_message_node()`**: the LLM (`DraftedMessage`) writes the message using the goal, target price, and the place's reviews (as leverage). Saves `planned_message` and `strategy_reasoning`.
- **⏸ interrupt before `send_message`**: `ainvoke` returns. Nothing has been sent.

The endpoint returns the draft. **`app.js` → `showApprovalPanel()`** shows it in the yellow "sticky note" panel, where you can edit it.

### Step 7: the user approves → the message is sent
**`app.js` → `approveFirstMessage()`** → `POST /agent/approve`
**`app/main.py` → `approve()`**
1. Checks that the graph is really paused at `send_message` (otherwise error 409)
2. If you edited the text: `aupdate_state(config, {"planned_message": edited})`
3. `agent.ainvoke(None, config)`: **continue from the pause** → **`send_message_node()`**:
   - `get_target_phone()` = the shop's phone from Google, or `DEFAULT_TARGET_NUMBER` from `.env`
   - `send_message()` (`app/tools/chat.py`) sends it over WhatsApp through Twilio
   - adds `{"role": "agent", "content": ...}` to `negotiation_history`
   - sets status `waiting_for_reply` (or `send_failed`)
   - → END

The browser opens the chat panel (`openChat()`) and starts polling.

### Step 8: wait for the shop's reply
**`app.js` → `startPolling()`** calls `checkForReply()` every 10 seconds → `GET /agent/check-reply`.

**`app/main.py` → `check_reply()`**
1. If the status isn't `waiting_for_reply` → answer "no reply" and stop.
2. **`find_new_reply()`** reads the Twilio WhatsApp inbox (`get_inbox()`) and keeps only messages that:
   - come **from the shop's number** (`same_phone()` compares the last 10 digits, so `+91 99885 93333` equals `919988593333`, and senders like `AD-ICICIT` never match)
   - arrived **after** the negotiation started
   - are **not already** in the history

   If several match, it takes the oldest one.
3. Adds the reply to the history and **runs the graph again**: `agent.ainvoke({"negotiation_history": history}, config)`.
   - `choose_path()` → still `negotiate` → **`check_negotiation_node()`**. The last message is from the shopkeeper, so the LLM (`ReplyVerdict`) judges it: `continue`, `deal_reached`, or `rejected`
   - `continue` → `draft_message` → ⏸ pause (same as Step 6)
   - `deal_reached` / `rejected` → END
4. Returns the reply, the status, and the new draft (if any).

The browser shows the reply. If a draft is waiting, it shows it with **Send / Skip** buttons (`showChatDraft()`):
- **Send** → `sendChatDraft()` → `POST /agent/approve` → back to Step 7
- **Skip** → `POST /agent/approve` with `approved: false`. Nothing is sent, the status goes back to `waiting_for_reply`, and polling resumes.

So **one graph run = one message turn**, and the loop is: reply → judge → draft → you approve → send → wait.

### Other buttons in the chat
- **Typing your own message** → `sendManualMessage()` → `POST /agent/send-message`. Sends it directly (no LLM, no graph) and adds it to the history.
- **End** → `endNegotiation()` → `POST /agent/terminate` sets status `terminated`, so `check-reply` stops looking.

---

## 5. The graph in one picture

```
START
  │  choose_path(): state["route"]
  ├── "search" ─────► rank_places
  │                      │  after_ranking()
  │                      ├── user wants a list ─────────────► END
  │                      └──► fetch_reviews ──► pick_winner ──► END
  │
  └── "negotiate" ──► check_negotiation
                         │  after_check()
                         ├── deal / rejected / waiting ─────► END
                         └──► draft_message ──⏸──► send_message ──► END
                                   (pause: human approves the message)
```

Who starts each run:

| Run | Started by | Input |
| --- | --- | --- |
| Search | `GET /agent/stream` | `None` (the state was saved by `/agent/start`) |
| First message | `POST /agent/negotiate/start` | `route="negotiate"`, the place, the target price |
| Resume after approval | `POST /agent/approve` | `None` (continue from the pause) |
| Each reply | `GET /agent/check-reply` | the history with the new reply |

---

## 6. The state: what the agent remembers

Defined in `app/agent/state.py`.

| Key | Written by | Read by |
| --- | --- | --- |
| `user_query`, `city`, `place_type`, `user_intent`, `serp_results` | `/agent/start` | search nodes, `/agent/stream` |
| `route` | `/agent/start` (`search`), `/agent/negotiate/start` (`negotiate`) | `choose_path()` |
| `show_all` | `rank_places` | `after_ranking()` |
| `recommendations` | `rank_places`, `pick_winner`, `/agent/negotiate/start` | `fetch_reviews`, `pick_winner`, negotiation nodes |
| `reviews` | `fetch_reviews` | `pick_winner`, `draft_message` |
| `winner_summary` | `pick_winner` | `/agent/stream` |
| `target_price`, `negotiation_start_time` | `/agent/negotiate/start` | negotiation nodes, `find_new_reply()` |
| `negotiation_history` | `send_message`, `/agent/check-reply`, `/agent/send-message` | `check_negotiation`, `draft_message` |
| `negotiation_status` | `check_negotiation`, `send_message`, `/approve` (skip), `/terminate` | `after_check()`, `/agent/check-reply` |
| `planned_message`, `strategy_reasoning` | `draft_message` (`planned_message` can be edited in `/approve`) | `send_message`, API responses |

`negotiation_status` values are listed in `NegotiationStatus` (`negotiation_nodes.py`).

---

## 7. File-by-file reference

| File | What's inside |
| --- | --- |
| `app/config.py` | `Settings`: every key from `.env`, with defaults. `settings` is used everywhere. |
| `app/llm.py` | `get_llm()`: the only place a Groq model is created |
| `app/schemas.py` | Request bodies: `StartRequest`, `NegotiationStartRequest`, `ApprovalRequest`, `ManualMessageRequest`, `ThreadRequest` |
| `app/main.py` | The FastAPI app, all endpoints, `find_new_reply()`, and serving `frontend/` at `/` |
| `app/agent/state.py` | `AgentState` |
| `app/agent/query_parser.py` | `parse_user_query()`, `build_search_text()` |
| `app/agent/search_nodes.py` | `rank_places_node`, `after_ranking`, `fetch_reviews_node`, `pick_winner_node` |
| `app/agent/negotiation_nodes.py` | `NegotiationStatus`, `check_negotiation_node`, `after_check`, `draft_message_node`, `send_message_node`, phone helpers |
| `app/agent/graph.py` | `build_agent()` wires everything; `agent` is the compiled graph; `thread_config()` |
| `app/tools/serpstack.py` | `search_places()` and the three Google-result parsers |
| `app/tools/reviews.py` | `fetch_reviews()`: WebScraping.AI → BeautifulSoup → LLM |
| `app/tools/chat.py` | `send_message()`, `get_inbox()`, `normalize_phone()`, `same_phone()` |
| `frontend/index.html` | Static layout: search card, log, approval panel, results, chat, modal |
| `frontend/app.js` | All page logic, in journey order |
| `frontend/style.css` | Styles |
| `frontend/background.js` | WebGL light-rays decoration (safe to delete) |

---

## 8. API reference

| Method & path | Body / query | Returns |
| --- | --- | --- |
| `GET /health` | – | `{"status": "ok"}` |
| `POST /agent/start` | `{user_query}` | `{thread_id, parsed, search_text, places_found}` |
| `GET /agent/stream` | `?thread_id=` | SSE events: `step` (rank_places / fetch_reviews / pick_winner), `complete`, `error` |
| `POST /agent/negotiate/start` | `{thread_id, place_name, target_price?, goal?}` | `{place, phone, planned_message, reasoning}` |
| `POST /agent/approve` | `{thread_id, approved, edited_message?}` | `{status, sent_message, place_name, phone}` or `{status: "skipped"}` |
| `GET /agent/check-reply` | `?thread_id=` | `{has_reply, status, reply_text?, planned_message?, reasoning?}` |
| `POST /agent/send-message` | `{thread_id, message}` | `{success}` |
| `POST /agent/terminate` | `{thread_id}` | `{status: "terminated"}` |

FastAPI also generates interactive docs at **http://localhost:8000/docs**, where you can call every endpoint by hand.

---

## 9. Limits you should know about

- **Memory is in RAM** (`MemorySaver`). Restarting the server forgets every conversation. To keep them, switch to `SqliteSaver` from the `langgraph-checkpoint-sqlite` package.
- **Phone-number parsing is India-only** (`PHONE_PATTERN` in `serpstack.py`).
- **Scraping Google is fragile.** If Google changes its page, `fetch_reviews` may find nothing. The winner is then picked on rating alone.
- **No login.** Anyone who can open the server can send WhatsApp messages from your Twilio number. Don't expose it to the internet without adding authentication.
- **Inbox polling.** Replies are found by reading the whole inbox every 10 s. Two identical texts with the same timestamp would count as one.

---

## 10. Exercises to learn the code

1. **Follow one request.** Set `LOG_LEVEL=DEBUG` in `.env`, run a search, and match each log line to a function in this guide.
2. **Change a decision.** In `search_nodes.py`, set `TOP_CANDIDATES = 5`. What changes on screen, and how many WebScraping.AI calls does a search now make?
3. **Add a node.** Add a `summarize_reviews` node between `fetch_reviews` and `pick_winner` that only logs how many reviews were found. You will touch `search_nodes.py` (the function) and `graph.py` (`add_node` + edges).
4. **Read the state.** Add a debug endpoint to `main.py`:
   ```python
   @app.get("/agent/state")
   async def debug_state(thread_id: str):
       return await load_state(thread_id)
   ```
   (put it **above** the `app.mount(...)` line). Run a search and open `/agent/state?thread_id=...` after each step to see exactly what is saved.
5. **Make memory survive restarts.** Replace `MemorySaver()` with a SQLite checkpointer.
