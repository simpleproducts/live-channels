"""Key-less live detection: scrape https://www.youtube.com/channel/<UC>/live and pin the live video id.

Used by build.py (bootstrap / no YT_API_KEY). A channel is "live now" when the page's canonical URL is
watch?v=<id> and the page contains '"isLive":true'. '"playableInEmbed"' tells whether the embed works.
"""
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605", "Accept-Language": "en-US"}


def check_channel(uc):
    """-> {"live": bool, "videoId": str|None, "embeddable": bool|None} or None when the fetch failed."""
    url = "https://www.youtube.com/channel/%s/live" % uc
    for attempt in range(3):
        try:
            time.sleep(0.25)
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return None
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    if not m or '"isLive":true' not in html:
        return {"live": False, "videoId": None, "embeddable": None}
    return {"live": True, "videoId": m.group(1), "embeddable": '"playableInEmbed":true' in html}


def apply(channel, res):
    """Update a source channel dict from a check result. Returns True when something changed.
    Offline (or failed fetch): keep the previously pinned id; the app falls back to live_stream?channel=."""
    if not res or not res["live"]:
        return False
    before = (channel.get("youtubeVideoId"), channel.get("embeddable"))
    channel["embeddable"] = res["embeddable"]
    if res["embeddable"]:
        channel["youtubeVideoId"] = res["videoId"]
    return before != (channel.get("youtubeVideoId"), channel.get("embeddable"))


def scrape_sources(sources, workers=8):
    """sources: {filename: source dict}. Mutates channels in place; returns (#live, #changed, set of changed files)."""
    jobs = [(name, ch) for name, s in sources.items() for ch in s.get("channels", [])]
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda j: check_channel(j[1]["youtubeChannelId"]), jobs))
    live = changed = 0
    files = set()
    for (name, ch), res in zip(jobs, results):
        live += bool(res and res["live"])
        if apply(ch, res):
            changed += 1
            files.add(name)
    failed = sum(r is None for r in results)
    print("live scrape: %d channels, %d live now, %d updated, %d fetch failures" % (len(jobs), live, changed, failed),
          file=sys.stderr)
    return live, changed, files
