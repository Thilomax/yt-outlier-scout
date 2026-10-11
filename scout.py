"""
yt-outlier-scout
Finds YouTube "outlier" videos: videos that got far more views than their
channel normally gets. Uses the free YouTube Data API v3 and writes the
results to outliers.json.

Outlier score = video views / median views of the channel's recent uploads.
A score of 10 means the video did 10x better than that channel usually does.
"""

import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3"
ROOT = Path(__file__).parent


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def api_get(endpoint, key, **params):
    params["key"] = key
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Surface Google's explanation (quota, key restriction, API disabled...)
        try:
            reason = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:
            reason = str(e)
        raise RuntimeError(f"{endpoint}: HTTP {e.code}: {reason}") from None


def chunks(items, size=50):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def parse_duration(iso):
    """'PT1H2M3S' -> seconds."""
    if not iso or not iso.startswith("PT"):
        return 0
    total, num = 0, ""
    for ch in iso[2:]:
        if ch.isdigit():
            num += ch
        else:
            mult = {"H": 3600, "M": 60, "S": 1}.get(ch, 0)
            total += int(num or 0) * mult
            num = ""
    return total


def search_videos(key, query, published_after, cfg):
    data = api_get(
        "search",
        key,
        part="snippet",
        q=query,
        type="video",
        order="viewCount",
        publishedAfter=published_after,
        maxResults=cfg["results_per_query"],
        relevanceLanguage=cfg.get("language", "en"),
    )
    return [item["id"]["videoId"] for item in data.get("items", [])]


def video_details(key, ids):
    out = {}
    for batch in chunks(ids):
        data = api_get(
            "videos", key, part="snippet,statistics,contentDetails", id=",".join(batch)
        )
        for v in data.get("items", []):
            out[v["id"]] = v
    return out


def channel_baselines(key, channel_ids, exclude_ids, cfg):
    """Median views of each channel's recent uploads (excluding the candidate videos)."""
    uploads, subs = {}, {}
    for batch in chunks(channel_ids):
        data = api_get(
            "channels", key, part="contentDetails,statistics", id=",".join(batch)
        )
        for c in data.get("items", []):
            uploads[c["id"]] = c["contentDetails"]["relatedPlaylists"]["uploads"]
            subs[c["id"]] = int(c["statistics"].get("subscriberCount", 0) or 0)

    baselines = {}
    for cid, playlist in uploads.items():
        try:
            data = api_get(
                "playlistItems",
                key,
                part="contentDetails",
                playlistId=playlist,
                maxResults=cfg["baseline_videos"],
            )
        except Exception as e:  # deleted/private playlists etc.
            print(f"  skip channel {cid}: {e}", file=sys.stderr)
            continue
        recent = [
            it["contentDetails"]["videoId"]
            for it in data.get("items", [])
            if it["contentDetails"]["videoId"] not in exclude_ids
        ]
        if len(recent) < cfg["min_baseline_videos"]:
            continue
        stats = video_details(key, recent)
        views = [
            int(v["statistics"].get("viewCount", 0) or 0)
            for v in stats.values()
            # only long-form uploads count toward the baseline, Shorts skew it
            if parse_duration(v["contentDetails"].get("duration")) >= cfg["min_duration_sec"]
        ]
        if len(views) >= cfg["min_baseline_videos"]:
            baselines[cid] = statistics.median(views)
    return baselines, subs


def resolve_creators(key, creators):
    """Map creator names to channel IDs. Results are cached in creators_cache.json
    so the expensive search (100 quota units) only happens once per creator.
    A creator in config.json can pin an exact channel with "id"."""
    cache_path = ROOT / "creators_cache.json"
    cache = load_json(cache_path, {})
    for c in creators:
        name = c["name"]
        if c.get("id"):
            cache[name] = {"id": c["id"], "title": cache.get(name, {}).get("title", name), "pinned": True}
            continue
        if c.get("handle"):
            if cache.get(name, {}).get("handle") == c["handle"]:
                continue
            data = api_get("channels", key, part="snippet", forHandle=c["handle"])
            items = data.get("items", [])
            if items:
                cache[name] = {"id": items[0]["id"], "title": items[0]["snippet"]["title"], "handle": c["handle"]}
            else:
                print(f"  no channel found for handle {c['handle']}", file=sys.stderr)
            continue
        if name in cache:
            continue
        data = api_get("search", key, part="snippet", q=name, type="channel", maxResults=1)
        items = data.get("items", [])
        if items:
            cache[name] = {
                "id": items[0]["snippet"]["channelId"],
                "title": items[0]["snippet"]["channelTitle"],
            }
        else:
            print(f"  no channel found for {name}", file=sys.stderr)
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cache


def creator_outliers(key, creators, cache, seen, now, cfg):
    """Recent videos from chosen creators that beat that creator's own median."""
    lookback = timedelta(days=cfg.get("creator_lookback_days", 90))
    min_age = timedelta(days=cfg["min_age_days"])
    min_score = cfg.get("creator_min_score", 1.5)
    core = {c["name"] for c in creators if c.get("core")}
    by_id = {cache[c["name"]]["id"]: c["name"] for c in creators if c["name"] in cache}

    results = []
    for batch in chunks(list(by_id)):
        data = api_get("channels", key, part="contentDetails,statistics", id=",".join(batch))
        for ch in data.get("items", []):
            name = by_id[ch["id"]]
            subs = int(ch["statistics"].get("subscriberCount", 0) or 0)
            playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
            try:
                items = api_get(
                    "playlistItems", key, part="contentDetails",
                    playlistId=playlist, maxResults=cfg["baseline_videos"],
                ).get("items", [])
            except Exception as e:
                print(f"  skip creator {name}: {e}", file=sys.stderr)
                continue
            vids = video_details(key, [it["contentDetails"]["videoId"] for it in items])
            longform = {
                vid: v for vid, v in vids.items()
                if parse_duration(v["contentDetails"].get("duration")) >= cfg["min_duration_sec"]
            }
            views = [int(v["statistics"].get("viewCount", 0) or 0) for v in longform.values()]
            if len(views) < cfg["min_baseline_videos"]:
                continue
            base = statistics.median(views)
            if not base:
                continue
            for vid, v in longform.items():
                published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
                age = now - published
                if vid in seen or age < min_age or age > lookback:
                    continue
                score = int(v["statistics"].get("viewCount", 0) or 0) / base
                if score < min_score:
                    continue
                results.append(
                    {
                        "video_id": vid,
                        "title": v["snippet"]["title"],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "channel": v["snippet"]["channelTitle"],
                        "creator": name,
                        "core_vibe": name in core,
                        "subscribers": subs,
                        "views": int(v["statistics"]["viewCount"]),
                        "channel_median_views": int(base),
                        "outlier_score": round(score, 1),
                        "published": v["snippet"]["publishedAt"][:10],
                        "duration_min": round(parse_duration(v["contentDetails"]["duration"]) / 60),
                    }
                )
    results.sort(key=lambda r: r["outlier_score"], reverse=True)
    return results[: cfg.get("max_creator_results", 15)]


def write_error(message):
    """Record a failed run in outliers.json so it is visible without the Actions logs.
    If an earlier run left good results, keep them and only note the failure, so a
    temporary problem (e.g. API quota) doesn't wipe the week's ideas."""
    previous = load_json(ROOT / "outliers.json", {})
    if previous.get("outliers") or previous.get("creator_outliers"):
        previous["last_error"] = message
        previous.pop("error", None)
        out = previous
    else:
        out = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "count": 0,
            "error": message,
            "outliers": [],
        }
    (ROOT / "outliers.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def main():
    try:
        run()
    except Exception as e:
        write_error(str(e))
        sys.exit(f"Scout failed: {e}")


def run():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("Missing YOUTUBE_API_KEY secret")

    cfg = load_json(ROOT / "config.json", None)
    if cfg is None:
        raise RuntimeError("config.json missing or invalid")
    seen = set(load_json(ROOT / "seen.json", []))

    now = datetime.now(timezone.utc)
    published_after = (now - timedelta(days=cfg["lookback_days"])).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # 1. search every topic
    candidates = {}  # video id -> topic
    errors = []
    for topic in cfg["topics"]:
        for query in topic["queries"]:
            try:
                ids = search_videos(key, query, published_after, cfg)
            except Exception as e:
                print(f"search failed for '{query}': {e}", file=sys.stderr)
                errors.append(str(e))
                continue
            for vid in ids:
                if vid not in seen:
                    candidates.setdefault(vid, topic["name"])
    total_queries = sum(len(t["queries"]) for t in cfg["topics"])
    if errors and len(errors) == total_queries:
        raise RuntimeError(f"All searches failed. First error: {errors[0]}")
    print(f"{len(candidates)} new candidate videos")

    # 2. details + filter (long-form only, old enough to judge, enough views)
    details = video_details(key, list(candidates))
    min_age = timedelta(days=cfg["min_age_days"])
    kept = {}
    for vid, v in details.items():
        published = datetime.fromisoformat(
            v["snippet"]["publishedAt"].replace("Z", "+00:00")
        )
        views = int(v["statistics"].get("viewCount", 0) or 0)
        duration = parse_duration(v["contentDetails"].get("duration"))
        if (
            duration >= cfg["min_duration_sec"]
            and now - published >= min_age
            and views >= cfg["min_views"]
        ):
            kept[vid] = v
    print(f"{len(kept)} long-form videos after filtering")

    # 3. channel baselines
    channel_ids = sorted({v["snippet"]["channelId"] for v in kept.values()})
    baselines, subs = channel_baselines(key, channel_ids, set(kept), cfg)

    # 4. score
    results = []
    for vid, v in kept.items():
        cid = v["snippet"]["channelId"]
        base = baselines.get(cid)
        if not base:
            continue
        views = int(v["statistics"]["viewCount"])
        score = views / base
        if score < cfg["min_outlier_score"]:
            continue
        results.append(
            {
                "video_id": vid,
                "title": v["snippet"]["title"],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "channel": v["snippet"]["channelTitle"],
                "subscribers": subs.get(cid, 0),
                "views": views,
                "channel_median_views": int(base),
                "outlier_score": round(score, 1),
                "published": v["snippet"]["publishedAt"][:10],
                "duration_min": round(parse_duration(v["contentDetails"]["duration"]) / 60),
                "topic": candidates[vid],
            }
        )

    results.sort(key=lambda r: r["outlier_score"], reverse=True)
    results = results[: cfg["max_results"]]

    # 5. creators he likes (an extra source, never the only one)
    creator_results, creator_error = [], None
    creators = cfg.get("creators", [])
    if creators:
        try:
            cache = resolve_creators(key, creators)
            creator_results = creator_outliers(key, creators, cache, seen, now, cfg)
        except Exception as e:  # keyword results still get written
            creator_error = str(e)
            print(f"creator scouting failed: {e}", file=sys.stderr)

    if not results and not creator_results:
        # nothing new this run: keep last week's file instead of wiping it
        previous = load_json(ROOT / "outliers.json", {})
        if previous.get("outliers") or previous.get("creator_outliers"):
            previous["note"] = f"No new outliers on {now.strftime('%Y-%m-%d')}; kept previous results"
            (ROOT / "outliers.json").write_text(json.dumps(previous, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print("No new outliers; kept previous results")
            return

    out = {
        "generated": now.strftime("%Y-%m-%d"),
        "count": len(results),
        "outliers": results,
        "creator_outliers": creator_results,
    }
    if creator_error:
        out["creator_error"] = creator_error
    (ROOT / "outliers.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    seen.update(r["video_id"] for r in results)
    seen.update(r["video_id"] for r in creator_results)
    (ROOT / "seen.json").write_text(json.dumps(sorted(seen), indent=0) + "\n", encoding="utf-8")
    print(f"Wrote {len(results)} keyword outliers and {len(creator_results)} creator outliers")


if __name__ == "__main__":
    main()
