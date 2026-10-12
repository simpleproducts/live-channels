#!/usr/bin/env python3
"""Refresh pinned live video ids for every channel, using the YouTube Data API (cheap quota first).

Why: many broadcasters block `embed/live_stream?channel=UC...` (YouTube error 150) while embedding the
current live *video id* works. So every channel carries the id of its current live broadcast
("youtubeVideoId" in sources/<CC>.json; build.py copies it to docs/).

Per run:
  1. videos.list (part=snippet,status; 50 ids per call, 1 unit each call) on all pinned ids.
     Still live -> keep (and record status.embeddable).
  2. Channels without a live pin: read the channel RSS feed
     (https://www.youtube.com/feeds/videos.xml?channel_id=UC..., no quota) and videos.list the recent
     ids, looking for liveBroadcastContent == "live".
  3. Only if still nothing: search.list eventType=live (100 units), capped at SEARCH_CAP calls per run
     (default 6). Priority: channels whose pinned broadcast just ended, then a rotation of the rest.
     Budget: 12 runs/day x (6 x 100 + ~20 videos.list) ~= 7.5k units < 10k daily quota.
Offline channels keep their previous id (the app falls back to live_stream?channel=).
Live videos that are not embeddable are recorded as "embeddable": false (build.py excludes them).

Usage:  YT_API_KEY=... [SEARCH_CAP=6] python3 -I scripts/refresh_live.py
Exits 0 quietly when YT_API_KEY is not set (the workflow then lets build.py scrape instead).
"""
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import read_source, source_files, write_source  # noqa: E402

API = "https://www.googleapis.com/youtube/v3/"
SEARCH_CAP = int(os.environ.get("SEARCH_CAP", "6"))
RSS_RECENT = 6  # newest feed entries to inspect per channel


def api(endpoint, key, **params):
    params["key"] = key
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print("  YouTube API %s error %s: %s" % (endpoint, e.code, e.read().decode("utf-8", "replace")[:300]),
              file=sys.stderr)
    except Exception as e:
        print("  YouTube API %s failed: %s" % (endpoint, e), file=sys.stderr)
    return None


def videos_info(ids, key):
    """{videoId: {"live": bool, "embeddable": bool, "channelId": str}} for the ids YouTube returned."""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        res = api("videos", key, part="snippet,status", id=",".join(ids[i:i + 50]), maxResults=50)
        for it in (res or {}).get("items", []):
            out[it["id"]] = {"live": it["snippet"].get("liveBroadcastContent") == "live",
                             "embeddable": bool(it.get("status", {}).get("embeddable", True)),
                             "channelId": it["snippet"].get("channelId")}
    return out


def rss_ids(channel_id):
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    return re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", xml)[:RSS_RECENT]


def search_live(channel_id, key):
    res = api("search", key, part="id", channelId=channel_id, eventType="live", type="video", maxResults=1)
    items = (res or {}).get("items") or []
    return items[0]["id"]["videoId"] if items else None


def main():
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key:
        return 0
    sources = {n: read_source(n) for n in source_files()}
    chans = [(n, ch) for n, s in sources.items() for ch in s["channels"]]
    dirty = set()

    def pin(name, ch, vid, emb):
        before = (ch.get("youtubeVideoId"), ch.get("embeddable"))
        ch["embeddable"] = emb
        if emb:
            ch["youtubeVideoId"] = vid
        if before != (ch.get("youtubeVideoId"), ch.get("embeddable")):
            dirty.add(name)

    # 1. pinned ids
    info = videos_info([ch["youtubeVideoId"] for _, ch in chans if ch.get("youtubeVideoId")], key)
    todo, ended = [], set()
    kept = 0
    for name, ch in chans:
        v = info.get(ch.get("youtubeVideoId") or "")
        if v and v["live"]:
            pin(name, ch, ch["youtubeVideoId"], v["embeddable"])
            kept += 1
        else:
            if ch.get("youtubeVideoId"):
                ended.add(ch["youtubeChannelId"])
            todo.append((name, ch))

    # 2. RSS feeds -> videos.list
    ucs = [ch["youtubeChannelId"] for _, ch in todo]
    with ThreadPoolExecutor(8) as ex:
        feed = dict(zip(ucs, ex.map(rss_ids, ucs)))
    info = videos_info([v for ids in feed.values() for v in ids], key)
    left = []
    via_rss = 0
    for name, ch in todo:
        hit = next((v for v in feed[ch["youtubeChannelId"]] if info.get(v, {}).get("live")), None)
        if hit:
            pin(name, ch, hit, info[hit]["embeddable"])
            via_rss += 1
        else:
            left.append((name, ch))

    # 3. capped search.list
    hour = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H")
    left.sort(key=lambda nc: (nc[1]["youtubeChannelId"] not in ended,
                              hashlib.sha1((hour + nc[1]["youtubeChannelId"]).encode()).hexdigest()))
    searched = via_search = 0
    found = {}
    for name, ch in left[:SEARCH_CAP]:
        searched += 1
        vid = search_live(ch["youtubeChannelId"], key)
        if vid:
            found[vid] = (name, ch)
    info = videos_info(list(found), key)
    for vid, (name, ch) in found.items():
        v = info.get(vid)
        if v and v["live"]:
            pin(name, ch, vid, v["embeddable"])
            via_search += 1

    for name in sorted(dirty):
        write_source(sources[name])
    print("refresh_live: %d channels, %d still live, %d found via RSS, %d via search (%d searches), %d files updated"
          % (len(chans), kept, via_rss, via_search, searched, len(dirty)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
