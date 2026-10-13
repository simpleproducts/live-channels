"""Key-less live detection: scrape https://www.youtube.com/channel/<UC>/live and pin the live video id.

Used by build.py (bootstrap / no YT_API_KEY). A channel is live now only when ALL hold:
  * the page's canonical URL is watch?v=<id>
  * ytInitialPlayerResponse.playabilityStatus.status == "OK"   (upcoming events are "LIVE_STREAM_OFFLINE")
  * videoDetails has "isLive":true
  * "playableInEmbed":true                                      (else the channel is marked embeddable=false)
When /live shows something else (an upcoming event, nothing), the previously pinned id and the 3 newest
RSS feed entries are re-checked on their watch pages: a channel can be live while /live advertises a
scheduled event.
"""
import datetime
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RSS_FALLBACK = 3  # newest feed entries checked when /live is not live
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605", "Accept-Language": "en-US"}
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}


def fetch(url):
    for attempt in range(3):
        try:
            time.sleep(0.25)
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def started_at(html, now):
    """ISO UTC start time from 'Started streaming on Oct 1, 2023' / 'Started streaming 3 hours ago'."""
    m = re.search(r'"(?:simpleText|content)":"Started streaming ([^"]+)"', html)
    if not m:
        return None
    text = m.group(1)
    d = re.match(r"on (\w{3}) (\d{1,2}), (\d{4})", text)
    if d:
        try:
            dt = datetime.datetime.strptime("%s %s %s" % d.groups(), "%b %d %Y").replace(tzinfo=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    a = re.match(r"(\d+) (second|minute|hour|day|week|month|year)s? ago", text)
    if a:
        dt = now - datetime.timedelta(seconds=int(a.group(1)) * _UNITS[a.group(2)])
        return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def parse_watch(html, now):
    """-> dict(videoId, status, isLive, embeddable, since) for a /live or /watch page."""
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    st = re.search(r'"playabilityStatus":\{"status":"(\w+)"', html)
    i = html.find('"videoDetails":{')
    vd = i >= 0 and '"isLive":true' in html[i:i + 6000]
    return {"videoId": m.group(1) if m else None, "status": st.group(1) if st else None,
            "isLive": bool(vd), "embeddable": '"playableInEmbed":true' in html, "since": started_at(html, now)}


def evaluate(p):
    """-> 'live' | 'noembed' | 'off'"""
    if not p["videoId"] or p["status"] != "OK" or not p["isLive"]:
        return "off"
    return "live" if p["embeddable"] else "noembed"


def check_channel(uc, prev_vid=None, now=None):
    """-> {"state": live|noembed|off, "videoId", "since"} or None when the fetch failed."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    html = fetch("https://www.youtube.com/channel/%s/live" % uc)
    if html is None:
        return None
    p = parse_watch(html, now)
    state = evaluate(p)
    if state != "live":
        # /live may advertise an upcoming event while another broadcast is live: try the previous pin,
        # then the newest uploads from the channel's RSS feed.
        tried = {p["videoId"]}
        extra = [prev_vid] if prev_vid else []
        extra += rss_ids(uc)[:RSS_FALLBACK]
        for vid in extra:
            if not vid or vid in tried:
                continue
            tried.add(vid)
            h2 = fetch("https://www.youtube.com/watch?v=%s" % vid)
            if h2:
                p2 = parse_watch(h2, now)
                if evaluate(p2) == "live":
                    return {"state": "live", "videoId": vid, "since": p2["since"]}
    return {"state": state, "videoId": p["videoId"], "since": p["since"]}


def rss_ids(uc):
    xml = fetch("https://www.youtube.com/feeds/videos.xml?channel_id=%s" % uc)
    return re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", xml or "")


def apply(channel, res):
    """Update a source channel dict. Offline/upcoming keeps the previously pinned id (the app falls back
    to live_stream?channel=) but marks live=false. Returns True when something changed."""
    if res is None:  # fetch failed: leave everything as it was
        return False
    keys = ("youtubeVideoId", "embeddable", "live", "liveSince")
    before = tuple(channel.get(k) for k in keys)
    if res["state"] == "live":
        channel["youtubeVideoId"] = res["videoId"]
        channel["embeddable"] = True
        channel["live"] = True
        if res["since"]:
            channel["liveSince"] = res["since"]
        else:
            channel.pop("liveSince", None)
    else:
        if res["state"] == "noembed":
            channel["embeddable"] = False
        channel["live"] = False
        channel.pop("liveSince", None)
    return before != tuple(channel.get(k) for k in keys)


def scrape_sources(sources, workers=8):
    """sources: {filename: source dict}. Mutates channels in place; returns (#live, #changed, changed files)."""
    jobs = [(name, ch) for name, s in sources.items() for ch in s.get("channels", [])]
    now = datetime.datetime.now(datetime.timezone.utc)
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda j: check_channel(j[1]["youtubeChannelId"], j[1].get("youtubeVideoId"), now), jobs))
    changed = 0
    files = set()
    for (name, ch), res in zip(jobs, results):
        if apply(ch, res):
            changed += 1
            files.add(name)
    live = sum(1 for r in results if r and r["state"] == "live")
    noembed = sum(1 for r in results if r and r["state"] == "noembed")
    failed = sum(r is None for r in results)
    print("live scrape: %d channels, %d live now, %d live-but-not-embeddable, %d updated, %d fetch failures"
          % (len(jobs), live, noembed, changed, failed), file=sys.stderr)
    return live, changed, files
