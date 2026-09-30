# Build Progress Log

Running log of what has been built, decisions taken, and what is still open.
Newest entries at the bottom of each phase.

## Status

| Phase | Status |
|---|---|
| 1 — Configuration & state | ✅ Done |
| 2 — Fetch | 🟡 Code done, tested with a mock API; needs a live run with your key |
| 3 — Classify | 🟡 Code done, tested with a mock Claude client; needs a live run on 10 real videos |
| 4 — Metrics | ✅ Done (pure arithmetic, fully verified by tests) |
| 5 — Comment synthesis | 🟡 Code done, tested with mocks; needs a live run |
| 6 — Report | 🟡 Built; sample digest from synthetic data is on the dashboard. Needs your review |
| 7 — Deliver & schedule | 🟡 Code and workflow written; needs a GitHub repo + secrets for a `workflow_dispatch` run |
| README | ⬜ Not started |

## Needed from you

- [ ] **YouTube Data API v3 key** → put in `.env` as `YOUTUBE_API_KEY`
- [ ] **Anthropic API key** → `.env` as `ANTHROPIC_API_KEY`
- [ ] **Competitor channels** (names + channel IDs or @handles) → `config/brands.yaml`
- [ ] *(optional)* Slack incoming webhook → `.env` as `SLACK_WEBHOOK_URL`
- [ ] *(Phase 7)* A GitHub repo to push to, so the Actions workflow can run

---

## Setup

- Project lives at `~/code/creative-monitor`, git initialised.
- Python 3.11.9 virtualenv in `.venv/`; dependencies in `requirements.txt`
  (pyyaml, requests, anthropic, python-dotenv, flask, markdown, pytest).
- Secrets go in `.env` (git-ignored); `.env.example` documents the keys.
- **Local dashboard** (`dashboard.py`) at http://localhost:8765 — shows this log,
  the database contents, run history and the latest digest. Refreshes every 5 s.

## Phase 1 — Configuration & state ✅

**Built**
- `config/brands.yaml` — placeholder brands (to be replaced with real channels).
- `config/rubric.yaml` — the six dimensions from the plan, plus a `version` field.
- `src/config.py` — loads and validates both YAML files.
- `src/store.py` — SQLite schema + all read/write helpers. DB at `data/monitor.db`.
- `tests/test_store.py` — write/read a video, rerun safety, config loading. 3/3 passing.

**Decisions / deviations from the plan**
- **Classification values stored as JSON (`values_json`) instead of one column per
  dimension.** Per-dimension columns would mean a rubric change needs a schema
  migration, which breaks the "rubric is configuration, not code" principle.
  `rubric_version` is kept on every row.
- **Added a `channels` table** to cache the uploads-playlist ID (plan says cache it).
- **Added a `quarantine` table** so videos that fail classification are recorded with
  a reason rather than silently dropped, and are not retried forever.
- **`metrics.measured_at` is a UTC date**, not a timestamp, so running twice in one day
  overwrites rather than double-counts.
- **`runs` gained `status` and `message`** so a failed run (e.g. quota exhausted) is
  visible in history.

## Phase 2 — Fetch 🟡

**Built**
- `src/fetch.py`
  - `YouTubeClient`: thin wrapper over the REST API (plain `requests`, no Google SDK).
    Counts quota units and raises `QuotaExceeded` or `YouTubeError` with a clear message.
  - `uploads_playlist_id`: channels.list, cached in the `channels` table after the first lookup.
    Accepts either a `UC…` channel ID or an `@handle` in `brands.yaml`.
  - `scan_new_video_ids`: walks the uploads playlist newest-first and stops at the first
    known video, anything older than the brand's latest stored video, or 100 items.
  - `fetch_video_details`: videos.list batched 50 per call. Parses ISO-8601 durations and
    picks the best thumbnail (maxres, then high).
  - `fetch_stats`: refreshes counts for each brand's 20 most recent known videos so the
    baseline uses current numbers (1 unit per 50 videos).
  - `fetch_comments`: top 50 by relevance, **text only** (author names/IDs are dropped at
    the source). Returns `None` when comments are disabled.
  - `run_fetch`: fetches everything into memory first, **then writes in a single
    transaction**, so an API error or exhausted quota leaves the DB untouched.
- `tests/test_fetch.py`: a fake YouTube API that covers:
  - second run processes zero videos ✅
  - a new upload is picked up ✅
  - the uploads playlist ID is cached ✅
  - a 10-video burst is capped at N per run and the remainder comes in on later runs ✅
  - exhausted quota leaves state untouched ✅
  - disabled comments return `None` instead of crashing ✅

**Decisions**
- **First run backfills the 20 newest videos per brand.** Without these there is no
  baseline to compare against, and Phase 4 needs the last 20.
- **Overflow handling:** later runs process new uploads oldest-first, up to `per_run_cap`
  (25). The rest are logged and picked up automatically next run, because the scan stops
  at the first *known* video, not at a date.
- **Comments are not stored at fetch time.** They stay in memory and go straight to
  synthesis (Phase 5). If a run is interrupted, they are re-fetched for any video without
  themes, which costs 1 unit.

**Still to verify:** a real run against your competitor channels.

## Phase 3 — Classify 🟡

**Built**
- `src/classify.py`: one Claude call per video.
  - Sends the thumbnail (base64, best available size) first, then the title, the
    description (truncated to 1,500 chars), tags, duration and the rubric.
  - `build_schema(rubric)` turns `rubric.yaml` into a JSON schema with an `enum` per
    dimension. `build_prompt` lists the same allowed values. **Swapping the rubric changes
    both the schema and the prompt with no code change** (there's a test for this).
  - `validate()` follows the plan's order:
    - parse the JSON; on failure, retry once with the error appended
    - check every value against the rubric; out-of-list values are **kept as-is, and
      confidence is forced to `low` and logged** (never coerced)
    - on a second failure, raise `ClassificationFailed` so the orchestrator quarantines
      the video and increments the error count
  - Refusal and `max_tokens` stop reasons are treated as failures, never parsed.
- `tests/test_classify.py`: 8 tests covering:
  - a good response
  - malformed then good (retries exactly once)
  - malformed twice (raises, so the video gets quarantined)
  - an out-of-rubric value (flagged low, not coerced)
  - a refusal
  - the thumbnail is sent first
  - the schema is derived from the rubric
  - rubric swap

**Decisions**
- **Model: `claude-opus-5-5`, effort `low`.** Classification is a narrow judgment task.
  Low effort keeps cost down, and quality can be checked on the live run. Both can be
  overridden with the `CLAUDE_MODEL` / `CLAUDE_EFFORT` env vars.
- **Structured outputs (`output_config.format` with a JSON schema).** The API constrains
  the response to the rubric's enums. The plan's validation still runs on top as
  defence in depth, so a schema bypass or a swapped model can't silently corrupt data.
- **Server-side refusal fallback enabled** (`fallbacks: "default"`). If the primary
  model declines, the API retries on a fallback model inside the same call. If
  everything declines, the video is quarantined.

**Still to verify:** 10 real videos classified with a live key.

## Phase 4 — Metrics ✅

**Built**
- `src/metrics.py`:
  - `channel_baseline`: **median** views of the brand's last 20 videos that are at
    least 7 days old, using each video's latest metrics row.
  - `score_brand` returns, for every video:
    - `view_index = views / baseline`
    - `engagement_rate = (likes + comments) / views`
    - a `too_early` flag
  - Edge cases: no eligible videos → baseline `None` → index `None` (reported as
    "no baseline yet"); zero views → engagement `None`.
- `tests/test_metrics.py`: 8 tests covering:
  - a viral 1M-view outlier doesn't move the median
  - a median video scores exactly 1.0 ✅
  - videos under 7 days old are excluded from the baseline and flagged ✅
  - only the last 20 count
  - edge cases

**Done-when criteria met:** a median video indexes at 1.0 and recent videos are excluded.

## Phase 5 — Comment synthesis 🟡

**Built**
- `src/synthesise.py`: one Claude call per video with its top 50 comments. Returns JSON
  with four fields:
  - `recurring_themes`
  - `objections`
  - `questions`
  - `praise`

  Also returns `overall_sentiment`, capped at 5 short items per list.
- The prompt tells Claude to report patterns only, paraphrase, never quote, and never
  include names or handles.
- Comments disabled or empty → no API call, `NULL` stored (so it is not retried).
- `tests/test_synthesise.py` (5 tests):
  - themes are returned
  - disabled comments skip the call
  - a bad response raises
  - **a full dump of the DB contains no commenter names or raw text** ✅
  - disabled comments are recorded as NULL

**Privacy decision:** comments are held in memory for one API call and then discarded.
Author names and IDs are dropped at fetch time and never reach Claude or the DB. Only
the synthesised themes are stored.

**Decision:** synthesis only runs for videos published in the last 21 days, because the
digest only shows themes for recent videos. This avoids ~20 extra calls per brand for
the first-run backfill.

## Phase 6 — Report 🟡

**Built**
- `src/report.py`: `build_digest()` is deterministic (no LLM) and follows the plan's
  structure:
  - per-brand verdicts (**Outperforming** ≥1.5x median, **Underperforming** ≤0.67x)
    with an attribute line and a sentence on audience themes
  - **What's working:** attribute values whose median view index is ≥1.2x across ≥2
    videos
  - **Shift worth noting:** the most common value in the last 5 videos vs the 5 before,
    when it changed by ≥2
  - **Flagged:** low-confidence classifications, videos too early to judge, quarantined
    videos, deferred overflow
- `scripts/sample_digest.py` renders a digest from **synthetic** data into
  `data/sample.db` / `reports/sample-digest.md` so the format can be reviewed before live
  keys arrive. The dashboard's *Latest digest* tab shows it until a real one exists.
- `tests/test_report.py` (4 tests):
  - leads with the insight
  - no raw data tables
  - low-confidence videos are flagged
  - the no-baseline message
  - label fallback for a swapped rubric

**Decisions**
- **Resolved a tension in the plan.** A weekly run means nearly every video launched
  this week is under the 7-day age guard, so the headline verdicts could never apply
  to new videos. Each brand section now has two parts:
  1. **Verdicts** for videos that *crossed* the 7-day mark this week (published 7–14 days ago)
  2. **Just launched:** this week's videos with attributes and early views, clearly
     marked too early to judge

  This way every video gets reported twice: once when it launches and once when it can
  be judged.
- **Low-confidence classifications are excluded** from the "What's working" and "Shift"
  aggregates, and listed under Flagged (principle 5).
- Unknown rubric dimensions render as the bare value, so a swapped rubric still reads well.

## Phase 7 — Deliver & schedule 🟡

**Built**
- `src/deliver.py`: always writes `reports/digest-<date>.md`, which the workflow needs
  for the artifact. If `SLACK_WEBHOOK_URL` is set, it also posts, converting markdown to
  Slack mrkdwn and chunking long digests.
- `src/main.py` runs the stages in order: fetch → classify (anything unclassified,
  including leftovers from an interrupted run) → synthesise → report → deliver.
  - Fetch failure: rollback, run recorded as `failed`, exit code 1.
  - Per-video classification/synthesis failures increment `errors` and never fail the run.
  - Every classification is committed as it lands, so a crash loses at most one video's work.
- `.github/workflows/weekly.yml`:
  - Mondays 13:00 UTC, plus `workflow_dispatch`
  - secrets `YOUTUBE_API_KEY`, `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`
  - commits `data/monitor.db` + `reports/` back to the repo
  - uploads the digest as an artifact
  - `concurrency` guard so two runs never overlap
- `tests/test_deliver_and_main.py`: Slack conversion, file fallback, Slack post, and a
  **full end-to-end run against fake YouTube + fake Claude**:
  - 20 videos, 19 classified, 1 quarantined, 1 with comments disabled
  - **a second run makes zero new Claude calls and adds nothing** ✅
  - a quota failure exits 1 with state untouched ✅

**Test suite: 40 passing.**

**Still to verify:** a live local run (`.venv/bin/python -m src.main`), then a
`workflow_dispatch` run on GitHub.
