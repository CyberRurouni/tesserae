# Tesserae

AI-driven prospecting automation for the Meta Ad Library.

Tesserae searches the Meta Ad Library for ads matching your profile, then uses AI
checkpoints to judge which ads are actually relevant to you — so you get a short
list of real advertisers hiring for your stack instead of thousands of raw rows.

## How it works

| Stage | What happens |
| --- | --- |
| **Keyword lifecycle** | Either you supply keywords, or Tesserae generates them from your profile. Your keywords and the AI-generated ones form a single cycle, and your own keywords are only ever searched once. |
| **Scraping** | Ads are collected sequentially, one keyword at a time, on a single shared browser session — captcha safety. |
| **Relevance judging** | Every ad is scored against your profile. Only relevant ads are kept. |
| **Coverage review** | Once enough keywords are in the pool, Tesserae identifies gaps and composes replacement prompts, so later runs target fresh territory. |

Runs are resumable: keywords and prompts carry TTLs in Redis, so a crashed run
picks up where it left off and expired territories simply reopen.

## Requirements

- Python 3.11+ (developed against 3.14)
- Node.js 18+ (Next.js 14)
- Redis
- A Gemini API key

## Setup

```bash
# 1. Backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env      # then add your GEMINI_API_KEY

# 2. Redis must be running
redis-server

# 3. Frontend
cd frontend
npm install
```

## Running

```bash
# Backend — FastAPI on :8000
python3 -m interface.main

# Frontend — Next.js on :3000
cd frontend && npm run dev
```

Then open http://localhost:3000.

> Run `next build` and `next dev` separately, never at the same time — a
> concurrent build corrupts `.next` and causes `ChunkLoadError` 404s.

### Search from the CLI

```bash
python3 -m interface.orchestrate --run-mode generate --cycles 3
```

## Configuration

`.env` (see `.env.example`):

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Powers the AI checkpoints. **Required.** |
| `GEMINI_MODEL` | Model override. |
| `REDIS_HOST` / `REDIS_PORT` | Redis location. |

## Layout

```
core/          Pydantic schemas shared across the system
modules/       scraper, checkpoints, storage, orchestrator, collection
interface/     FastAPI app + CLI entry points
frontend/      Next.js dashboard
genesis/       First-run bootstrap
```

## Privacy

Runtime data is deliberately not committed. `data/`, `browser_profile/` (a
Chromium user profile that contains session cookies), `exports/`, logs and any
`.env` file are all git-ignored. Generate them locally by running a search.