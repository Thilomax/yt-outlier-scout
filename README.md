# yt-outlier-scout

Finds YouTube **outlier videos** — videos that massively outperformed their channel's normal view count — in topics I care about. Runs every day for free on GitHub Actions.

## How it works

1. Searches the YouTube Data API for each query in `config.json` (videos from the last 45 days), and adds YouTube's trending charts for the regions and categories under `trending` (1 quota unit each), so topics aren't limited to the search terms.
2. Keeps long-form videos only (5+ min, no Shorts), at least 3 days old, with 10k+ views.
3. For each channel, takes the **median views of its recent long-form uploads** as a baseline.
4. **Outlier score = video views ÷ channel median.** A score of 20 means the video did 20× better than that channel usually does.
5. Drops noise: channels under `min_subscribers` or with a median under `min_channel_median` (one lucky video against a median of 50 views gives absurd scores), uploads over `max_duration_sec`, non-English audio, and titles containing any `blocked_title_terms` (full movies, dubbed dramas, highlights). Description chapters are saved as `chapters`.
6. Adds new finds to a rolling 7-day pool in `outliers.json` (each entry has a `found` date, up to `max_results` videos) and remembers them in `seen.json` so they don't repeat. The weekly Notion idea run picks from the whole pool.

Then `transcripts.py` saves the transcript and top comments of every creator outlier and the top keyword outliers to `transcripts/<video_id>.txt` (settings under `transcripts` in `config.json`), so the weekly idea run can analyse how the videos are actually built instead of guessing from titles. It uses the free `youtube-transcript-api` package; comments cost 1 quota unit per video. YouTube currently blocks transcript requests from GitHub's servers; entries then get a `transcript_error`, and the file still holds the chapters and top comments. To fetch transcripts without re-running the search: **Actions → Run workflow → tick "Only fetch transcripts"**.

`creators_alltime.py` finds what made each creator in `creators` big: it goes through their whole upload history and scores every long-form video against the median of the ~20 videos they posted around the same time (`breakout_score`), so early breakouts from when they were small count as much as recent hits. The top breakouts per creator go to `creator_alltime.json`. It refreshes at most once every `refresh_days` (settings under `creator_alltime`) and costs about 2 quota units per 50 uploads.

Every outlier records the exact search (or trending chart) that found it in `query`, and `search_stats.json` logs per search per day how many videos it returned, how many were new and how many became outliers, with a few example titles (kept 60 days). That's the input for a monthly review of which searches to keep, change or drop.

`scout.py` uses only the Python standard library. One run costs roughly 6,500 of the 10,000 free daily API quota units (each of the ~50 searches is 100 units, plus about 2 units per channel checked), so only one full run fits per day. The quota resets at midnight Pacific time. Every push that changes `scout.py`, `config.json` or the workflow also triggers a run, which uses up that day's quota; add `[skip ci]` to the commit message to leave it for the next scheduled run.

## Setup

1. Get a YouTube Data API v3 key from Google Cloud Console (APIs & Services → enable *YouTube Data API v3* → Credentials → API key).
2. In this repo: **Settings → Secrets and variables → Actions → New repository secret**, name it `YT_OUTLIER_SCOUT`.
3. **Actions** tab → *Weekly outlier scout* → **Run workflow** to test it.

Run locally:

```bash
YOUTUBE_API_KEY=your_key python scout.py
```

## Tuning

Edit `config.json`: add or remove topics and search queries, or change the thresholds (`min_outlier_score`, `min_views`, `lookback_days`, …).
