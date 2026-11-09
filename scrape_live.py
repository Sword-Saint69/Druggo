#!/usr/bin/env python3
"""
Scrape live TV stream links from known Stremio-compatible addons and merge
verified-working streams into data/streams.json for the Druggo addon.

- Dependency-free (stdlib). Safe to run in GitHub Actions.
- Sensitive sources (e.g. PenguPlay) are ONLY used if provided via env vars;
  nothing secret is hardcoded.
- Designed for frequent runs: bounded catalogs/streams, deduped, rechecked.

Env:
  PENGUPLAY_BASE  optional; full PenguPlay addon base URL (contains token) -
                  store as a GitHub Secret, never commit it.
  SCRAPE_MAX_METAS       per-catalog meta fetch cap (default 60)
  SCRAPE_MAX_STREAMS     per-meta stream fetch cap (default 80 per catalog total)
  CHECK_TIMEOUT          seconds (default 10)
  CHECK_WORKERS          parallel checkers (default 25)
"""
from __future__ import annotations
import json, os, re, sys, time, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "streams.json")

# Public sources (no secrets). Each: name, base, catalogs [(type,id)], limits.
SOURCES = [
    {"name": "nuviosports", "base": "https://nuviosports.xyz",
     "catalogs": [("tv", "nuvio_sports_live"), ("tv", "nuvio_sports_networks")], "max_metas": 80, "max_stream_lookups": 120},
    {"name": "stremverse", "base": "https://stremverse1.alwaysdata.net",
     "catalogs": [("tv", "stremverse_live_events"), ("tv", "stremverse_xtra_events")], "max_metas": 60, "max_stream_lookups": 100},
    {"name": "all-iptv", "base": "https://fun.lnpx.workers.dev",
     "catalogs": [("tv", "iptv-channels")], "max_metas": 60, "max_stream_lookups": 80},
    {"name": "watchio", "base": "https://watchio-addon.pages.dev",
     "catalogs": [("tv", "watchio_tv")], "max_metas": 40, "max_stream_lookups": 60},
    {"name": "flixnest-free", "base": "https://free.flixnest.app",
     "catalogs": [("tv", "flix_live")], "max_metas": 40, "max_stream_lookups": 60},
    {"name": "canales-deportivos", "base": "https://139c618fc3f4-canales-deportivos.baby-beamup.club",
     "catalogs": [("tv", "sports_channels")], "max_metas": 40, "max_stream_lookups": 60},
    {"name": "nightknight-cinema", "base": "https://nightknight-cinema.vercel.app",
     "catalogs": [("tv", "live_tv")], "max_metas": 40, "max_stream_lookups": 60},
    {"name": "highfly-sports", "base": "https://sports.highfly.dev",
     "catalogs": [("tv", "sports_live")], "max_metas": 30, "max_stream_lookups": 40},
]


def add_secret_sources():
    pb = os.environ.get("PENGUPLAY_BASE", "").strip()
    if pb:
        SOURCES.append({"name": "penguplay", "base": pb.rstrip("/"),
                        "catalogs": [("tv", "live")], "max_metas": 60, "max_stream_lookups": 80})


def fetch_json(url, timeout=12):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read(2 * 1024 * 1024)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        return {"_error": str(e), "_url": url}


def discover_catalogs(base, timeout=10):
    m = fetch_json(base.rstrip("/") + "/manifest.json", timeout)
    if not isinstance(m, dict) or m.get("_error"):
        return []
    out = []
    for c in m.get("catalogs") or []:
        if isinstance(c, dict) and c.get("type") and c.get("id"):
            out.append((c["type"], c["id"]))
    return out


def get_metas(base, typ, cat_id, limit, timeout=12):
    metas = []
    for skip in range(0, limit, 100):
        extra = f"/skip={skip}.json" if skip else ".json"
        url = f"{base.rstrip('/')}/catalog/{typ}/{cat_id}{extra}"
        d = fetch_json(url, timeout)
        batch = d.get("metas") if isinstance(d, dict) else None
        if not batch:
            # try query-style skip
            if skip == 0:
                d = fetch_json(f"{base.rstrip('/')}/catalog/{typ}/{cat_id}.json", timeout)
                batch = d.get("metas") if isinstance(d, dict) else None
            if not batch:
                break
        metas.extend(batch)
        if len(batch) < 50 or len(metas) >= limit:
            break
    return metas[:limit]


def get_streams(base, typ, meta_id, timeout=12):
    url = f"{base.rstrip('/')}/stream/{typ}/{meta_id}.json"
    d = fetch_json(url, timeout)
    if isinstance(d, dict):
        return d.get("streams") or []
    return []


def extract_stream_entries(streams, source, meta_name):
    out = []
    for s in streams or []:
        if not isinstance(s, dict):
            continue
        u = s.get("url") or s.get("externalUrl")
        if not u or u.startswith("magnet:") or u.startswith("infohash:"):
            continue
        bh = s.get("behaviorHints") or {}
        hdrs = (bh.get("proxyHeaders") or {}).get("request")
        # avoid carrying secrets accidentally embedded in non-Pengu public sources? (none expected)
        title = s.get("title") or s.get("description") or s.get("name") or meta_name or "Live"
        out.append({
            "url": u, "name": f"{meta_name} - {title}" if meta_name and meta_name not in str(title) else str(title),
            "addon": source, "behaviorHints": s.get("behaviorHints") or {}, "headers": hdrs,
        })
    return out


def scrape_source(src):
    base = src["base"].rstrip("/")
    catalogs = src.get("catalogs") or discover_catalogs(base)
    if not catalogs:
        catalogs = discover_catalogs(base)
    found = []
    lookups = 0
    for (typ, cid) in catalogs[:4]:
        metas = get_metas(base, typ, cid, src.get("max_metas", 40))
        for m in metas:
            if lookups >= src.get("max_stream_lookups", 60):
                break
            mid = m.get("id")
            if not mid:
                continue
            lookups += 1
            streams = get_streams(base, typ, mid)
            found.extend(extract_stream_entries(streams, src["name"], m.get("name") or m.get("title") or mid))
        if lookups >= src.get("max_stream_lookups", 60):
            break
    # dedupe by url
    seen = {}
    for x in found:
        seen.setdefault(x["url"], x)
    return list(seen.values())


# ---- checker (minimal inline, compatible with check_streams.py behavior) ----
M3U8_CT = ("mpegurl", "application/x-mpegurl", "vnd.apple.mpegurl", "audio/mpegurl")


def parse_hls_qualities(body):
    qualities = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = {}
        for k, v in re.findall(r'([A-Z0-9-]+)=(".*?"|[^,]*)', line):
            attrs[k] = v.strip('"')
        q = {"resolution": attrs.get("RESOLUTION"),
             "bandwidth": int(attrs["BANDWIDTH"]) if attrs.get("BANDWIDTH", "").isdigit() else attrs.get("BANDWIDTH"),
             "codecs": attrs.get("CODECS"), "frame_rate": attrs.get("FRAME-RATE")}
        qualities.append(q)
    return qualities


def check_url(url, timeout=10, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ct = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(256 * 1024)
        text = raw.decode("utf-8", errors="replace")
        is_m3u8 = url.split("?")[0].lower().endswith(".m3u8") or any(x in ct for x in M3U8_CT) or text.lstrip().startswith("#EXTM3U")
        return {"ok": 200 <= status < 400, "status": status, "content_type": ct,
                "is_m3u8": is_m3u8, "qualities": parse_hls_qualities(text) if is_m3u8 else []}
    except HTTPError as e:
        return {"ok": False, "status": e.code, "content_type": "", "is_m3u8": False, "qualities": []}
    except Exception:
        return {"ok": False, "status": None, "content_type": "", "is_m3u8": False, "qualities": []}


def make_item(entry, check):
    u = entry["url"]
    name = (entry.get("name") or "Live Stream").strip()
    addon = entry.get("addon", "unknown")
    ephemeral = bool(re.search(r'[?&](psig|hdntl|t=|e=|token=|sig=|signature=|st=|exp=|SessionID=)', u, re.I))
    q = check.get("qualities") or []
    qlabel = ""
    if q:
        best = q[0]; res = best.get("resolution") or "?"; bw = best.get("bandwidth")
        if isinstance(bw, int): bw = f"{bw/1_000_000:.2f}Mbps" if bw >= 1_000_000 else f"{bw/1000:.0f}kbps"
        qlabel = f" {res}" + (f" {bw}" if bw else "")
    sid = "dg_" + hashlib.sha1(u.encode()).hexdigest()[:16]
    bh = entry.get("behaviorHints") or {}
    if entry.get("headers") and "proxyHeaders" not in bh:
        bh = {**bh, "proxyHeaders": {"request": entry["headers"]}}
    return {
        "id": sid, "type": "tv", "name": name,
        "description": f"Source: {addon}\n{'EPHEMERAL/SIGNED - may expire' if ephemeral else 'Checked OK'}\n{check.get('content_type','')}\nHLS qualities: {len(q)}{qlabel}\nLast checked: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "posterShape": "square", "genres": [addon], "ephemeral": ephemeral, "addon": addon,
        "stream": {"url": u, "title": f"{name}{qlabel}\n{addon}" + (" [may expire]" if ephemeral else ""), "name": "Druggo", "behaviorHints": bh if bh else {"notWebReady": False}},
        "qualities": q, "is_m3u8": check.get("is_m3u8", True), "last_ok": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def load_existing():
    try:
        return json.load(open(DATA))
    except Exception:
        return {"meta": {}, "items": []}


def main():
    add_secret_sources()
    timeout = float(os.environ.get("CHECK_TIMEOUT", "10"))
    workers = int(os.environ.get("CHECK_WORKERS", "25"))
    print(f"Scraping {len(SOURCES)} sources...")
    candidates = []
    for src in SOURCES:
        try:
            got = scrape_source(src)
            print(f"  {src['name']}: {len(got)} candidate streams")
            candidates.extend(got)
        except Exception as e:
            print(f"  {src['name']}: ERROR {e}")
    # dedupe candidates
    by_url = {}
    for c in candidates:
        by_url.setdefault(c["url"], c)
    candidates = list(by_url.values())
    print(f"Total unique candidates: {len(candidates)}")

    # check
    ok_items = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_url, c["url"], timeout, c.get("headers")): c for c in candidates}
        for fut in as_completed(futs):
            c = futs[fut]
            r = fut.result()
            if r.get("ok"):
                ok_items.append(make_item(c, r))
    print(f"Verified OK now: {len(ok_items)}")

    existing = load_existing()
    # merge: keep newly OK; keep previously OK stable non-ephemeral items not re-seen? (optional)
    merged = {}
    for it in ok_items:
        merged[it["stream"]["url"]] = it
    # retain recent stable items from existing if still present or not contradicted? keep existing OK stable ones as fallback
    for it in existing.get("items") or []:
        u = (it.get("stream") or {}).get("url")
        if u and u not in merged and not it.get("ephemeral"):
            merged[u] = it  # keep previously stable
    items = sorted(merged.values(), key=lambda x: (x.get("ephemeral", True), x.get("addon", ""), x.get("name", "").lower()))
    out = {"meta": {"generated": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    "source": "scrape_live.py", "count": len(items),
                    "ephemeral_count": sum(1 for x in items if x.get("ephemeral")),
                    "verified_now": len(ok_items)},
           "items": items}
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {DATA}: {len(items)} items ({out['meta']['ephemeral_count']} ephemeral, {len(ok_items)} verified this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
