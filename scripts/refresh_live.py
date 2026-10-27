#!/usr/bin/env python3
"""Refresh pinned live video ids for every channel, using the YouTube Data API (cheap quota first).

Why: many broadcasters block `embed/live_stream?channel=UC...` (YouTube error 150) while embedding the
current live *video id* works. So every channel carries the id of its current live broadcast
("youtubeVideoId" in sources/<CC>.json; build.py copies it to docs/).

Per run (hourly in the workflow):
  1. videos.list (part=snippet,status; 50 ids per call, 1 unit each call) on the pinned ids.
     Still live -> keep (and record status.embeddable).
  2. Channels without a live pin: read the channel RSS feed
     (https://www.youtube.com/feeds/videos.xml?channel_id=UC..., no quota) and videos.list the recent
     ids, looking for liveBroadcastContent == "live".
  3. Still nothing: the channel's live-streams playlist ("UULV" + the id without "UC"; playlistItems.list,
     1 unit) and videos.list on its newest broadcasts, for up to LIVE_PLAYLIST_CAP channels (default 100),
     so every channel gets a look every few hours. A 24/7 stream that restarted under a new id turns up
     here for a hundredth of the cost of a search.
  4. Still nothing: search.list eventType=live (100 units), capped at SEARCH_CAP calls (default 2).
  Steps 3 and 4 take channels in this order: the ones whose stream just ended (live at the last check),
  then channels on an event wall from EVENT_LEAD before its start until its end, then an hourly rotation
  of the rest.
Videos that came back neither live nor upcoming (finished broadcasts, uploads) are remembered for
FINISHED_DAYS in <BUILD_CACHE>/refresh_live_finished.json and not sent to videos.list again.
Budget: 24 runs/day x (2 x 100 + 100 + ~20 videos.list) ~= 7.7k units < 10k daily quota. After a
quotaExceeded answer the rest of the run makes no API calls, and a run that can't check the pinned ids
changes nothing.
Offline channels keep their previous id (the app falls back to live_stream?channel=).
Live videos that are not embeddable are recorded as "embeddable": false (build.py excludes them).
Every channel also gets "live": true/false (+ "liveSince"), which build.py uses to pick defaultTiles.

Usage:  YT_API_KEY=... [SEARCH_CAP=2] [LIVE_PLAYLIST_CAP=100] python3 -I scripts/refresh_live.py
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
from common import ROOT, read_source, source_files, write_source  # noqa: E402

API = "https://www.googleapis.com/youtube/v3/"
SEARCH_CAP = int(os.environ.get("SEARCH_CAP", "2"))
LIVE_PLAYLIST_CAP = int(os.environ.get("LIVE_PLAYLIST_CAP", "100"))
LIVE_PLAYLIST_DEPTH = 20  # newest broadcasts to check in a live-streams playlist
RSS_RECENT = 6  # newest feed entries to inspect per channel
EVENT_LEAD = datetime.timedelta(days=3)  # event-wall channels go first from this long before the start
FINISHED_DAYS = 30
CACHE = os.environ.get("BUILD_CACHE") or os.path.join(ROOT, ".cache")
COST = {"search": 100}  # quota units per call; every other endpoint used here costs 1
QUOTA = {"units": 0, "out": False, "failed": 0}


def api(endpoint, key, quiet=(), **params):
    """GET a Data API endpoint; None on any error. Once YouTube answers quotaExceeded, later calls return None."""
    if QUOTA["out"]:
        return None
    QUOTA["units"] += COST.get(endpoint, 1)
    params["key"] = key
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 403 and "quotaExceeded" in body:
            QUOTA["out"] = True
            print("  YouTube API quota exceeded: no more calls this run", file=sys.stderr)
        elif e.code not in quiet:
            print("  YouTube API %s error %s: %s" % (endpoint, e.code, body[:300]), file=sys.stderr)
    except Exception as e:
        print("  YouTube API %s failed: %s" % (endpoint, e), file=sys.stderr)
    return None


def load_finished(today):
    """{videoId: "YYYY-MM-DD" first seen finished}, without entries older than FINISHED_DAYS."""
    try:
        with open(os.path.join(CACHE, "refresh_live_finished.json"), encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=FINISHED_DAYS)).isoformat()
    return {v: d for v, d in seen.items() if d >= cutoff}


def save_finished(finished):
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, "refresh_live_finished.json"), "w", encoding="utf-8") as f:
        json.dump(finished, f, separators=(",", ":"), sort_keys=True)


def videos_info(ids, key, finished=None, today=None):
    """{videoId: {"live": bool, "embeddable": bool, "channelId": str}} for the ids YouTube returned.
    With `finished`, ids in it are skipped, and ids that come back neither live nor upcoming (or not at
    all: deleted or private) are added to it."""
    out = {}
    ids = [v for v in dict.fromkeys(ids) if finished is None or v not in finished]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        res = api("videos", key, part="snippet,status,liveStreamingDetails", id=",".join(chunk), maxResults=50)
        if res is None:
            QUOTA["failed"] += 1
            continue
        for it in res.get("items", []):
            # only "live" counts; "upcoming" (scheduled) and "none" do not
            content = it["snippet"].get("liveBroadcastContent")
            out[it["id"]] = {"live": content == "live",
                             "since": it.get("liveStreamingDetails", {}).get("actualStartTime"),
                             "embeddable": bool(it.get("status", {}).get("embeddable", True)),
                             "channelId": it["snippet"].get("channelId")}
            if finished is not None and content == "none":
                finished[it["id"]] = today
        if finished is not None:
            for v in chunk:
                if v not in out:
                    finished[v] = today
    return out


def _iso(ts):
    """'2026-10-12T09:01:02Z' or with fractions/offset -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def rss_ids(channel_id):
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    return re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", xml)[:RSS_RECENT]


def live_playlist_ids(channel_id, key):
    """Newest broadcasts in the channel's live-streams playlist (empty when the channel has none)."""
    res = api("playlistItems", key, quiet=(404,), part="contentDetails", playlistId="UULV" + channel_id[2:],
              maxResults=LIVE_PLAYLIST_DEPTH)
    return [it["contentDetails"]["videoId"] for it in (res or {}).get("items", [])]


def _utc(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def event_channels(sources, walls, now):
    """youtubeChannelIds on an event wall, from EVENT_LEAD before its start until its end."""
    uc = {"%s.%s" % (s["code"].lower(), ch["id"]): ch["youtubeChannelId"]
          for s in sources.values() for ch in s["channels"]}
    out = set()
    for w in walls:
        start, end = _utc(w.get("start")), _utc(w.get("end"))
        if start and end and start - EVENT_LEAD <= now < end:
            out.update(uc[g] for g in w["tiles"] if g in uc)
    return out


def search_live(channel_id, key):
    res = api("search", key, part="id", channelId=channel_id, eventType="live", type="video", maxResults=1)
    items = (res or {}).get("items") or []
    return items[0]["id"]["videoId"] if items else None


def main():
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date().isoformat()
    sources = {n: read_source(n) for n in source_files()}
    chans = [(n, ch) for n, s in sources.items() for ch in s["channels"]]
    walls = read_source("walls.json").get("walls", [])
    finished = load_finished(today)
    dirty = set()

    keys = ("youtubeVideoId", "embeddable", "live", "liveSince")

    def pin(name, ch, vid, v):
        """v: videos_info entry of a live video, or None = channel not live now (previous id is kept)."""
        before = tuple(ch.get(k) for k in keys)
        if v:
            ch["embeddable"] = v["embeddable"]
            ch["live"] = v["embeddable"]
            if v["embeddable"]:
                ch["youtubeVideoId"] = vid
            since = _iso(v.get("since"))
            if since and v["embeddable"]:
                ch["liveSince"] = since
            else:
                ch.pop("liveSince", None)
        else:
            ch["live"] = False
            ch.pop("liveSince", None)
        if before != tuple(ch.get(k) for k in keys):
            dirty.add(name)

    # 1. pinned ids
    info = videos_info([ch["youtubeVideoId"] for _, ch in chans if ch.get("youtubeVideoId")], key, finished, today)
    if QUOTA["failed"]:
        # without an answer for every pin, "not live" would be a guess: keep the catalog as it is
        print("refresh_live: YouTube API unavailable for the pinned ids; nothing changed", file=sys.stderr)
        return 0
    todo, just_ended = [], set()
    kept = 0
    for name, ch in chans:
        v = info.get(ch.get("youtubeVideoId") or "")
        if v and v["live"]:
            pin(name, ch, ch["youtubeVideoId"], v)
            kept += 1
        else:
            if ch.get("live"):  # live at the last check: a 24/7 stream may have restarted under a new id
                just_ended.add(ch["youtubeChannelId"])
            todo.append((name, ch))

    # 2. RSS feeds -> videos.list
    ucs = [ch["youtubeChannelId"] for _, ch in todo]
    with ThreadPoolExecutor(8) as ex:
        feed = dict(zip(ucs, ex.map(rss_ids, ucs)))
    info = videos_info([v for ids in feed.values() for v in ids], key, finished, today)
    left = []
    via_rss = 0
    for name, ch in todo:
        hit = next((v for v in feed[ch["youtubeChannelId"]] if info.get(v, {}).get("live")), None)
        if hit:
            pin(name, ch, hit, info[hit])
            via_rss += 1
        else:
            left.append((name, ch))

    # steps 3 and 4 take the channels most worth finding first
    hour = now.strftime("%Y%m%d%H")
    events = event_channels(sources, walls, now)
    left.sort(key=lambda nc: (0 if nc[1]["youtubeChannelId"] in just_ended else 1 if nc[1]["youtubeChannelId"] in events else 2,
                              hashlib.sha1((hour + nc[1]["youtubeChannelId"]).encode()).hexdigest()))

    # 3. live-streams playlists -> videos.list
    lists = {ch["youtubeChannelId"]: live_playlist_ids(ch["youtubeChannelId"], key) for _, ch in left[:LIVE_PLAYLIST_CAP]}
    info = videos_info([v for ids in lists.values() for v in ids], key, finished, today)
    rest = []
    via_playlist = 0
    for name, ch in left:
        uc = ch["youtubeChannelId"]
        hit = next((v for v in lists.get(uc, []) if info.get(v, {}).get("live") and info[v]["channelId"] == uc), None)
        if hit:
            pin(name, ch, hit, info[hit])
            via_playlist += 1
        else:
            rest.append((name, ch))
    left = rest

    # 4. capped search.list
    searched = via_search = 0
    found = {}
    for name, ch in left[:SEARCH_CAP]:
        searched += 1
        vid = search_live(ch["youtubeChannelId"], key)
        if vid:
            found[vid] = (name, ch)
    info = videos_info(list(found), key)
    confirmed = set()
    for vid, (name, ch) in found.items():
        v = info.get(vid)
        if v and v["live"]:
            pin(name, ch, vid, v)
            via_search += 1
            confirmed.add(id(ch))
    # everything not confirmed live this run (including channels beyond the search cap) is not live now
    for name, ch in left:
        if id(ch) not in confirmed:
            pin(name, ch, None, None)

    for name in sorted(dirty):
        write_source(sources[name])
    save_finished(finished)
    print("refresh_live: %d channels, %d still live, %d found via RSS, %d via live playlists (%d checked), "
          "%d via search (%d searches), %d files updated, %d quota units%s"
          % (len(chans), kept, via_rss, via_playlist, len(lists), via_search, searched, len(dirty), QUOTA["units"],
             ", quota exceeded" if QUOTA["out"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
