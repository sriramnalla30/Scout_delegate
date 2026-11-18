# Deployment

```
browser ──> Netlify  ramplacesearch.netlify.app      the chat page only (static/)
   │            config.js says where the API is
   └──────> Render   scout-place-finder.onrender.com the API: coordinator, scout agent, geocode, Google Maps search
                       ├── Groq (the model)       ├── Langfuse (traces, optional)
                       └── /data disk: accounts, chats, the agent's memory, stored searches
```

The page is a complete static site (`static/`). The API serves the same page too, so the Render address also works on its own.

## What is deployed now

| | Page | API |
|---|---|---|
| Where | Netlify site `ramplacesearch` (id `4f987fc4-dc95-40e2-b234-d2e1ea29c400`) | Render service `scout-place-finder` (id `srv-dbukrvhsrm7s73f6nun0`), Docker, free plan, Singapore |
| Address | https://ramplacesearch.netlify.app | https://scout-place-finder.onrender.com |
| Follows | this repo, branch `first_half`, publish `static`, command `node scripts/write-config.mjs` | this repo, branch `first_half`, auto-deploy on push |
| Settings | `SCOUT_API_BASE` | see the table below |

When `first_half` is merged into `main`, change the branch in both places (Netlify: Site configuration, Build and deploy, Branches; Render: Settings, Branch).
The old service `PlaceDiscoverAgent` (Render, Python, repo `PlaceDiscoverAgent`) was the backend of the earlier page. It is not used any more and can be deleted.

Measured on the free plan: the first search takes about 50 seconds (geocode about 40 s because the free instance has a fraction of a CPU, the search 2 s, the model calls the rest) and fits in 512 MB.

## What is where

| Piece | File | Used by |
|---|---|---|
| Page address of the API | `static/config.js` (empty = same site), written at build time by `scripts/write-config.mjs` | Netlify build |
| Netlify settings | `netlify.toml` (publish `static`, run the script) | Netlify |
| Container | `Dockerfile` (Python, headless Chromium, the Linux scraper `gmaps` v1.18.1) | Render |
| Render settings | `render.yaml` (service, plan, disk, variables) | Render |
| Accounts and sign-in | `app/auth.py` (users, sessions, Google check, who owns which chat), `static/auth.js` (the sign-in screen) | the server, the page |
| Who may call the API | `app/access.py` (other sites, the signed-in account, message and sign-in limits) | the server |

## Settings

| Variable | Where | Meaning |
|---|---|---|
| `GROQ_API_KEY` | Render | the model key (required) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Render | traces and prompt versions (optional) |
| `LANGFUSE_TRACING_ENVIRONMENT` | Render (set in the Dockerfile) | `production`: the Environment (and a tag) on every trace and session in Langfuse. Locally it is `development`. `/api/health` shows the value in use |
| `ALLOWED_ORIGINS` | Render | the Netlify address, so its page may call the API (`https://ramplacesearch.netlify.app`) |
| `GOOGLE_CLIENT_ID` | Render | the Google OAuth client id that switches "Continue with Google" on (see Sign-in below) |
| `SESSION_DAYS` | Render (optional) | how long a sign-in lasts (30) |
| `CHAT_RATE_PER_HOUR` | Render | messages one account may send an hour (40), so a public page cannot drain the model key |
| `SCOUT_API_BASE` | Netlify | the Render address, written into `config.js` at build time. A production build without it fails |

Other keys in `.env` (Twilio, SerpAPI, Tavily, the evals' Gemini key) are not read by the app and must not be put on a host.

## Steps

1. **Render**: New, Blueprint, pick this repo and branch. Fill in the keys marked `sync: false`. The first build takes several minutes (Chromium). `render.yaml` is set to the free plan; the paid settings are in its comments.
2. **Netlify**: Site configuration, Build and deploy: repository this one, publish directory `static`, command `node scripts/write-config.mjs`. Environment variable `SCOUT_API_BASE` = the Render address. Deploy.
3. Open the Netlify address, ask for "gyms in Koramangala Bangalore", and check that the places appear.

## Limits to know

- **Memory.** The geocode step starts Chromium: about 900 MB while it runs on Windows (working set of Python plus Chromium), yet it completed on the free plan's 512 MB on Linux. `SCOUT_MAPS_PARALLEL=1` keeps it to one lookup at a time. If the service ever runs out of memory, Render restarts it and the chat shows an error: move to the `standard` plan (2 GB).
- **The model's refusals.** Groq sometimes refuses a reply with `tool_use_failed` when the model writes its final report as text. `app/llm.py` recovers that report (see `tests/test_llm.py`); a failed chat turn is written to the service log with its cause.
- **Speed.** A first search takes about 30 to 60 seconds (geocode about 20 s, the search 3 s, the model calls). A repeated search is faster (the stored result is reused for 30 minutes).
- **Disk.** Without the `/data` disk (paid plans only), chats and the agent's memory are lost whenever the service restarts or sleeps and wakes. The free plan also sleeps after 15 minutes idle: the first request after that waits about a minute.
- **Accounts live in `/data` too.** `users.sqlite` (accounts, sessions, who owns which chat) sits next to the chats. On the free plan there is no disk, so every restart or sleep-and-wake **deletes the accounts and every chat**: people must sign up again (Google accounts are recreated on the next sign-in, but the history is gone). To keep them, use a paid plan with the disk in `render.yaml`.
- **No email checks.** A password account is not verified by email (there is no mail service). Google sign-in is verified by Google; when a Google sign-in meets an unverified password account with the same address, the account is kept and the password is removed, so nobody can hold an address they never proved.

## Sign-in

Everyone signs in: with an email and password, or with Google. Each account sees only its own chats, and a conversation can be used only by the account that started it (`app/auth.py`, `app/access.py`). The page keeps a session token in the browser and sends it as `Authorization: Bearer ...`; the server stores only a hash of it, and passwords only as salted scrypt hashes. Wrong passwords are rate-limited per address and email.

**On a laptop** `.env` can list accounts to create at startup, so they are always there:

```
SEED_USERS=test@rate.gmail.com:test@rate123;thread123@threadmail.com:thread123
LEGACY_CHATS_OWNER=thread123@threadmail.com
```

`LEGACY_CHATS_OWNER` copies the chats saved before accounts existed (`data/chats/*.json`) into that account, once; the old files are left where they are. Do not set either on the deployed API: there, people sign up themselves and start with an empty history.

**Google sign-in** needs a client id (the code is ready, the id is the one thing only the owner of the Google account can create):

1. Google Cloud Console, APIs and Services, OAuth consent screen: create the app (External, name Scout, your email), then add yourself and your sister as test users while it is in Testing mode (or publish it).
2. Credentials, Create credentials, OAuth client ID, type Web application. **Authorized JavaScript origins**: `https://ramplacesearch.netlify.app`, `https://scout-place-finder.onrender.com`, `http://localhost:8000`, `http://127.0.0.1:8000`. No redirect URI is needed.
3. Copy the client id (`...apps.googleusercontent.com`) into Render's `GOOGLE_CLIENT_ID` (and into `.env` for a laptop), then restart. The Google button on the sign-in screen starts working at once; no rebuild of the page is needed.
