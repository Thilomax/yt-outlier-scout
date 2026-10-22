"""
Fetches what the weekly idea run needs to understand *why* a video worked, not
just its title:

- the transcript (free, via youtube-transcript-api, no API key)
- the top comments, i.e. what viewers reacted to (YouTube Data API, 1 quota unit each)

Runs after scout.py. Covers every creator outlier plus the top keyword outliers.
Each video gets transcripts/<video_id>.txt, and its entry in outliers.json gets a
"transcript" path (or a "transcript_error"). Files for videos that left the pool
are deleted, so the folder never grows past one week of finds.

A failure here never breaks the scraper: the worst case is that entries are
marked with an error and the weekly run falls back to titles and chapters.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "transcripts"
API = "https://www.googleapis.com/youtube/v3"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def stamp(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fetch_transcript(api, video_id):
    """English transcript, or another language translated to English.
    Returns text with a [m:ss] marker roughly every 30 seconds."""
    try:
        snippets = api.fetch(video_id, languages=["en", "en-US", "en-GB"]).snippets
    except Exception as first:
        if type(first).__name__ != "NoTranscriptFound":
            raise
        listing = api.list(video_id)
        transcript = next(iter(listing))
        if not transcript.language_code.startswith("en") and transcript.is_translatable:
            transcript = transcript.translate("en")
        snippets = transcript.fetch().snippets

    lines, current, next_mark = [], [], 0.0
    for sn in snippets:
        if sn.start >= next_mark:
            if current:
                lines.append(" ".join(current))
            current = [f"[{stamp(sn.start)}]"]
            next_mark = sn.start + 30
        current.append(sn.text.replace("\n", " ").strip())
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def fetch_comments(key, video_id, limit):
    if not key or limit <= 0:
        return []
    params = urllib.parse.urlencode(
        {
            "part": "snippet",
            "videoId": video_id,
            "order": "relevance",
            "maxResults": limit,
            "textFormat": "plainText",
            "key": key,
        }
    )
    with urllib.request.urlopen(f"{API}/commentThreads?{params}", timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for item in data.get("items", []):
        c = item["snippet"]["topLevelComment"]["snippet"]
        text = " ".join(c.get("textDisplay", "").split())[:400]
        out.append(f"({c.get('likeCount', 0)} likes) {text}")
    return out


def main():
    from youtube_transcript_api import YouTubeTranscriptApi

    cfg = load_json(ROOT / "config.json", {}).get("transcripts", {})
    max_chars = cfg.get("max_chars", 40000)
    n_comments = cfg.get("comments_per_video", 15)
    key = os.environ.get("YOUTUBE_API_KEY")

    data = load_json(ROOT / "outliers.json", {})
    keyword = data.get("outliers", []) or []
    creators = data.get("creator_outliers", []) or []
    alltime_data = load_json(ROOT / "creator_alltime.json", {})
    alltime = [b for c in alltime_data.get("creators", {}).values() for b in c.get("breakouts", [])]
    for b in alltime:  # header fields the files use
        b.setdefault("channel_median_views", b.get("neighbour_median_views", 0))
        b.setdefault("outlier_score", b.get("breakout_score", 0))
    targets = creators + keyword[: cfg.get("top_keyword_outliers", 25)] + alltime
    if not targets:
        print("No outliers to fetch transcripts for")
        return

    OUT_DIR.mkdir(exist_ok=True)
    api = YouTubeTranscriptApi()
    fetched = failed = 0
    blocked = None

    for r in targets:
        vid = r["video_id"]
        path = OUT_DIR / f"{vid}.txt"
        rel = f"transcripts/{vid}.txt"
        if path.exists() and "## Transcript\n(unavailable" not in path.read_text(encoding="utf-8"):
            r["transcript"] = rel
            r.pop("transcript_error", None)
            continue

        header = [
            f"# {r['title']}",
            f"{r['channel']} | {r['views']:,} views vs usual {r['channel_median_views']:,} "
            f"({r['outlier_score']}x) | {r['duration_min']} min | {r['url']}",
        ]
        transcript = None
        if blocked:
            r["transcript_error"] = blocked
        else:
            try:
                transcript = fetch_transcript(api, vid)
                r.pop("transcript_error", None)
            except Exception as e:
                name = type(e).__name__
                r["transcript_error"] = name
                failed += 1
                print(f"  {vid}: {name}", file=sys.stderr)
                # YouTube blocking this machine's IP: stop trying instead of hammering it
                if name in ("RequestBlocked", "IpBlocked"):
                    blocked = f"{name} (YouTube blocked transcript requests from this server)"

        # comments use the official API, so they work even when transcripts are blocked
        if path.exists() and transcript is None:
            r["transcript"] = rel  # keep the comments-only file from an earlier run
            continue
        try:
            comments = fetch_comments(key, vid, n_comments)
        except Exception as e:
            comments = [f"(comments unavailable: {e})"]

        if transcript is None and not comments:
            continue
        if transcript is None:
            body = header + ["", "## Transcript", "(unavailable: watch the video itself)"]
        else:
            body = header + ["", "## Transcript", transcript[:max_chars]]
            if len(transcript) > max_chars:
                body.append(f"[... cut at {max_chars} characters]")
            fetched += 1
        if r.get("chapters"):
            body += ["", "## Chapters (from the description)"] + r["chapters"]
        if comments:
            body += ["", "## Top comments (by relevance)"] + comments
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        r["transcript"] = rel
        if not blocked:
            time.sleep(1.5)  # be gentle

    # keep only files for videos that are still in the pool
    keep = {f"{r['video_id']}.txt" for r in keyword + creators + alltime}
    for f in OUT_DIR.glob("*.txt"):
        if f.name not in keep:
            f.unlink()

    if blocked:
        data["transcript_error"] = blocked
    else:
        data.pop("transcript_error", None)
    (ROOT / "outliers.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if alltime:
        for b in alltime:  # drop the helper fields again
            b.pop("channel_median_views", None)
            b.pop("outlier_score", None)
        (ROOT / "creator_alltime.json").write_text(
            json.dumps(alltime_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(f"Transcripts: {fetched} new, {failed} failed, {len(targets)} targets")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the workflow over transcripts
        print(f"Transcript step failed: {e}", file=sys.stderr)
