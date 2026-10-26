# Place Discover Agent

An AI agent that **finds local places** (gyms, cafes, PGs, ...), **reads their Google reviews to pick the best one**, and can **negotiate the price with the shop by WhatsApp**. Every message waits for your approval before it is sent.

Built with **FastAPI** (web server), **LangGraph** (agent workflow), **Groq / GPT-OSS 120B** (LLM), **SerpStack** (Google search), **WebScraping.AI** (reviews). Chat with shops runs over **WhatsApp via Twilio**.

> 📖 New to the code? Read **[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)**. It walks through the whole project file by file.
> 🔎 What was cleaned up and why: **[docs/CODE_REVIEW.md](docs/CODE_REVIEW.md)**.

---

## What it does

1. You type: _"Best gym in Koramangala, Bangalore"_
2. The LLM understands it → place type `gym`, area `Koramangala`, city `Bangalore`
3. SerpStack searches Google → ~10 places with rating, address, phone
4. The agent ranks them by rating, reads reviews of the top 3, and the LLM picks a winner
5. You click **Negotiate** on any place and enter your goal / target price
6. The LLM drafts a message → **you approve or edit it** → it is sent over WhatsApp (Twilio)
7. The page checks for the shop's reply every 10 s → the LLM judges the reply and drafts the next message → you approve again ... until a deal, a refusal, or you press **End**

---

## Setup

You need **Python 3.10+** and these API keys (all have free tiers):

| Key | Where | Needed for |
| --- | --- | --- |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | everything (LLM) |
| `SERP_API_KEY` | [serpstack.com](https://serpstack.com) | searching places |
| `WEBSCRAPING_AI_API_KEY` | [webscraping.ai](https://webscraping.ai) | reading reviews (optional) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | [cloud.langfuse.com](https://cloud.langfuse.com) → Settings → API keys | everything: all prompts live there (+ tracing) |

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

# 2. Install packages
pip install -r requirements.txt

# 3. Add your keys
copy .env.example .env          # Windows   (cp on macOS / Linux)
#    then open .env and fill it in

# 4. Upload the prompts to your Langfuse project (once)
python scripts/sync_prompts.py push

# 5. Start
python -m uvicorn app.main:app --reload
```

Open **http://localhost:8000**. On Windows you can also double-click `start_server.bat`.

---

## Project map

```
app/
├── main.py                 web server: all API endpoints + serves the web page
├── config.py               settings read from .env
├── llm.py                  creates the Groq LLM
├── prompts.py              loads every LLM prompt from Langfuse
├── telemetry.py            tracing to Langfuse (OpenTelemetry)
├── schemas.py              shapes of request bodies
├── agent/                  the LangGraph agent
│   ├── state.py            what the agent remembers
│   ├── query_parser.py     sentence -> city / place type
│   ├── search_nodes.py     rank_places, fetch_reviews, pick_winner
│   ├── negotiation_nodes.py check_negotiation, draft_message, send_message
│   └── graph.py            connects the nodes into a flowchart
└── tools/                  talking to outside services
    ├── serpstack.py        Google search
    ├── reviews.py          review scraping
    └── chat.py              send / receive message
frontend/
├── index.html              page layout
├── app.js                  page logic (calls the API)
├── style.css               looks
└── background.js           decorative animation (optional)
prompts/                    saved copies of the Langfuse prompts (not read by the app)
scripts/sync_prompts.py     pull / push prompts between Langfuse and prompts/
docs/                       explanations and diagrams
```

## Deploying

`render.yaml` deploys the API and the web page together to [Render](https://render.com) as one service. Add your keys as environment variables there.

Note: conversation state is kept in memory (`MemorySaver`). It is lost when the server restarts, and it doesn't work with more than one server worker.

## Prompts and observability (Langfuse)

[Langfuse](https://langfuse.com) (free Hobby plan) is used for two things:

**1. Prompts.** Every LLM prompt lives in **Langfuse → Prompts**; none is written in the code.
`app/prompts.py` loads the version labelled `production` (kept in memory for 60 s), so after you edit
a prompt in Langfuse the app uses it within a minute, without a redeploy. If a prompt is missing,
the app says so at startup.
`prompts/` holds a readable saved copy of each prompt (the app never reads it):
`python scripts/sync_prompts.py pull` refreshes it from Langfuse, `push` uploads it (first setup).

**2. Tracing.** Every request, LangGraph step, Groq call and outgoing HTTP call is recorded with
OpenTelemetry (`app/telemetry.py`) and sent to **Langfuse → Tracing**, with prompts, answers,
tokens, cost, and the prompt version each LLM call used.

- **Sessions:** each browser tab gets one session id, shown top-right on the page (click to copy). All traces of that visit are grouped under **Langfuse → Sessions**.
- **Privacy:** API keys, message recipients and message text are removed from recorded URLs. Prompts and answers (including the message negotiation text) are stored in Langfuse.
- **On your laptop:** set `LANGFUSE_TRACING=false` to send no traces (prompts still load from Langfuse).

Setup: create a project at cloud.langfuse.com → **Settings → API keys**, and set `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (EU `https://cloud.langfuse.com`, US `https://us.cloud.langfuse.com`)
in `.env` and on Render. Then upload the prompts once: `python scripts/sync_prompts.py push`.

For an animated, narrated explanation, open `docs/observability/how-it-works.html` in a browser.

### Save traces to Google Drive (optional, for testing from your laptop)

With `DRIVE_TRACES=true` the app also saves every trace as a file in a folder of your own Google Drive:
one `session-<id>.json` per browser session (paste the session id from the page's top-right corner into the Drive search box).
Langfuse keeps your data for 30 days on the free plan, so this gives you a permanent copy.

One-time setup:
1. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the **Google Drive API**.
2. **Google Auth Platform**: fill in **Branding** (app name, support email, developer email only), set **Audience** to *External* and leave it in *Testing*, then add your Gmail under **Test users**. (Publishing is not needed, and it would require Google's verification.)
3. **Clients → Create client → Desktop app**, then download the JSON into the project folder (`client_secret_*.json` or `google_oauth_client.json`; both are git-ignored).
4. Log in once: `python scripts/google_drive_login.py` (your browser opens; click Allow). This saves `google_token.json` (git-ignored) and puts a `connection-test.json` file in the Drive folder.
5. Set `DRIVE_TRACES=true` in `.env` and start the app. Files appear in the Drive folder `place-discover-traces` about 15 seconds after each search.

Notes: the app asks only for the `drive.file` permission, so it can see only the files it created. While the Google Cloud app is in *Testing*, Google expires the login after 7 days: run the login script again when the log says so. Saved traces contain prompts and answers (including message negotiation text), so keep the folder private. This is meant for your laptop; on Render the login files are not available, so leave it off there.
