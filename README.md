# Druggo

**Druggo** is a Stremio/Nuvio-compatible **live TV/sports addon** that serves verified stream links scraped from public Stremio-compatible sources. A GitHub Action re-scrapes and rechecks links (scheduled every 5 minutes) and commits updated `data/streams.json`.

> **Caveats:** Many live stream URLs are tokenized/signed, geo-restricted, rate-limited, or require specific headers — they can expire. "Verified" means the manifest/endpoint responded OK at check time; it does not guarantee every segment plays. GitHub may delay/throttle very frequent scheduled runs.

## Add to Nuvio / Stremio
Run the addon server locally or deploy it, then add its manifest URL:
```bash
node addon.js
# Add in Nuvio/Stremio: http://127.0.0.1:7000/manifest.json
```
The addon implements the Stremio HTTP interface: `/manifest.json`, `/catalog/...`, `/meta/...`, `/stream/...` (CORS enabled, no external dependencies).

## What gets scraped/checked
`scrape_live.py` discovers catalogs/streams from configured public Stremio addons (e.g. Nuvio Live Sports, StremVerse, All IPTV, Watchio, FlixNest Free, Canales Deportivos, NightKnight Cinema, HighFly Sports), extracts playable `url`/`externalUrl` entries, checks them, and merges verified-OK streams into `data/streams.json`.

- **No secrets hardcoded.** Optional sensitive sources are enabled only via env vars / GitHub Secrets:
  - `PENGUPLAY_BASE` (optional secret): full PenguPlay addon base URL. Store as a repository secret; never commit it.

## Check streams manually
```bash
python3 check_streams.py "https://example.com/live.m3u8"
python3 check_streams.py --json data/streams.json --limit 50 --out data/links_rechecked.json
```
The checker validates HTTP status/timeout, detects HLS (`#EXTM3U`/mpegurl), parses `#EXT-X-STREAM-INF` for `RESOLUTION/BANDWIDTH/CODECS/FRAME-RATE`, and respects `behaviorHints.proxyHeaders.request` when present.

## GitHub Action
Workflow: `.github/workflows/scrape-live.yml`
- Triggers: every 5 minutes (`*/5 * * * *`) + manual `workflow_dispatch`.
- Runs `scrape_live.py` (stdlib Python), commits `data/streams.json` if changed (`[skip ci]`).
- Requires `permissions: contents: write` (set in workflow). Uses the default `GITHUB_TOKEN`.
- Uses `concurrency` to prevent overlapping runs.

## Repo layout
```text
addon.js                 # Druggo Stremio/Nuvio addon server
manifest.json            # static manifest
scrape_live.py           # scrape catalogs/streams + verify + merge
check_streams.py         # reusable stream/HLS checker
data/streams.json        # verified stream data (auto-updated)
.github/workflows/       # 5-min scrape action
```

## Nuvio "plugin" vs addon
- **This repo = Stremio-compatible addon** (catalog/meta/stream over HTTP), addable to Nuvio/Stremio by manifest URL.
- **Nuvio provider-repo scrapers** (movie/TV, TMDB) use a different interface: repo `manifest.json` + `providers/*.js` exporting `getStreams(tmdbId, mediaType, season, episode)`. See your scraped references under `../scraped/providers/` if you need that form.

## Legal / safety
Only use sources and content you have rights to access. Do not commit tokens, cookies, or private manifest URLs. Review source terms and applicable laws before deploying publicly.