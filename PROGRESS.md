# Build Progress Log

Running log of what has been built, decisions taken, and what is still open.
Newest entries at the bottom of each phase.

## Status

| Phase | Status |
|---|---|
| 1 — Configuration & state | ✅ Done |
| 2 — Fetch | ✅ Verified live: second run found 0 new videos |
| 3 — Classify | ✅ Verified live: 300 real videos, 0 failures (OpenAI) |
| 4 — Metrics | ✅ Done, now with per-format baselines (Short / clip / episode) |
| 5 — Comment synthesis | ✅ Verified live, incl. a channel with comments disabled |
| 6 — Report | 🟡 Real digest on the dashboard; needs your review |
| 7 — Deliver & schedule | 🟡 Code and workflow written; needs a GitHub repo + secrets for a `workflow_dispatch` run |
| README | ⬜ Not started |

## Needed from you

- [x] YouTube Data API v3 key: in `.env`, verified working
- [x] LLM key: **OpenAI** (switched from Anthropic, see below), in `.env`
- [x] Channels: The Diary Of A CEO, What Now? with Trevor Noah, The Oprah Podcast
- [ ] *(optional)* Slack incoming webhook → `.env` as `SLACK_WEBHOOK_URL`
- [ ] *(Phase 7)* A GitHub repo to push to, with secrets `YOUTUBE_API_KEY`,
      `OPENAI_API_KEY` (+ optional `SLACK_WEBHOOK_URL`) and repo variable
      `RUBRIC_FILE=rubric.podcast.yaml`

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

## Change: LLM provider switched to OpenAI (30 Sep 2026)

You supplied an OpenAI key and chose to switch rather than get an Anthropic key.

**What changed**
- New `src/llm.py` is **the only file that talks to the provider**. `classify.py` and
  `synthesise.py` now build provider-neutral content parts (text and image) plus a JSON
  schema and call `llm.complete_json()`. Switching provider again means rewriting one
  ~70-line file.
- The call uses OpenAI Chat Completions with `response_format: json_schema, strict: true`,
  so the rubric enums are enforced by the API. The rubric validation stays as defence
  in depth.
- Refusals (`message.refusal`) and truncation (`finish_reason == "length"`) raise
  `LLMError` → the video is quarantined, and the run carries on.
- **Model: `gpt-5.4-mini`, `reasoning_effort=low`.** I picked it from the models your key
  can access: it's vision-capable, current and cheap. Override with `OPENAI_MODEL` /
  `OPENAI_REASONING_EFFORT`. I tested the exact call shape (image plus strict schema)
  live before writing the code.
- `requirements.txt`, `.env.example` and the workflow secrets now use `OPENAI_API_KEY`.
- Tests updated to fake the OpenAI client shape. Added tests for truncation and
  image-part format. **42 passing.**

## Change: podcast channels + podcast rubric (30 Sep 2026)

The channels are podcasts, not DTC brands, and the original rubric (offer, discount,
product thumbnail…) doesn't fit them. This is exactly the case the "rubric is
configuration" design is for:
- New `config/rubric.podcast.yaml` (version `podcast-1`) with seven dimensions:
  `content_type` (full episode / clip / short / trailer), `hook_type`, `topic`,
  `guest_type`, `title_style`, `thumbnail_treatment`, `emotional_tone`.
- Selected with `RUBRIC_FILE=rubric.podcast.yaml` in `.env`. The DTC rubric is untouched
  and still the default. **No pipeline code changed**; the report just got friendlier
  labels for the new dimensions.

**Channel resolution** (searched via the YouTube API):
- The Diary Of A CEO → `@TheDiaryOfACEO` (20M subs)
- What Now? → the dedicated channel has only one video from 2024. **Episodes post to
  `@trevornoah`**, so that's what we monitor.
- The Oprah Podcast → `@TheOprahPodcast` is empty. **Episodes post to `@Oprah`.**
- Caveat: Trevor's and Oprah's main channels also post non-podcast content. The
  `content_type` dimension makes that mix visible rather than hiding it.

## First live runs (30 Sep 2026)

**Run 1: 20 videos per channel.** 60 fetched, 60 classified (36 high / 23 medium /
1 low confidence), 0 errors, 12 YouTube quota units, about 5.5 minutes.
- Comment themes came back for DOAC and Trevor Noah. **Comments are disabled on all 20
  Oprah videos**; that was handled as designed (NULL recorded, no crash, no retry loop).

**Run 2: rerun.** 0 new videos, 0 LLM calls, 60 stats refreshed (6 units).
✅ **Phase 2 done-when met on live data.**

### What the real data exposed, and the fixes

1. **One channel median mixed Shorts with hour-long episodes.** These channels post
   mostly Shorts, so every full episode showed "22.6x channel median", and the digest
   claimed "full episode videos run at 22.6x", a fake insight. That's format mix, not
   creative quality (principle 4).
   → **Baselines are now per format bucket**, set by duration in code, never by the LLM:
   Short ≤3 min, clip 3–20 min, full episode >20 min. The 20-minute cut-off comes from
   a clean gap in the real data: no channel has anything between 20 and 30 minutes.
   Oprah's fake "34.6x" disappeared once clips and episodes were split.
2. **20 backfilled videos only covered 8–17 days on channels this busy**, so Oprah had one
   judgeable video (which was its own baseline → "1.0x, on par").
   → **Backfill raised to 100 per channel** (about 45–80 days). An existing brand below the
   backfill tops itself up automatically with a scan that skips known videos, so it stays
   rerun-safe (new test). Stats refresh now covers the last 100 per brand (2 units each).
3. **"Just launched" listed 16–19 items per brand**, the database dump the plan warns
   against.
   → Now shows the format mix in the heading, a "Mostly …" pattern line (values held by
   ≥40% of launches), and the **top 3 early leaders** with their early index vs format
   median, then "…and N more".
4. Smaller fixes:
   - At most 2 outperformers + 2 underperformers per brand.
   - "What's working" needs ≥3 videos.
   - Hidden likes → engagement shown as "likes hidden" instead of a misleading low rate.
   - Channels with comments off get one explanatory line.
   - Indexes under 0.1 show two decimals (0.04x, not 0.0x).
   - Dashboard link colour fixed for dark mode.
5. **Classification runs 6 LLM calls in parallel** (`LLM_WORKERS`); DB writes stay on
   one thread. 240 videos classified in ~2 minutes.

**Run 3: backfill top-up.** 240 new, 240 classified, **0 errors**. The DB now holds
300 videos (100 per channel, back to 9 Jul / 17 Aug / 24 Aug).

**Tests: 46 passing.**

### Early read of the real digest (for sanity, not conclusions)
- DOAC: **provocative-claim hooks with an alarming tone** dominate this week's launches,
  and expert-scientist guests run at 1.7x their format median across 38 videos.
- Trevor Noah: 14 of 16 launches are Shorts, mostly Jimmy Carr cut-downs. Question-style
  titles on the Kaepernick Shorts are the underperformers (0.2x).
- Oprah: this week is a "difficult coworkers" themed run of clips/Shorts. The 2.6-hour
  Book Club compilation is at 0.04x the full-episode median.

## Web app (30 Sep 2026) ✅

Built from `creative-monitor-design-spec.md`, styled on YouTube's visual language.
Runs at **http://127.0.0.1:8765** (`.venv/bin/python dashboard.py`).

**Channels:** Oprah removed from `brands.yaml`; its 100 videos purged from the DB. The
Slack/markdown digest was regenerated without it.

**Architecture**
- `src/digest.py` builds the digest as **structured data**: headline, per-brand
  verdicts, shift, winners, launches, trend and flags. `report.py` renders it as markdown,
  and the web app renders the same data, so the two surfaces can't disagree.
- `web/app.py` (Flask) + `web/templates/` + `web/static/app.css`. `dashboard.py`
  starts it with auto-reload.

**Screens (per the spec)**
1. **Weekly digest**
   - week navigation (‹ 23 Sep – 30 Sep ›) and channel jump chips
   - page title, then the **headline insight as a sentence**: the largest text after
     the title
   - per channel:
     - upload trend (busier / quieter / usual vs the recent weekly average)
     - **shift callout** set apart by tint and scale
     - "Now judgeable" verdict cards and "What's working"
     - "Just launched" grid of top 3 plus a pattern line
     - comments-off note
   - flagged footnote at the bottom
2. **Channel detail:** YouTube channel-page header, median views per format, format filter
   chips (formats with no videos are hidden), expanded cards grouped by week.
3. **Video detail:** YouTube watch-page layout:
   - large thumbnail linking to YouTube
   - description-style panel with a big performance indicator, the channel median, every
     tag with its dimension, and the model's rationale
   - **theme cluster**: objections (emphasised, the actionable ones), questions, praise
   - "More from this channel" rail
4. **Rubric editor:**
   - add and remove dimensions and values
   - saving writes a new version (`podcast-1` → `podcast-2`) and keeps the file's
     comments and dimension order
   - validation errors keep your draft on screen
   - shows how many videos are tagged with each version
5. Also: **Run history**, **Build log** (this file), and a **Design system** page:
   - colour and type written as intent
   - every component rendered from live data
   - all three empty/error states: no new creatives, comments disabled, failed run

**Design decisions**
- **YouTube Red = outperformance, and nothing else.** The logo mark is monochrome.
  Buttons, chips and headers are near-black and greys. Links use YouTube blue. If red
  is on screen, a creative beat its own channel's median by ≥1.5x *and* is old enough
  to judge. Videos under 7 days old get a dashed grey bar even when they're running hot.
- **Underperformance gets no colour.** A grey bar left of the baseline is enough, and
  the UI passes no judgment.
- **Performance indicator:** log scale from 0.1x to 10x, with the 1.0x baseline marker
  as the visual anchor. Always captioned "vs Short / clip / full-episode median".
- **Chips:** one neutral grey treatment, with the dimension name as a small caption. No
  per-dimension colours. Launch tiles show only the hook and topic/angle chips to keep
  the grid calm.
- Roboto (YouTube Sans isn't public), tabular figures for every number, light theme
  only (the spec warns against dark dashboards).
- YouTube patterns: 56px masthead, 240px guide rail (a drawer on mobile), chip bars,
  12px-radius 16:9 thumbnails with duration/SHORTS badges, "views · time ago" metadata.
- Thumbnails come from YouTube's public image CDN by video ID, so nothing extra is stored.

**Bugs found while checking in the browser, all fixed**
- the performance bar didn't draw (inline element)
- tiles and the player ignored 16:9 (inline links)
- rubric dimensions displayed alphabetically: Flask sorts JSON keys, and saving would
  have reordered the file
- the rubric editor echoed raw input into HTML (now escaped; drafts parsed server-side)
- avatar initials picked up "The"
- mobile masthead crowding

**Checks:** desktop and 375px mobile (no horizontal scroll). `tests/test_web.py`
(14 tests):
- every route renders; unknown channels/videos give 404; bad week dates give 400
- red appears on the outperformer
- theme cluster and comments-off state
- failed-run banner
- rubric save: bumps the version, keeps order and comments, normalises values; an
  unchanged save doesn't bump
- five validation failures leave the file untouched, and rejected input comes back escaped
- formatting helpers

**Tests: 60 passing.**

**Still open:** Phase 7 (GitHub repo + secrets for the scheduled run) and the README.
