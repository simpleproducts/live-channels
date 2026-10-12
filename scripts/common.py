"""Shared helpers: reading/writing source files in a stable, hand-editable format."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources")
DOCS = os.path.join(ROOT, "docs")

CATEGORIES = ["news", "broadcast", "economy", "regional", "government", "sports", "kids", "streaming", "radio",
              "music", "religious", "culture", "entertainment", "cameras", "other"]
CHANNEL_KEYS = ["id", "name", "category", "youtubeChannelId", "youtubeVideoId", "embeddable", "live", "liveSince", "languages", "wikidata"]
UC_RE = re.compile(r"^UC[\w-]{22}$")
VIDEO_RE = re.compile(r"^[\w-]{11}$")


def source_files():
    return sorted(f for f in os.listdir(SOURCES) if re.match(r"^[A-Z]{2}\.json$", f))


def read_source(name):
    with open(os.path.join(SOURCES, name), encoding="utf-8") as f:
        return json.load(f)


def _dump(v):
    return json.dumps(v, ensure_ascii=False)


def write_source(src):
    """One channel per line so diffs stay readable and editing by hand is easy."""
    lines = ["{", ' "code": %s,' % _dump(src["code"])]
    if "languages" in src:
        lines.append(' "languages": %s,' % _dump(src["languages"]))
    lines.append(' "defaultTiles": %s,' % _dump(src.get("defaultTiles", [])))
    lines.append(' "channels": [')
    chans = src.get("channels", [])
    for i, ch in enumerate(chans):
        ordered = {k: ch[k] for k in CHANNEL_KEYS if k in ch}
        ordered.update({k: v for k, v in ch.items() if k not in ordered})
        lines.append("  " + _dump(ordered) + ("," if i < len(chans) - 1 else ""))
    lines.append(" ]")
    lines.append("}")
    path = os.path.join(SOURCES, "%s.json" % src["code"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
