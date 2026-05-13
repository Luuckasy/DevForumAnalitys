# Roblox DevForum Monitor Bot

A Railway-ready Python service that polls the [Roblox DevForum](https://devforum.roblox.com), detects new topics, runs them through an AI model, and posts a Brazilian-Portuguese summary to a Discord webhook.

It uses the public Discourse JSON endpoints (`/latest.json`, category `*.json`, `/t/{id}.json`), so no DevForum credentials are needed.

## Features

- Polls one or more Discourse JSON endpoints on a configurable interval.
- Filters topics by keyword and ignore-keyword lists.
- AI analysis with a strict JSON schema (summary, context, impact, action, urgency, technical notes).
- Discord embeds with urgency-based color coding (Low / Medium / High / Critical).
- SQLite persistence so topics are never re-notified after a restart.
- Handles HTTP timeouts, Discord rate limits, and bad AI JSON without crashing.
- Minimal healthcheck HTTP server on `$PORT` for Railway.

## Local setup

```bash
pip install -r requirements.txt
cp .env.example .env   # edit values
python main.py
```

The first cycle will mark recent topics from the configured endpoints. Only items inserted in `processed_items` won't be re-notified.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | *(required)* | Discord webhook URL to post embeds to |
| `AI_PROVIDER` | `gemini` | Which AI to use: `openai`, `gemini`, or `groq` |
| `OPENAI_API_KEY` | *(required if `AI_PROVIDER=openai`)* | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI chat model |
| `GEMINI_API_KEY` | *(required if `AI_PROVIDER=gemini`)* | Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model |
| `GROQ_API_KEY` | *(required if `AI_PROVIDER=groq`)* | Groq API key |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model |
| `CHECK_INTERVAL_MINUTES` | `10` | Polling interval in minutes |
| `DEVFORUM_BASE_URL` | `https://devforum.roblox.com` | Base URL |
| `MONITORED_ENDPOINTS` | `/latest.json` | Comma-separated Discourse JSON endpoints |
| `KEYWORDS` | *(empty = accept all)* | Comma-separated keywords matched against title/excerpt/tags |
| `IGNORE_KEYWORDS` | *(empty)* | Comma-separated keywords to skip |
| `MIN_LIKES` | `0` | Minimum likes for any topic to be forwarded (fallback) |
| `MIN_LIKES_BY_CATEGORY` | *(empty)* | Per-category likes threshold. Format: `45=0,17=15` (Updates=any, Resources=15+) |
| `MAX_TOPICS_PER_CHECK` | `20` | Max topics processed per endpoint per cycle |
| `DATABASE_PATH` | `./data/devforum_bot.sqlite3` | SQLite file path |
| `PORT` | `8080` | Port for the healthcheck HTTP server |
| `HEALTHCHECK` | `1` | Set to `0` to disable the healthcheck server |

## AI providers

The bot can talk to three different APIs. Pick one with `AI_PROVIDER` and set the matching `*_API_KEY`. Only the key for the selected provider is required — the other key fields can stay empty.

| Provider | Get a key | Default model | JSON mode | Why pick it |
|---|---|---|---|---|
| `gemini` (**default**) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `gemini-2.0-flash` | yes (`response_mime_type=application/json`) | Generous free tier — start here. |
| `groq` | [console.groq.com/keys](https://console.groq.com/keys) | `llama-3.1-8b-instant` | yes (OpenAI-compatible) | Very fast, free tier with rate limits, Llama models. |
| `openai` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | `gpt-4o-mini` | yes | Highest quality output, requires paid quota. |

All three return the same JSON schema:

```json
{
  "summary": "...",
  "context": "...",
  "impact": "...",
  "recommended_action": "...",
  "urgency": "Low | Medium | High | Critical",
  "developer_notes": "..."
}
```

If the model returns broken JSON the bot retries once. If the API itself fails (auth, quota, rate limit, network), the bot posts a fallback embed whose **Notas técnicas** field contains the real error class and message — so you can tell apart `AuthenticationError`, `RateLimitError: insufficient_quota`, `NotFoundError: model ... not found`, etc. without digging through logs.

To switch providers at runtime, change `AI_PROVIDER` in Railway → Variables and redeploy. No code change needed.

## Deploying on Railway

1. **Create a GitHub repo** and push these files (`main.py`, `requirements.txt`, `Procfile`, `README.md`, `.env.example`, `.gitignore`).
2. **Create a Railway project** → *Deploy from GitHub repo*.
3. **Add environment variables** in Railway → *Variables* (use `.env.example` as reference). At minimum set `DISCORD_WEBHOOK_URL` and `OPENAI_API_KEY`.
4. **Start command** — Railway picks up `Procfile` automatically. If needed, set it manually to:
   ```
   python main.py
   ```
5. **Add a Railway Volume** mounted at `/app/data` and set `DATABASE_PATH=/app/data/devforum_bot.sqlite3` so the SQLite file survives redeploys.
6. **Check logs** in Railway → *Deployments* → *Logs*. You should see `Polling DevForum endpoints…` every interval and `Cycle done` summaries.

The bot also exposes `GET /` returning `ok` on `$PORT`, which Railway can use as a healthcheck.

## How it works

1. On each tick, fetch every endpoint in `MONITORED_ENDPOINTS` and read `topic_list.topics`.
2. For each topic:
   - Skip if its `item_id` is already in `processed_items`.
   - Skip (and record) if it matches `IGNORE_KEYWORDS`.
   - Skip if `KEYWORDS` is non-empty and none of them match.
   - Fetch `/t/{id}.json` and extract the first post (HTML-cleaned).
   - Send to OpenAI with a system prompt forcing a strict JSON response.
   - Send a Discord embed.
   - Only mark the topic as processed after the Discord call succeeds.
3. Sleep until the next interval.

## Customization for Roblox game developers

You almost certainly want to tune what gets through.

### Change keywords

Set `KEYWORDS` to a comma-separated list of topics you care about. The match is case-insensitive and checks the topic title, excerpt and tags. Examples:

- **Live-ops oriented:** `datastore,memory store,messagingservice,teleport,outage,downtime`
- **Monetization:** `monetization,dev products,gamepass,passes,robux,marketplace,policy`
- **Engine/perf:** `performance,physics,animation,rendering,humanoid,memory,leak,crash`
- **UGC/Avatar:** `avatar,ugc,bundle,layered clothing,marketplace`

Leave `KEYWORDS` empty to forward every new topic (high volume — not recommended).

Use `IGNORE_KEYWORDS` to silence noise, e.g. `hiring,recruitment,portfolio,commission`.

### Add monitored categories

Discourse exposes a JSON feed for every category. Open a category in the browser, append `.json`, and add the path to `MONITORED_ENDPOINTS`. Examples:

```
MONITORED_ENDPOINTS=/latest.json,/c/updates/announcements/45.json,/c/bug-reports/14.json,/c/development-discussion/56.json
```

You can keep `/latest.json` plus specific category endpoints to catch broad announcements and your area of interest at the same time.

### Tune urgency

Urgency comes from the AI. To bias it, prepend extra context to `KEYWORDS` (e.g. include `breaking,deprecated,policy,outage`) — the model treats those signals as higher impact. The mapping from urgency to embed color is in `URGENCY_COLORS` in `main.py`.

### Storage

By default SQLite lives at `./data/devforum_bot.sqlite3`. On Railway, mount a Volume and point `DATABASE_PATH` at it (see deploy step 5) so the dedup history survives redeploys.

## Troubleshooting

- **No Discord messages, but logs say "Cycle done: 0 sent"** — your `KEYWORDS` filter probably excluded everything. Temporarily empty `KEYWORDS` to confirm the pipeline works, then narrow it down.
- **`DISCORD_WEBHOOK_URL is required`** — env var isn't loaded. Locally, check `.env`; on Railway, check the Variables tab.
- **AI returns empty/invalid JSON** — the bot retries once and falls back to a plain summary. Check the logs for `AI returned unparseable JSON`.
- **First run floods the channel** — that's expected if your filters are wide. Narrow `KEYWORDS` or run it once with a tighter list to seed the database, then loosen.
