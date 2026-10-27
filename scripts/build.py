#!/usr/bin/env python3
"""Build docs/ from sources/: validate, enrich from Wikidata/Wikipedia, write the published catalog.

defaultTiles in docs/ are recomputed every build from the channels that were live at the last check
(sources' "defaultTiles" is only a preference order).

Usage:  python3 -I scripts/build.py [--offline] [--check] [--no-scrape]
  --offline    never hit the network (use cache / previously published enrichment only)
  --check      validate sources only, write nothing
  --no-scrape  skip the key-less live scrape (use when refresh_live.py with YT_API_KEY already ran)

Featured walls: sources/walls.json is validated and published in docs/index.json as "walls" (tiles that
are not live are dropped, walls with fewer than 2 live tiles or an end in the past are left out).

Live pinning: unless --no-scrape/--offline, every channel's /live page is fetched; when it is live and
embeddable its video id is written to sources/<CC>.json as "youtubeVideoId" (+ "embeddable"). Offline
channels keep their previous id. Channels whose live video is not embeddable ("embeddable": false) are
left out of docs/.

Env:   BUILD_CACHE  directory for HTTP response cache (default: <repo>/.cache)
       CACHE_TTL_DAYS  how long cached Wikidata/Wikipedia responses stay fresh (default 7)

Stdlib only.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CATEGORIES, DOCS, ROOT, UC_RE, VIDEO_RE, read_source, source_files, write_source  # noqa: E402
import live  # noqa: E402

UA = "LiveChannelsCatalog/1.0 (https://github.com/simpleproducts/live-channels)"
SPARQL = "https://query.wikidata.org/sparql"
WD_API = "https://www.wikidata.org/w/api.php"
CACHE = os.environ.get("BUILD_CACHE") or os.path.join(ROOT, ".cache")
TTL = float(os.environ.get("CACHE_TTL_DAYS", "7")) * 86400
OFFLINE = "--offline" in sys.argv
SCHEMA = 2
WALLS = os.path.join(ROOT, "sources", "walls.json")
WALL_TILES_MAX = 9
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"
DEGRADED = False  # set when Wikidata could not be reached; previously published enrichment is reused

# App locales -> Wikidata language-code fallback chain
LOCALES = {
    "en": ["en"], "es": ["es"], "zh-Hans": ["zh-hans", "zh-cn", "zh"], "ja": ["ja"], "fr": ["fr"],
    "pt-BR": ["pt-br", "pt"], "ko": ["ko"], "it": ["it"], "ru": ["ru"], "zh-Hant": ["zh-hant", "zh-tw", "zh-hk", "zh"],
    "ar": ["ar"], "tr": ["tr"], "nl": ["nl"], "pl": ["pl"], "sv": ["sv"], "id": ["id"], "de": ["de"], "ms": ["ms"],
}
WD_LANGS = sorted({c for chain in LOCALES.values() for c in chain})
# App locale -> (wiki site id, URL host, optional variant path)
WIKIS = {
    "en": ("enwiki", "en", None), "es": ("eswiki", "es", None), "zh-Hans": ("zhwiki", "zh", "zh-hans"),
    "ja": ("jawiki", "ja", None), "fr": ("frwiki", "fr", None), "pt-BR": ("ptwiki", "pt", None),
    "ko": ("kowiki", "ko", None), "it": ("itwiki", "it", None), "ru": ("ruwiki", "ru", None),
    "zh-Hant": ("zhwiki", "zh", "zh-hant"), "ar": ("arwiki", "ar", None), "tr": ("trwiki", "tr", None),
    "nl": ("nlwiki", "nl", None), "pl": ("plwiki", "pl", None), "sv": ("svwiki", "sv", None),
    "id": ("idwiki", "id", None), "de": ("dewiki", "de", None), "ms": ("mswiki", "ms", None),
}


# ---------------------------------------------------------------- HTTP + cache
def _cache_path(key):
    return os.path.join(CACHE, hashlib.sha1(key.encode()).hexdigest() + ".json")


def http_json(url, data=None, key=None):
    """GET (or POST form `data`) returning parsed JSON, cached on disk. Returns None on failure."""
    key = key or (url + "|" + (data or ""))
    path = _cache_path(key)
    if os.path.exists(path) and (OFFLINE or time.time() - os.path.getmtime(path) < TTL):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if OFFLINE:
        return None
    headers = {"User-Agent": UA, "Accept": "application/json, application/sparql-results+json"}
    body = data.encode() if data else None
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=90) as r:
                out = json.load(r)
            os.makedirs(CACHE, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            return out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(3 * (attempt + 1) if e.code != 429 else 30)
        except Exception:
            time.sleep(3 * (attempt + 1))
    print("WARN: request failed: %s" % url[:120], file=sys.stderr)
    return None


def sparql(query):
    res = http_json(SPARQL, data=urllib.parse.urlencode({"query": query, "format": "json"}), key="sparql|" + query)
    return res["results"]["bindings"] if res else None


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- Wikidata
def match_youtube_ids(ucs):
    """{UC id: QID} via P2397. When several items share a channel, the one with most sitelinks wins."""
    best = {}
    global DEGRADED
    for batch in chunks(sorted(ucs), 150):
        q = ("SELECT ?yt ?item ?links WHERE { VALUES ?yt { %s } ?item wdt:P2397 ?yt ; wikibase:sitelinks ?links . }"
             % " ".join('"%s"' % u for u in batch))
        rows = sparql(q)
        if rows is None:
            DEGRADED = True
            continue
        for b in rows:
            yt, qid, links = b["yt"]["value"], b["item"]["value"].rsplit("/", 1)[1], int(b["links"]["value"])
            if yt not in best or links > best[yt][1] or (links == best[yt][1] and qid < best[yt][0]):
                best[yt] = (qid, links)
    return {k: v[0] for k, v in best.items()}


def country_items(codes):
    """{ISO alpha-2: QID} via P297."""
    # sovereign states first; territories such as Hong Kong (no Q6256) fall back to any P297 holder
    q = ("SELECT ?code ?c ?sovereign WHERE { VALUES ?code { %s } ?c wdt:P297 ?code . "
         "FILTER NOT EXISTS { ?c wdt:P576 [] } "
         "BIND(EXISTS { ?c wdt:P31 wd:Q6256 } AS ?sovereign) }" % " ".join('"%s"' % c for c in sorted(codes)))
    best = {}
    for b in sparql(q) or []:
        code, qid = b["code"]["value"], b["c"]["value"].rsplit("/", 1)[1]
        rank = (b["sovereign"]["value"] != "true", int(qid[1:]))
        if code not in best or rank < best[code][0]:
            best[code] = (rank, qid)
    return {k: v[1] for k, v in best.items()}


def get_entities(qids, props):
    out = {}
    for batch in chunks(sorted(set(qids)), 50):
        url = WD_API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "format": "json", "ids": "|".join(batch), "props": props,
            "languages": "|".join(WD_LANGS + ["pt", "hi", "ur", "ca", "eu", "gl"])})
        res = http_json(url)
        if res and "entities" in res:
            out.update({k: v for k, v in res["entities"].items() if "missing" not in v})
        else:
            global DEGRADED
            DEGRADED = True
    return out


def pick(texts, chain):
    for code in chain:
        t = texts.get(code)
        # never accept language-fallback values (they would be English text under another locale)
        if t and t.get("value") and t.get("language", code) == code and "for-language" not in t:
            return t["value"]
    return None


def claim_values(ent, prop):
    """Main-snak values, preferred rank first, deprecated dropped."""
    cl = [c for c in ent.get("claims", {}).get(prop, []) if c.get("rank") != "deprecated"]
    cl.sort(key=lambda c: 0 if c.get("rank") == "preferred" else 1)
    vals = []
    for c in cl:
        dv = c.get("mainsnak", {}).get("datavalue")
        if not dv:
            continue
        v = dv["value"]
        vals.append(v["id"] if isinstance(v, dict) and "id" in v else v)
    return vals


def commons_png(filename):
    name = filename.replace(" ", "_")
    return "https://commons.wikimedia.org/wiki/Special:FilePath/%s?width=256" % urllib.parse.quote(name)


def first_sentence(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"^(.+?[.!?。！？])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= 300 else s[:297].rstrip() + "…"


def wikipedia_summary(host, title):
    url = "https://%s.wikipedia.org/api/rest_v1/page/summary/%s" % (host, urllib.parse.quote(title.replace(" ", "_"), safe=""))
    res = http_json(url)
    return first_sentence(res.get("extract")) if res and res.get("extract") else None


# ---------------------------------------------------------------- validation
def validate(sources):
    errors = []
    seen_ids = set()
    for name, src in sources.items():
        cc = src.get("code")
        if name != "%s.json" % cc or not re.match(r"^[A-Z]{2}$", cc or ""):
            errors.append("%s: code must be ISO alpha-2 uppercase and match the file name" % name)
        local = set()
        for i, ch in enumerate(src.get("channels", [])):
            where = "%s channel #%d (%s)" % (name, i + 1, ch.get("id"))
            cid = ch.get("id")
            if not cid or not re.match(r"^[A-Za-z0-9_-]+$", cid):
                errors.append("%s: id must be [A-Za-z0-9_-]+" % where)
            if cid in local:
                errors.append("%s: duplicate id" % where)
            local.add(cid)
            gid = "%s.%s" % ((cc or "").lower(), cid)
            if gid in seen_ids:
                errors.append("%s: duplicate global id %s" % (where, gid))
            seen_ids.add(gid)
            if not ch.get("name"):
                errors.append("%s: missing name" % where)
            if ch.get("category") not in CATEGORIES:
                errors.append("%s: invalid category %r" % (where, ch.get("category")))
            if not UC_RE.match(ch.get("youtubeChannelId") or ""):
                errors.append("%s: youtubeChannelId must match ^UC[\\w-]{22}$" % where)
            if ch.get("live") is not None and not isinstance(ch["live"], bool):
                errors.append("%s: live must be true/false" % where)
            if ch.get("embeddable") is not None and not isinstance(ch["embeddable"], bool):
                errors.append("%s: embeddable must be true/false" % where)
            vid = ch.get("youtubeVideoId")
            if vid is not None and not VIDEO_RE.match(vid):
                errors.append("%s: youtubeVideoId must be an 11-char video id" % where)
            if ch.get("wikidata") is not None and not re.match(r"^Q\d+$", ch["wikidata"]):
                errors.append("%s: wikidata must look like Q123" % where)
            for lang in ch.get("languages", []):
                if not re.match(r"^[a-z]{2}$", lang):
                    errors.append("%s: languages must be ISO 639-1 codes" % where)
        for t in src.get("defaultTiles", []):
            if t not in local:
                errors.append("%s: defaultTiles entry %r is not a channel id" % (name, t))
    return errors


def read_walls():
    if not os.path.exists(WALLS):
        return []
    with open(WALLS, encoding="utf-8") as f:
        return json.load(f).get("walls", [])


def parse_utc(v):
    return datetime.datetime.strptime(v, ISO_UTC).replace(tzinfo=datetime.timezone.utc)


def validate_walls(walls, sources):
    """sources/walls.json: every tile must be a channel id (<cc>.<id>) of the catalog."""
    errors = []
    gids = {"%s.%s" % (s["code"].lower(), ch["id"]) for s in sources.values() for ch in s.get("channels", [])}
    codes = {s["code"] for s in sources.values()}
    seen = set()
    allowed = {"id", "title", "tiles", "countries", "start", "end", "featured"}
    for i, w in enumerate(walls):
        where = "walls.json wall #%d (%s)" % (i + 1, w.get("id"))
        wid = w.get("id")
        if not wid or not re.match(r"^[a-z0-9-]+$", wid):
            errors.append("%s: id must be [a-z0-9-]+" % where)
        if wid in seen:
            errors.append("%s: duplicate id" % where)
        seen.add(wid)
        extra = set(w) - allowed
        if extra:
            errors.append("%s: unknown keys %s" % (where, sorted(extra)))
        title = w.get("title")
        if not isinstance(title, dict) or set(title) != set(LOCALES) or not all(isinstance(v, str) and v.strip() for v in title.values()):
            errors.append("%s: title must have a non-empty string for each of the %d locales" % (where, len(LOCALES)))
        tiles = w.get("tiles")
        if not isinstance(tiles, list) or not tiles or len(tiles) > WALL_TILES_MAX or len(set(tiles)) != len(tiles):
            errors.append("%s: tiles must be 1-%d distinct channel ids" % (where, WALL_TILES_MAX))
        for t in tiles or []:
            if t not in gids:
                errors.append("%s: tile %r is not a channel id" % (where, t))
        for cc in w.get("countries", []):
            if cc not in codes:
                errors.append("%s: country %r has no source" % (where, cc))
        for k in ("start", "end"):
            if k in w:
                try:
                    parse_utc(w[k])
                except (TypeError, ValueError):
                    errors.append("%s: %s must be ISO UTC like 2026-11-03T22:00:00Z" % (where, k))
        if "start" in w and "end" in w and not errors and parse_utc(w["start"]) >= parse_utc(w["end"]):
            errors.append("%s: start must be before end" % where)
        if "featured" in w and not isinstance(w["featured"], bool):
            errors.append("%s: featured must be true/false" % where)
    return errors


def build_walls(walls, live_gids, now):
    """Published walls: tiles in order, non-live (or unpublished) channels dropped; walls that ended or keep
    fewer than 2 live tiles are left out."""
    out = []
    for w in walls:
        if "end" in w and parse_utc(w["end"]) <= now:
            print("  wall %s left out: ended" % w["id"], file=sys.stderr)
            continue
        tiles = [t for t in w["tiles"] if t in live_gids]
        if len(tiles) < 2:
            print("  wall %s left out: only %d live tiles" % (w["id"], len(tiles)), file=sys.stderr)
            continue
        rec = {"id": w["id"], "title": {loc: w["title"][loc] for loc in LOCALES}, "tiles": tiles}
        if w.get("countries"):
            rec["countries"] = w["countries"]
        for k in ("start", "end"):
            if k in w:
                rec[k] = w[k]
        rec["featured"] = w.get("featured", False)
        out.append(rec)
    return out


# ---------------------------------------------------------------- build
def pick_defaults(src, excluded, now, langs, n=9):
    """Up to 9 channels that were genuinely live (status OK, embeddable) at the last live check.
    Order: news first, then channels in the country's main language, then 24/7 streams (live for 20h+), then the hand-written "defaultTiles"
    preference order, then source order."""
    pref = {t: i for i, t in enumerate(src.get("defaultTiles", []))}
    main_lang = (src.get("languages") or ["en"])[0]
    cands = []
    for i, ch in enumerate(src["channels"]):
        if ch["id"] in excluded or ch.get("live") is not True:
            continue
        always_on = False
        if ch.get("liveSince"):
            try:
                since = datetime.datetime.strptime(ch["liveSince"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                always_on = (now - since).total_seconds() >= 20 * 3600
            except ValueError:
                pass
        cands.append((ch["category"] != "news", main_lang not in langs.get(ch["id"], [main_lang]), not always_on, pref.get(ch["id"], 999), i, ch["id"]))
    if not cands:
        # nothing live at the last check (night in that time zone, or a country whose broadcasters only
        # stream events): fall back to the preference order, then news channels, so the list is never empty
        ids = [t for t in src.get("defaultTiles", []) if t not in excluded]
        ids += [ch["id"] for ch in src["channels"] if ch["category"] == "news" and ch["id"] not in excluded and ch["id"] not in ids]
        return ids[:n]
    return [c[-1] for c in sorted(cands)[:n]]


def flag(cc):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)


def load_previous(cc):
    path = os.path.join(DOCS, "countries", "%s.json" % cc)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    sources = {n: read_source(n) for n in source_files()}
    walls = read_walls()
    errors = validate(sources) + validate_walls(walls, sources)
    if errors:
        print("Validation failed:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)
    if "--check" in sys.argv:
        print("OK: %d countries, %d walls valid" % (len(sources), len(walls)))
        return
    if not OFFLINE and "--no-scrape" not in sys.argv:
        _live, _changed, files = live.scrape_sources(sources)
        for name in sorted(files):
            write_source(sources[name])

    all_ucs = {ch["youtubeChannelId"] for s in sources.values() for ch in s["channels"]}
    yt2q = match_youtube_ids(all_ucs) if all_ucs else {}
    codes = [s["code"] for s in sources.values()]
    c2q = country_items(codes)

    chan_q = {}
    for s in sources.values():
        for ch in s["channels"]:
            # an explicit "wikidata": null in the source disables matching (wrong P2397 owner on Wikidata)
            q = ch["wikidata"] if "wikidata" in ch else yt2q.get(ch["youtubeChannelId"])
            if q:
                chan_q[(s["code"], ch["id"])] = q
    ents = get_entities(set(chan_q.values()), "labels|descriptions|claims|sitelinks")
    countries = get_entities(set(c2q.values()), "labels")

    # secondary items: languages (need P218) and places (need labels)
    lang_q, place_q = set(), set()
    for e in ents.values():
        lang_q.update(claim_values(e, "P407") + claim_values(e, "P364"))
        place_q.update(claim_values(e, "P159")[:1] + claim_values(e, "P131")[:1])
    lang_ents = get_entities(lang_q, "claims")
    place_ents = get_entities(place_q, "labels|claims")
    # HQ often points at a building or street: climb P131 until a populated place (has P1082)
    for _ in range(3):
        parents = set()
        for e in place_ents.values():
            if not claim_values(e, "P1082"):
                parents.update(claim_values(e, "P131")[:1])
        parents -= set(place_ents)
        if not parents:
            break
        place_ents.update(get_entities(parents, "labels|claims"))

    def settle(q, depth=0):
        e = place_ents.get(q)
        if not e or claim_values(e, "P297"):  # missing, or a country
            return None
        if claim_values(e, "P1082") or depth >= 3:
            return q
        up = claim_values(e, "P131")[:1]
        return settle(up[0], depth + 1) if up else q
    iso = {q: (claim_values(e, "P218") or [None])[0] for q, e in lang_ents.items()}

    # Wikipedia first-sentence fallback for locales that have an article but no Wikidata description
    wanted = []
    for q in set(chan_q.values()):
        e = ents.get(q)
        if not e:
            continue
        for loc, (site, host, _v) in WIKIS.items():
            if site in e.get("sitelinks", {}) and not pick(e.get("descriptions", {}), LOCALES[loc]):
                wanted.append((q, loc, host, e["sitelinks"][site]["title"]))
    with ThreadPoolExecutor(4) as ex:
        summaries = dict(zip([(w[0], w[1]) for w in wanted], ex.map(lambda w: wikipedia_summary(w[2], w[3]), wanted)))

    now = datetime.datetime.now(datetime.timezone.utc)
    version_now = int(now.strftime("%Y%m%d%H"))
    index_countries = []
    any_changed = False
    stats = []
    live_gids = set()
    live_since = {}  # gid -> liveSince (or None) for docs/live.json
    for name in sorted(sources):
        s = sources[name]
        cc = s["code"]
        prev = load_previous(cc)
        prev_by_id = {c["id"]: c for c in (prev or {}).get("channels", [])}
        default_langs = s.get("languages") or ["en"]
        main_lang = default_langs[0]
        out_channels = []
        matched = 0
        excluded = {ch["id"] for ch in s["channels"] if ch.get("embeddable") is False}
        for ch in s["channels"]:
            if ch["id"] in excluded:
                print("  excluded %s/%s: live video not embeddable" % (cc, ch["id"]), file=sys.stderr)
                continue
            gid = "%s.%s" % (cc.lower(), ch["id"])
            q = chan_q.get((cc, ch["id"]))
            e = ents.get(q) if q else None
            rec = {"id": gid, "name": ch["name"], "category": ch["category"]}
            if e:
                matched += 1
                langs = []
                for lq in claim_values(e, "P407") + claim_values(e, "P364"):
                    code = iso.get(lq)
                    if code and re.match(r"^[a-z]{2}$", code) and code not in langs:
                        langs.append(code)
                desc = {}
                for loc, chain in LOCALES.items():
                    d = pick(e.get("descriptions", {}), chain) or summaries.get((q, loc))
                    if d:
                        desc[loc] = d
                wiki = {}
                for loc, (site, host, variant) in WIKIS.items():
                    sl = e.get("sitelinks", {}).get(site)
                    if sl:
                        title = urllib.parse.quote(sl["title"].replace(" ", "_"), safe="():,'!*-._~")
                        wiki[loc] = "https://%s.wikipedia.org/%s/%s" % (host, variant or "wiki", title)
                logos = claim_values(e, "P154")
                sites = [w for w in claim_values(e, "P856") if isinstance(w, str)]
                places = claim_values(e, "P159")[:1] or claim_values(e, "P131")[:1]
                city = None
                pq = settle(places[0]) if places else None
                if pq:
                    city = pick(place_ents[pq].get("labels", {}), [main_lang, "en"] + WD_LANGS)
                enrich = {"languages": langs, "wikidata": q, "description": desc, "wikipedia": wiki,
                          "logo": commons_png(logos[0]) if logos else None, "website": sites[0] if sites else None,
                          "city": city}
            elif DEGRADED and ch["id"] in prev_by_id and prev_by_id[ch["id"]]["youtubeChannelId"] == ch["youtubeChannelId"]:
                # Wikidata unreachable this run: keep what was published last time
                p = prev_by_id[ch["id"]]
                matched += bool(p.get("wikidata"))
                enrich = {k: p.get(k) for k in ("wikidata", "description", "wikipedia", "logo", "website", "city")}
                enrich["languages"] = p.get("languages") or []
            else:
                enrich = {"languages": [], "wikidata": q, "description": {}, "wikipedia": {}, "logo": None,
                          "website": None, "city": None}
            languages = ch.get("languages") or enrich.get("languages") or default_langs
            rec.update({
                "languages": languages,
                "youtubeChannelId": ch["youtubeChannelId"],
                "youtubeVideoId": ch.get("youtubeVideoId"),
                "embeddable": ch.get("embeddable"),
                "live": ch.get("live") is True,
                "wikidata": enrich.get("wikidata"),
                "description": enrich.get("description") or {},
                "wikipedia": enrich.get("wikipedia") or {},
                "logo": enrich.get("logo"),
                "website": enrich.get("website"),
                "city": enrich.get("city"),
            })
            out_channels.append(rec)
            if rec["live"]:
                live_gids.add(gid)
                live_since[gid] = ch.get("liveSince")

        body = {"schema": SCHEMA, "code": cc, "defaultTiles": ["%s.%s" % (cc.lower(), t) for t in pick_defaults(s, excluded, now, {c["id"].split(".", 1)[1]: c["languages"] for c in out_channels})],
                "channels": out_channels}
        prev_body = {k: v for k, v in (prev or {}).items() if k != "version"}
        if prev and prev_body == body:
            version = prev["version"]
        else:
            version = version_now
            any_changed = True
        doc = {"schema": SCHEMA, "code": cc, "version": version, "defaultTiles": body["defaultTiles"], "channels": out_channels}
        write_json(os.path.join(DOCS, "countries", "%s.json" % cc), doc)

        cname = {}
        cent = countries.get(c2q.get(cc, ""), {})
        for loc, chain in LOCALES.items():
            v = pick(cent.get("labels", {}), chain)
            if v:
                cname[loc] = v
        if not cname:  # Wikidata unreachable: reuse the previous index entry
            cname = next((c["name"] for c in _prev_index().get("countries", []) if c["code"] == cc), {"en": cc})
        index_countries.append({"code": cc, "flag": flag(cc), "name": cname, "languages": default_langs,
                                "channelCount": len(out_channels), "version": version,
                                "file": "countries/%s.json" % cc})
        stats.append((cc, len(out_channels), matched))

    out_walls = build_walls(walls, live_gids, now)
    prev_index = _prev_index()
    updated = prev_index.get("updated")
    prev_countries = prev_index.get("countries")
    if any_changed or prev_countries != index_countries or prev_index.get("walls") != out_walls or not updated:
        updated = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(os.path.join(DOCS, "index.json"), {"schema": SCHEMA, "updated": updated, "countries": index_countries,
                                                  "walls": out_walls})
    # docs/live.json: what is live right now and since when, for a page that needs only that (a few KB
    # instead of every country file). "updated" moves only when the set changes.
    live_path = os.path.join(DOCS, "live.json")
    try:
        with open(live_path, encoding="utf-8") as f:
            prev_live = json.load(f)
    except (OSError, ValueError):
        prev_live = {}
    live_map = dict(sorted(live_since.items()))
    live_updated = prev_live.get("updated") if prev_live.get("live") == live_map else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(live_path, {"schema": 1, "updated": live_updated, "live": live_map})
    # remove published files for countries that no longer have a source
    for f in os.listdir(os.path.join(DOCS, "countries")):
        if f.endswith(".json") and f not in sources:
            os.remove(os.path.join(DOCS, "countries", f))
    open(os.path.join(DOCS, ".nojekyll"), "a").close()

    total = sum(n for _, n, _ in stats)
    print("Built %d countries, %d channels, %d walls" % (len(stats), total, len(out_walls)))
    for w in out_walls:
        print("  wall %s: %d tiles" % (w["id"], len(w["tiles"])))
    for cc, n, m in stats:
        print("  %s %3d channels, Wikidata %3d (%d%%)" % (cc, n, m, round(100 * m / n) if n else 0))


_PREV_INDEX = None


def _prev_index():
    global _PREV_INDEX
    if _PREV_INDEX is None:
        path = os.path.join(DOCS, "index.json")
        _PREV_INDEX = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    return _PREV_INDEX


if __name__ == "__main__":
    main()
