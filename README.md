# yt-outlier-scout

Finds YouTube **outlier videos** — videos that massively outperformed their channel's normal view count — in topics I care about. Runs every day for free on GitHub Actions.

## How it works

1. Searches the YouTube Data API for each query in `config.json` (videos from the last 45 days).
2. Keeps long-form videos only (5+ min, no Shorts), at least 3 days old, with 10k+ views.
3. For each channel, takes the **median views of its recent long-form uploads** as a baseline.
4. **Outlier score = video views ÷ channel median.** A score of 20 means the video did 20× better than that channel usually does.
5. Adds new finds to a rolling 7-day pool in `outliers.json` (each entry has a `found` date, up to `max_results` videos) and remembers them in `seen.json` so they don't repeat. The weekly Notion idea run picks from the whole pool.

Uses only the Python standard library. One run costs roughly 6,500 of the 10,000 free daily API quota units (each of the ~50 searches is 100 units, plus about 2 units per channel checked), so only one full run fits per day. The quota resets at midnight Pacific time. Every push that changes `scout.py`, `config.json` or the workflow also triggers a run, which uses up that day's quota; add `[skip ci]` to the commit message to leave it for the next scheduled run.

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
