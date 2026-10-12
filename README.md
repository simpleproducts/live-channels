# Live Channels catalog

The channel catalog for the live-TV multi-screen app (iPhone, iPad, Mac, Vision Pro). The app plays
only broadcasters' **official YouTube live streams** through YouTube's official IFrame embed.

GitHub Pages serves `docs/`:

- `https://<user>.github.io/live-channels/index.json`: the list of countries
- `https://<user>.github.io/live-channels/countries/<CC>.json`: one file per country

## Layout

```
sources/<CC>.json          hand-edited source of truth, one file per country (ISO 3166-1 alpha-2)
scripts/build.py           sources/ -> docs/ (live pinning, Wikidata enrichment, validation)
scripts/refresh_live.py    refreshes pinned live video ids with the YouTube Data API (YT_API_KEY)
scripts/live.py            key-less live detection (scrapes /channel/<UC>/live), used by build.py
scripts/common.py          shared helpers (stable source-file formatting)
.github/workflows/build.yml  on push to main and every 2 hours: refresh, build, commit docs/ if changed
docs/                      published output (do not edit by hand)
```

## How the pipeline works

1. **Live pinning.** Many broadcasters block `embed/live_stream?channel=UC…` (YouTube error 150),
   but embedding the current live *video id* works. So every channel carries the id of its live
   broadcast in `youtubeVideoId`:
   - **With `YT_API_KEY`** (repo secret): `refresh_live.py` batches `videos.list` on all pinned ids
     (50 per call, 1 quota unit). For pins that ended, it reads each channel's RSS feed (free) and
     checks the recent ids with `videos.list`. Only then does it call `search.list` (100 units),
     capped at `SEARCH_CAP` (default 6) per run so 12 runs a day stay under the 10k daily quota.
     The workflow then runs `build.py --no-scrape`.
   - **Without a key**: `build.py` fetches `https://www.youtube.com/channel/<UC>/live` for every
     channel. The channel is live when the canonical URL is `watch?v=<id>` and the page has
     `"isLive":true`. The embed works when the page has `"playableInEmbed":true`.
   - Offline channels keep their previous id. The app falls back to `live_stream?channel=`.
   - When a live video is not embeddable, the channel gets `"embeddable": false` and is **left out
     of docs/** until a later run finds an embeddable broadcast and sets it back to `true`.
   Pinned ids are written back into `sources/` (the build copies them), so they appear in git history.
2. **Enrichment.** `build.py` matches every YouTube channel id to Wikidata through property
   **P2397** (SPARQL, batched `VALUES`). A `"wikidata": "Q…"` in the source overrides the match, and
   `"wikidata": null` disables it (for example when Wikidata attaches the YouTube id to the parent
   company). For matched items it pulls:
   - labels and descriptions in the 18 app locales, with the first sentence of the Wikipedia
     summary when an article exists but Wikidata has no description
   - Wikipedia sitelinks
   - the logo, P154 (always a Wikimedia Commons file, published as a
     `Special:FilePath/…?width=256` PNG thumbnail)
   - the website, P856
   - the language, P407/P364 mapped to ISO 639-1
   - the city, P159/P131, climbed to the nearest populated place and labeled in the country's main language
   Responses are cached in `.cache/` (or `$BUILD_CACHE`) for 7 days (`$CACHE_TTL_DAYS`). If
   Wikidata is unreachable, the previously published enrichment is kept.
3. **Validation.** The build fails (exit 1) on:
   - a duplicate id
   - an unknown category
   - a `youtubeChannelId` that doesn't match `^UC[\w-]{22}$`
   - a malformed `youtubeVideoId` or QID
   - a `defaultTiles` entry that isn't a channel id, or more than 9 of them
4. **Output.** Output is deterministic: source order, `ensure_ascii=False`, `indent=1`. A
   country's `version` (`yyyymmddHH`, UTC) only changes when its published content changes.
   `index.json.updated` only changes when a country changes.

## Commands

```sh
python3 -I scripts/build.py                 # scrape live ids + enrich + write docs/
python3 -I scripts/build.py --check         # validate sources only
python3 -I scripts/build.py --no-scrape     # build without touching live ids
python3 -I scripts/build.py --offline       # no network at all (cache + previous docs)
YT_API_KEY=... python3 -I scripts/refresh_live.py   # refresh live ids via the Data API, then:
python3 -I scripts/build.py --no-scrape
```

Python 3.9+ standard library only.

## Source schema (`sources/<CC>.json`)

One channel per line keeps diffs readable. The scripts rewrite the files in this format.

```json
{
 "code": "AR",
 "languages": ["es"],
 "defaultTiles": ["tn", "c5n", "lnmas", "a24", "canal26", "cronica", "tvpublica", "telefe", "eltrece"],
 "channels": [
  {"id": "tn", "name": "TN", "category": "news", "youtubeChannelId": "UCj6PcyLvpnIRT_2W_mwa9Aw"},
  {"id": "lnmas", "name": "LN+", "category": "news", "youtubeChannelId": "UCba3hpU7EFBSk817y9qZkiA", "youtubeVideoId": "FEWZjXJ7M0c", "embeddable": true, "wikidata": "Q27478538"}
 ]
}
```

| field | required | notes |
|---|---|---|
| `code` | yes | ISO 3166-1 alpha-2, uppercase, same as the file name |
| `languages` | no | country default languages (ISO 639-1); default `["en"]` |
| `defaultTiles` | no | up to 9 channel ids shown on first launch, best (news-first) first |
| `id` | yes | `[A-Za-z0-9_-]+`, unique within the country; published as `<cc>.<id>` |
| `name` | yes | display name |
| `category` | yes | one of `news, broadcast, economy, regional, government, sports, kids, streaming, radio, music, religious, culture, entertainment, cameras, other` |
| `youtubeChannelId` | yes | the broadcaster's **official** channel (`UC` + 22 chars) |
| `youtubeVideoId` | managed | current or last live video id, maintained by the scripts |
| `embeddable` | managed | `false` hides the channel from docs/ |
| `languages` (channel) | no | overrides Wikidata/country languages (for example DW Español → `["es"]`) |
| `wikidata` | no | QID override; `null` disables the P2397 match |

## Published schema (`docs/countries/<CC>.json`)

```json
{ "schema": 2, "code": "AR", "version": 2026101214, "defaultTiles": ["ar.tn", "..."],
  "channels": [ {
    "id": "ar.tn", "name": "TN", "category": "news", "languages": ["es"],
    "youtubeChannelId": "UC...", "youtubeVideoId": "abc123def45" | null, "embeddable": true | null,
    "wikidata": "Q..." | null,
    "description": { "en": "...", "es": "..." },
    "wikipedia": { "en": "https://en.wikipedia.org/wiki/...", "zh-Hans": "https://zh.wikipedia.org/zh-hans/..." },
    "logo": "https://commons.wikimedia.org/wiki/Special:FilePath/X.svg?width=256" | null,
    "website": "https://..." | null,
    "city": "Córdoba" | null
  } ] }
```

The `description` and `wikipedia` maps only contain the locales that exist. Locales: en, es,
zh-Hans, ja, fr, pt-BR, ko, it, ru, zh-Hant, ar, tr, nl, pl, sv, id, de, ms.

`docs/index.json`:

```json
{ "schema": 2, "updated": "2026-10-12T14:22:02Z",
  "countries": [ { "code": "AR", "flag": "🇦🇷", "name": { "en": "Argentina", "...": "..." },
                   "languages": ["es"], "channelCount": 59, "version": 2026101214, "file": "countries/AR.json" } ] }
```

## Adding a channel

1. Find the broadcaster's **official** channel. Use the verified badge and the link from the
   broadcaster's own website. Never add re-uploaders, restreams or paid services.
2. Get the `UC…` id from the channel page (View source → `"externalId"`, or the canonical URL).
3. Check that it actually goes live: `https://www.youtube.com/channel/<UC>/live` should be a
   `watch?v=` page with `"isLive":true` and `"playableInEmbed":true`, or the channel's "Live" tab
   should show regular recent streams. Skip channels that are never live.
4. Add one line to `sources/<CC>.json`, keeping news first, then broadcast, economy, regional,
   government, sports, radio and the rest. Optionally add it to `defaultTiles`.
5. Run `python3 -I scripts/build.py`, check the channel in `docs/countries/<CC>.json`, and commit.
   If Wikidata matched the wrong item, set `"wikidata"` to the right QID or to `null`.

## Adding a country

1. Create `sources/<CC>.json` with `code`, `languages`, `defaultTiles` and `channels`.
2. Run the build. The country name (18 locales) comes from Wikidata (P297 lookup), and the flag is
   derived from the code. The country appears in `index.json` automatically.

## GitHub setup

- Pages: Settings → Pages → deploy from branch `main`, folder `/docs`.
- Optional secret `YT_API_KEY`: a YouTube Data API v3 key. Without it, the workflow uses the
  key-less scrape.
- The workflow needs `contents: write` (already declared) to commit `docs/` and `sources/`.
