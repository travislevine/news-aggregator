# Signal

A lightweight, local-first news collector and daily digest for three topics:

- **Finance**: markets, macro, central banks and your ticker watchlist
- **Cybersecurity**: breaches, vulnerabilities, actively exploited bugs (CISA KEV), ransomware, crypto/DeFi hacks
- **Embedded / IoT / Homelab**: smart home, SBCs and microcontrollers, self-hosting, beginner homelab guides, IoT security

Signal collects articles from RSS feeds every hour, saves each one as a Markdown file (the
folder opens as an **Obsidian vault**), summarises them with a **local model through Ollama**
(or a cloud API if you prefer), writes a **daily digest**, and sends the highlights to your phone.
A small web UI lets you read digests, browse and search articles, manage sources and ask
questions about the news.

No Docker, no Node, no build step. One Python process that idles at about 100 MB of RAM.

---

## Contents

- [Quick start (Windows 11)](#quick-start-windows-11)
- [Daily use](#daily-use)
- [Configuration](#configuration)
- [Models](#models)
- [Notifications](#notifications)
- [Start with Windows](#start-with-windows)
- [The vault (Obsidian)](#the-vault-obsidian)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Quick start (Windows 11)

### 1. Install the tools

Open **PowerShell** and run:

```powershell
# uv: Python project manager (it also installs Python 3.12+ for you)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Ollama: runs models locally on your GPU
winget install Ollama.Ollama

# Git, if you don't have it
winget install Git.Git
```

Close and reopen PowerShell so the new commands are on your PATH.

### 2. Download a model

Ollama starts automatically after install (look for the llama icon in the system tray).

```powershell
ollama pull qwen3:14b          # summaries, digest and Q&A (about 9 GB)
ollama pull nomic-embed-text   # optional: better search and clustering (about 270 MB)
```

### 3. Get Signal

```powershell
cd $HOME\Documents
git clone https://github.com/travislevine/news-aggregator.git signal
cd signal
uv sync
```

`uv sync` creates a virtual environment in `.venv` and installs everything.

### 4. Configure

```powershell
Copy-Item .env.example .env
uv run signal validate-sources
```

The first command (or the first run of any command) also creates `config.yaml` from
`config.example.yaml`. Edit `config.yaml` to change your watchlist, schedule or models,
or use the **Settings** page later.

`validate-sources` checks every feed in `sources.yaml`. For any dead feed it tries, in order:
the feed's fallback URLs, feed autodiscovery on the site, then a Google News `site:` search.
If nothing works it disables the source with a note. Nothing is silently dropped. A report is
saved to `logs\validate-sources-*.md`. Use `--dry-run` to only report.

### 5. Run it

```powershell
uv run signal serve
```

Open <http://127.0.0.1:8765>. On start Signal collects from every source immediately, then
every hour. The digest is built at 11:00 (Brisbane time). If you start the PC after 11:00, the
digest is built as soon as the first collection finishes.

To get a digest straight away: click **Collect now**, wait for it to finish, then click
**Regenerate digest**.

---

## Daily use

### Web UI

| Page | What it's for |
|---|---|
| **Today** | Latest digest with flags at the top. Buttons: Collect now, Summarise now, Regenerate digest. |
| **Digests** | Every past digest by date. |
| **Articles** | Filter by topic, source, date, ticker, CVE, KEV and "affects me". Full-text search. |
| **Ask** | Ask questions about the collected news. Answers cite the articles they came from. |
| **Sources** | Health of every feed, enable/disable, test, add a source (paste a site URL and the feed is found for you), run history. |
| **Settings** | Watchlist, your gear, schedule, models per job (with a connection test), notifications (with a test button). |

### Command line

Run these from the project folder, prefixed with `uv run`:

| Command | What it does |
|---|---|
| `signal serve` | Web UI + scheduler. Add `--no-scheduler` for the UI only. |
| `signal collect` | Fetch all sources now. `-s "BleepingComputer"` for one source, `--summarize` to summarise afterwards. |
| `signal summarize` | Summarise articles that don't have a summary yet. |
| `signal digest` | Build today's digest. `--force` rebuilds it, `--no-llm` makes a headline-only digest, `--no-notify` skips notifications. |
| `signal ask "What happened with Ivanti this week?"` | Q&A from the terminal. |
| `signal validate-sources` | Check every feed and repair or disable dead ones. |
| `signal reindex` | Rebuild the SQLite index from the vault files. |
| `signal notify-test` | Send a test notification. |
| `signal embed` | Compute embeddings for existing articles (needs `qa.embeddings_enabled: true`). |
| `signal cleanup` | Delete old articles that never appeared in a digest (only if `retention.cleanup_after_days` is set). |

---

## Configuration

Three files, all in the project folder:

| File | Holds | In git? |
|---|---|---|
| `config.yaml` | Settings: paths, time zone, schedule, topics and keywords, watchlist, gear, models, notifications, web, retention. Copied from `config.example.yaml` on first run. Every option is commented. | No |
| `sources.yaml` | Every feed. Edit by hand or from the Sources page. | Yes |
| `.env` | Secrets: API keys, Apprise URLs, web password. | No |

### Watchlist

```yaml
watchlist:
  - ticker: NVDA
    names: [Nvidia]
  - ticker: BHP.AX          # ASX tickers work too
    names: [BHP, "BHP Group"]
```

Each ticker gets its own Yahoo Finance and Google News feed. Articles are tagged with a ticker
when they contain `$NVDA`, `(NASDAQ: NVDA)`, a company name, or the bare ticker as a whole word.
Bare tickers only count when they're 3+ letters and not common words, so `ON`, `IT`, `A` and
`ALL` only match in the `$ON` or `(NYSE: ALL)` forms.

### Your gear

`gear` starts empty. When you build a homelab, list what you own, for example:

```yaml
gear: [Ubiquiti, Synology, "Home Assistant", Proxmox, OpenWrt]
```

Articles that mention any of these get `affects_me: true`, show up under **Affects your gear**
in the digest, and trigger an instant alert.

### Sources

Each entry in `sources.yaml`:

```yaml
- name: BleepingComputer
  url: https://www.bleepingcomputer.com/feed/
  topic: cyber                 # finance | cyber | iot
  type: rss                    # rss | json | google_news | hnrss
  enabled: true
  fetch_full_text: true        # fetch the full page if the feed text is under ~800 characters
  paywalled: false             # true = keep headline + feed summary only, never fetch the page
  priority: 1                  # 1 best .. 3 lowest (used to pick the main source of a story)
  notes: ""
  # optional:
  query: '...'                 # google_news only
  keyword_filter: [hack, exploit]   # keep only items that mention one of these
  fallback_urls: [...]         # tried by validate-sources if the main URL is dead
  extra_topics: [cyber]        # also tag as another topic
```

For beginners, the starting list includes selfh.st, Jeff Geerling, r/homelab, r/selfhosted,
filtered XDA Developers homelab articles and a Google News search for beginner homelab guides.

---

## Models

Each job can use a different provider and model:

```yaml
llm:
  jobs:
    article_summary: { provider: ollama, model: "qwen3:14b" }
    digest:          { provider: ollama, model: "qwen3:14b" }
    ask:             { provider: ollama, model: "qwen3:14b" }
    embeddings:      { provider: ollama, model: "nomic-embed-text" }
  providers:
    ollama: { base_url: "http://localhost:11434", num_ctx: 16384, keep_alive: "10m", think: false }
```

**Context window:** Ollama's default context is small and silently cuts off long prompts.
Signal always sends `num_ctx` (16384 by default) and sizes its prompts to fit.

**For a 16 GB GPU** (such as an RTX 5080), a 12–14B model at Q4 fits comfortably with a 16k context:

| Model | Size | Notes |
|---|---|---|
| `qwen3:14b` (default) | ~9 GB | Strong at summaries and JSON. `think: false` keeps it fast. |
| `gemma3:12b` | ~8 GB | Good writing, slightly faster. |

To switch, pull the model (`ollama pull gemma3:12b`) and change the model name on the
Settings page or in `config.yaml`. Model names live only in config.

**Cloud APIs:** add the key to `.env`, then set the job's provider:

- `openai_compat` for OpenAI or anything OpenAI-compatible (LM Studio, vLLM...). Set `base_url`.
- `openrouter` (preconfigured, uses `OPENROUTER_API_KEY`).
- `anthropic` (uses `ANTHROPIC_API_KEY`).

A common mix: local model for per-article summaries (many small calls), a cloud model for the
digest (one bigger call). Set `llm.fallback_provider` to use an API only when Ollama isn't running.

**If the model is down,** collection keeps working and articles are stored without summaries.
The digest is retried after each collection. If the model is still down
`digest_fallback_after_hours` (4 h) after digest time, a headline-only digest is sent instead.

**Prompts** are Markdown files in `signal_news/llm/prompts/`. To customise them without touching
the code, copy them to a folder and set `prompts_dir` in `config.yaml`.

---

## Notifications

Signal uses [Apprise](https://github.com/caronc/apprise/wiki), so one URL format covers ntfy,
Discord, Telegram, email and many more. Put the URLs in `.env` (`APPRISE_URLS`, comma-separated)
or on the Settings page.

**ntfy (no account needed):**
1. Install the ntfy app on your phone and subscribe to a long, random topic name, e.g. `signal-tl-x7k2q9`. Anyone who knows the topic can read it, so make it hard to guess.
2. Set `APPRISE_URLS=ntfy://signal-tl-x7k2q9`.

**Discord:**
1. In your server: Server Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Paste it as is (`https://discord.com/api/webhooks/...`) or in the form `discord://webhook_id/webhook_token`.

Both together: `APPRISE_URLS=ntfy://signal-tl-x7k2q9, https://discord.com/api/webhooks/123/abc`

Then click **Send test** on the Settings page (or run `uv run signal notify-test`).

What gets sent:
- After each digest: **Today's top 5 + Flags**, trimmed to fit (Discord's limit is 2000 characters), with a link to the full digest. Email targets (`mailto://`, `mailtos://`) get the whole digest as HTML.
- Instant alerts (on by default): new CISA KEV entries and articles about your gear.

Notification failures are logged and never stop a run.

---

## Start with Windows

**Task Scheduler (recommended):** from the project folder run

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_startup.ps1
```

This registers a task that starts `signal serve` hidden, one minute after you log in, and
restarts it if it crashes. Output goes to `logs\startup.log`. Remove it with the same command
plus `-Uninstall`.

**Startup folder (simpler):** press `Win+R`, type `shell:startup`, and create a shortcut with
target:

```
powershell.exe -WindowStyle Hidden -Command "cd '%USERPROFILE%\Documents\signal'; uv run signal serve"
```

---

## The vault (Obsidian)

```
vault/
  articles/2026/11/04/cyber/2026-11-04-ivanti-zero-day-exploited-in-the-wild-a1b2c3.md
  digests/2026-11-04.md
  qa/2026-11-04-143012-what-happened-with-ivanti.md
```

Each article is a Markdown file with YAML front matter (`title`, `url`, `source`, `topic`,
`subtopics`, `published`, `collected`, `tickers`, `cves`, `kev`, `vendors`, `affects_me`,
`cluster_id`, `full_text`, `summary`, `importance`, `sentiment`, `tags`...) followed by the
article text. Digests link to articles with `[[wikilinks]]` and to the original page.

In Obsidian: **Open folder as vault** and pick the `vault` folder. Tags like `#news/cyber`,
`#kev` and `#ticker/NVDA` work out of the box.

**The vault is the source of truth.** The SQLite database in `data/` is only an index and cache.
Delete it and run `uv run signal reindex` to rebuild it. Any tool (or another program) can read
the vault without Signal.

---

## How it works

**Collection** (`signal collect`, hourly):
1. Fetch every enabled feed concurrently (async httpx). Conditional GET with ETag/Last-Modified, at most one request per second per site, retries with backoff on timeouts, 429 and 5xx, and a clear User-Agent.
2. Google News links are resolved to the real article URL. If that fails, the Google link and the feed snippet are kept.
3. URLs are cleaned (tracking parameters, fragments, AMP variants) and exact duplicates skipped.
4. The full page is fetched only if `fetch_full_text: true`, the feed text is under ~800 characters, the source isn't paywalled, and `robots.txt` allows it. Extraction uses trafilatura, with readability as fallback.
5. Rule-based tagging: subtopics, tickers, CVEs, vendors, your gear. CVEs are cross-checked against CISA KEV.
6. Same-story articles from different outlets get a shared `cluster_id` (title similarity within 48 h, plus embeddings if enabled).
7. CISA KEV: each run diffs the catalog. Every newly added entry becomes its own article and always appears in the digest.
8. Source health is recorded. A source that fails 10 times in a row is disabled and named in the next digest footer.

**Summaries** (after each collection): each new article gets 1–2 factual sentences plus
`importance` (1–5), `sentiment` (finance only) and `entities`, written back into its front matter.

**Digest** (daily): covers everything collected **since the previous digest**, so weekends and
days off roll into one. If the last digest is more than 3 calendar days old, only the last 72
hours are used, and the digest says so. Story clusters are summarised per topic section in
chunks that fit the context window; then the model picks **Today's top 5**. Flags (KEV, your
gear, watchlist news) are rule-based. Every bullet must cite numbered stories. Citations become
links, and bullets without a valid citation are dropped.

**Q&A:** your question is parsed for dates ("this week", "since Monday", "last 3 days"), topics
and tickers, then matched with SQLite FTS5 (BM25), optionally combined with embedding search.
The model answers only from the retrieved articles and cites them.

**Decoupled by design:** collection, summarising and digests are separate commands sharing
state through SQLite and the vault. You could later run `signal collect` on an always-on
Raspberry Pi writing to a shared vault folder and run `signal summarize`/`digest` on the PC.
SQLite run locks stop overlapping runs.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Settings → Test connection says Ollama isn't reachable | Make sure Ollama is running (tray icon) or run `ollama serve`. Check `llm.providers.ollama.base_url`. |
| "model not found" | `ollama pull qwen3:14b` (or whichever model the job uses). |
| A source keeps failing | Sources page → Test. Run `uv run signal validate-sources`. Reddit sometimes returns 429; that's tolerated. |
| Digest says "headlines only" | The model was unavailable. Run `uv run signal digest --force` once Ollama is up. |
| Want to reach the UI from your phone | Set `web.host: 0.0.0.0` and `SIGNAL_WEB_PASSWORD` in `.env`. Signal refuses to listen beyond localhost without a password. |
| Logs | `logs\signal.log` (rotating). Run history is on the Sources page. |

---

## Development

```powershell
uv sync                     # includes dev tools
uv run pytest               # offline tests with saved fixtures, no network
uv run ruff check .
uv run ruff format .
```

Layout:

```
signal_news/
  cli.py            typer commands
  config.py         config/sources/.env loading (pydantic)
  jobs.py           collect/summarise/digest runners with locks and progress
  scheduler.py      APScheduler jobs and catch-up logic
  notify.py         Apprise wrapper
  collector/        feeds, extraction, Google News/hnrss/KEV, dedupe, tagging, validation
  storage/          vault (Markdown + front matter), SQLite index, reindex
  llm/              providers (Ollama, OpenAI-compatible, Anthropic) and prompts
  summarize/        per-article summaries, clustering, digest, Q&A
  web/              FastAPI app, Jinja2 templates, static files (HTMX and Pico.css bundled)
tests/              pytest suite and fixtures
scripts/            Windows startup script
```

### Notes on choices

- The Python package is `signal_news`, not `signal`: a package named `signal` would shadow
  Python's built-in `signal` module, which asyncio and uvicorn rely on. The command is still `signal`.
- HTMX and Pico.css are bundled in `signal_news/web/static`, so the UI works offline.
- `nh3` sanitises rendered HTML, because feed content is shown in the browser.
- Embeddings are stored as float32 blobs in SQLite and searched with numpy, which is simpler
  than `sqlite-vec` at this scale.
- Finance summaries report news only. Nothing in Signal recommends trades.
