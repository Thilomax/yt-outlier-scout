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


def chapters(description):
    """Timestamped chapter lines from a description ('0:00 Intro', '12:30 - The test').
    They are the creator's own outline of the video's beats. Costs no API quota."""
    out = []
    for line in description.splitlines():
        line = line.strip()
        stamp = line.split(" ", 1)[0]
        parts = stamp.split(":")
        if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts) and " " in line:
            out.append(line[:120])
    return out[:40] if len(out) >= 3 else []


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


CATEGORY_NAMES = {
    "1": "Film & Animation", "2": "Autos", "10": "Music", "15": "Pets & Animals",
    "17": "Sports", "19": "Travel", "20": "Gaming", "22": "People & Blogs",
    "23": "Comedy", "24": "Entertainment", "25": "News", "26": "Howto & Style",
    "27": "Education", "28": "Science & Tech",
}


def trending_videos(key, region, category):
    """YouTube's own 'most popular' chart for one region and category. Costs 1 quota
    unit, and brings in topics the keyword searches would never ask for."""
    data = api_get(
        "videos", key, part="id", chart="mostPopular",
        regionCode=region, videoCategoryId=category, maxResults=50,
    )
    return [item["id"] for item in data.get("items", [])]


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
                        "chapters": chapters(v["snippet"].get("description", "")),
                    }
                )
    results.sort(key=lambda r: r["outlier_score"], reverse=True)
    return results[: cfg.get("max_creator_results", 15)]


def is_noise(r, cfg):
    """True for keyword finds that are almost never useful: tiny channels (one lucky
    video against a median of 50 views gives absurd scores), full movies and dubbed
    dramas, sports highlights, and very long uploads."""
    if r.get("subscribers", 0) < cfg.get("min_subscribers", 0):
        return True
    if r.get("channel_median_views", 0) < cfg.get("min_channel_median", 0):
        return True
    if r.get("duration_min", 0) * 60 > cfg.get("max_duration_sec", 10**9):
        return True
    title = r.get("title", "").lower()
    return any(term.lower() in title for term in cfg.get("blocked_title_terms", []))


def wrong_language(v, cfg):
    """Skip videos whose audio language is set and isn't the configured one."""
    want = cfg.get("language", "en")
    lang = v["snippet"].get("defaultAudioLanguage") or v["snippet"].get("defaultLanguage")
    return bool(lang) and not lang.lower().startswith(want)


def merge_pool(previous, field, new_items, now, cfg, noise_filter=False):
    """Previous finds still inside the pool window, plus today's new ones, best first."""
    cutoff = (now - timedelta(days=cfg.get("pool_days", 7))).strftime("%Y-%m-%d")
    fallback = previous.get("generated", now.strftime("%Y-%m-%d"))
    pool = {}
    for r in previous.get(field, []) or []:
        r.setdefault("found", fallback)
        if noise_filter and is_noise(r, cfg):
            continue
        if r["found"] > cutoff:
            pool[r["video_id"]] = r
    for r in new_items:
        pool[r["video_id"]] = r
    return sorted(pool.values(), key=lambda r: r["outlier_score"], reverse=True)


def write_search_stats(today, stats, results):
    """Daily log of what every search brought in, kept for 60 days, for the monthly
    search review: which searches find good outliers and which only bring junk."""
    path = ROOT / "search_stats.json"
    log = load_json(path, {"days": []})
    days = [d for d in log.get("days", []) if d.get("date") != today]
    for query, s in stats.items():
        s["examples"] = [
            f"{r['title'][:80]} ({r['outlier_score']}x)"
            for r in results if r.get("query") == query
        ][:3]
    days.append({"date": today, "searches": stats})
    days = sorted(days, key=lambda d: d["date"])[-60:]
    path.write_text(json.dumps({"days": days}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def write_error(message):
    """Record a failed run in outliers.json so it is visible without the Actions logs.
    If an earlier run left good results, keep them and only note the failure, so a
    temporary problem (e.g. API quota) doesn't wipe the week's pool."""
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
    found_by = {}  # video id -> the exact search (or trending chart) that found it
    stats = {}  # search -> {"returned", "new", "outliers"}, for the monthly search review
    errors = []
    for topic in cfg["topics"]:
        for query in topic["queries"]:
            try:
                ids = search_videos(key, query, published_after, cfg)
            except Exception as e:
                print(f"search failed for '{query}': {e}", file=sys.stderr)
                errors.append(str(e))
                continue
            fresh = [vid for vid in ids if vid not in seen]
            stats[query] = {"returned": len(ids), "new": len(fresh), "outliers": 0}
            for vid in fresh:
                candidates.setdefault(vid, topic["name"])
                found_by.setdefault(vid, query)
    total_queries = sum(len(t["queries"]) for t in cfg["topics"])
    if errors and len(errors) == total_queries:
        raise RuntimeError(f"All searches failed. First error: {errors[0]}")

    # 1b. YouTube's trending charts, for topics outside the search terms
    trend = cfg.get("trending", {})
    for region in trend.get("regions", []):
        for cat in trend.get("categories", []):
            try:
                ids = trending_videos(key, region, str(cat))
            except Exception as e:  # some categories have no chart in some regions
                print(f"trending {region}/{cat} failed: {e}", file=sys.stderr)
                continue
            label = f"Trending: {CATEGORY_NAMES.get(str(cat), cat)}"
            source = f"trending {region} / {CATEGORY_NAMES.get(str(cat), cat)}"
            fresh = [vid for vid in ids if vid not in seen]
            stats[source] = {"returned": len(ids), "new": len(fresh), "outliers": 0}
            for vid in fresh:
                candidates.setdefault(vid, label)
                found_by.setdefault(vid, source)
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
            and not wrong_language(v, cfg)
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
                "query": found_by.get(vid, ""),
                "chapters": chapters(v["snippet"].get("description", "")),
            }
        )

    results = [r for r in results if not is_noise(r, cfg)]
    results.sort(key=lambda r: r["outlier_score"], reverse=True)
    results = results[: cfg["max_results"]]
    for r in results:
        if r["query"] in stats:
            stats[r["query"]]["outliers"] += 1

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

    # 6. merge into the rolling pool: keep finds from the last `pool_days` days so a
    # daily scraper builds up a full week of candidates for the weekly Notion run
    today = now.strftime("%Y-%m-%d")
    for r in results + creator_results:
        r["found"] = today
    previous = load_json(ROOT / "outliers.json", {})
    outliers_pool = merge_pool(previous, "outliers", results, now, cfg, noise_filter=True)[
        : cfg["max_results"]
    ]
    creators_pool = merge_pool(previous, "creator_outliers", creator_results, now, cfg)[
        : cfg.get("max_creator_results", 15)
    ]

    out = {
        "generated": today,
        "pool_days": cfg.get("pool_days", 7),
        "new_today": len(results) + len(creator_results),
        "count": len(outliers_pool),
        "outliers": outliers_pool,
        "creator_outliers": creators_pool,
    }
    if creator_error:
        out["creator_error"] = creator_error
    (ROOT / "outliers.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_search_stats(now.strftime("%Y-%m-%d"), stats, results)
    seen.update(r["video_id"] for r in results)
    seen.update(r["video_id"] for r in creator_results)
    (ROOT / "seen.json").write_text(json.dumps(sorted(seen), indent=0) + "\n", encoding="utf-8")
    print(
        f"{len(results)} new keyword and {len(creator_results)} new creator outliers; "
        f"pool now {len(outliers_pool)} + {len(creators_pool)}"
    )


if __name__ == "__main__":
    main()
