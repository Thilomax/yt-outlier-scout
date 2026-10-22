"""
All-time breakouts of the creators in config.json: the videos that made them big.

Most-viewed isn't the same as "made them big": old videos keep collecting views,
and a creator's recent videos are seen by a much bigger audience than their early
ones. So every long-form upload is scored against its neighbours, i.e. the median
views of the videos the creator posted around the same time. A score of 10 means
the video got 10x what that creator's videos usually got back then: a breakout.

Writes creator_alltime.json with the top breakouts per creator. Refreshes itself
at most every `refresh_days` (it runs inside the daily workflow, and quietly does
nothing until it's due). Cost: about 2 quota units per 50 uploads per creator.
"""

import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from scout import ROOT, api_get, load_json, parse_duration, resolve_creators, video_details

OUT = ROOT / "creator_alltime.json"


def all_uploads(key, channel_id, limit):
    ch = api_get("channels", key, part="contentDetails,statistics", id=channel_id)["items"][0]
    playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    subs = int(ch["statistics"].get("subscriberCount", 0) or 0)
    ids, token = [], None
    while len(ids) < limit:
        params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = api_get("playlistItems", key, **params)
        ids += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:limit], subs


def breakouts(videos, cfg):
    """Score each video against the median of the `window` uploads around it."""
    window = cfg.get("neighbour_window", 20)
    videos = sorted(videos, key=lambda v: v["published"])
    out = []
    for i, v in enumerate(videos):
        lo = max(0, i - window // 2)
        hi = min(len(videos), lo + window + 1)
        lo = max(0, hi - window - 1)
        neighbours = [n["views"] for j, n in enumerate(videos[lo:hi], start=lo) if j != i]
        if len(neighbours) < 5:
            continue
        base = statistics.median(neighbours)
        if base <= 0:
            continue
        out.append({**v, "neighbour_median_views": int(base), "breakout_score": round(v["views"] / base, 1)})
    return out


def main():
    cfg_all = load_json(ROOT / "config.json", {})
    cfg = cfg_all.get("creator_alltime", {})
    if not cfg.get("enabled", True):
        return
    previous = load_json(OUT, {})
    force = "--force" in sys.argv
    if previous.get("generated") and not force:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(previous["generated"] + "T00:00:00+00:00")
        if age.days < cfg.get("refresh_days", 30):
            print(f"creator_alltime.json is {age.days} days old; next refresh after {cfg.get('refresh_days', 30)}")
            return

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("Missing YOUTUBE_API_KEY")
    creators = cfg_all.get("creators", [])
    cache = resolve_creators(key, creators)
    min_dur = cfg_all.get("min_duration_sec", 300)
    top_n = cfg.get("top_per_creator", 10)
    min_score = cfg.get("min_breakout_score", 2)

    result = {}
    for c in creators:
        name = c["name"]
        if name not in cache:
            continue
        try:
            ids, subs = all_uploads(key, cache[name]["id"], cfg.get("max_uploads_per_creator", 2000))
            details = video_details(key, ids)
        except Exception as e:
            print(f"  skip {name}: {e}", file=sys.stderr)
            continue
        videos = []
        for vid, v in details.items():
            dur = parse_duration(v["contentDetails"].get("duration"))
            if dur < min_dur:
                continue  # Shorts would drag the neighbour median around
            videos.append(
                {
                    "video_id": vid,
                    "title": v["snippet"]["title"],
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "channel": v["snippet"]["channelTitle"],
                    "creator": name,
                    "views": int(v["statistics"].get("viewCount", 0) or 0),
                    "published": v["snippet"]["publishedAt"][:10],
                    "duration_min": round(dur / 60),
                }
            )
        scored = [r for r in breakouts(videos, cfg) if r["breakout_score"] >= min_score]
        scored.sort(key=lambda r: r["breakout_score"], reverse=True)
        result[name] = {
            "subscribers": subs,
            "longform_uploads": len(videos),
            "breakouts": scored[:top_n],
        }
        print(f"  {name}: {len(videos)} long-form uploads, {len(scored)} breakouts >= {min_score}x")

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": "breakout_score = views / median views of the creator's ~20 uploads around the same time",
        "creators": result,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the workflow over this
        print(f"All-time creator step failed: {e}", file=sys.stderr)
