#!/usr/bin/env python3
"""
Check if streaming URLs (HLS/DASH/direct) are working.

Usage:
  python3 check_streams.py <url> [<url> ...]
  python3 check_streams.py --json /path/to/live_streams_ok.json
  python3 check_streams.py --json /path/to/file.json --limit 50
  echo "https://example.com/stream.m3u8" | python3 check_streams.py --stdin

What "working" means here:
  - HTTP request succeeds (2xx/3xx final) within timeout
  - For HLS (.m3u8 / #EXTM3U / application/vnd.apple.mpegurl), the manifest is
    parseable; quality variants are extracted from #EXT-X-STREAM-INF
  - Non-HLS streams are considered working if the endpoint responds OK

Caveats:
  - Tokenized/signed/geo-restricted URLs may expire or require specific headers,
    referrer/origin, IP, or cookies (see behaviorHints.proxyHeaders in sources).
  - A working manifest does not guarantee every media segment plays.
  - 403/429/502/timeout can be transient, geo, rate-limit, or token issues.
"""
from __future__ import annotations
import argparse, json, sys, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
M3U8_CT = ("mpegurl", "application/x-mpegurl", "vnd.apple.mpegurl", "audio/mpegurl")


def parse_hls_qualities(body: str):
    qualities = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = {}
        for k, v in re.findall(r'([A-Z0-9-]+)=(".*?"|[^,]*)', line):
            attrs[k] = v.strip('"')
        q = {
            "resolution": attrs.get("RESOLUTION"),
            "bandwidth": int(attrs["BANDWIDTH"]) if attrs.get("BANDWIDTH", "").isdigit() else attrs.get("BANDWIDTH"),
            "codecs": attrs.get("CODECS"),
            "frame_rate": attrs.get("FRAME-RATE"),
        }
        uri = lines[i + 1].strip() if i + 1 < len(lines) and not lines[i + 1].startswith("#") else None
        if uri:
            q["uri"] = uri
        qualities.append(q)
    return qualities


def check_url(url: str, timeout: float = 12.0, headers: dict | None = None, method: str = "GET"):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    t0 = time.time()
    req = Request(url, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            final = resp.url
            status = resp.status
            ct = (resp.headers.get("Content-Type") or "").lower()
            # read a bounded amount: enough for HLS manifest
            raw = resp.read(256 * 1024)
        elapsed = round(time.time() - t0, 2)
        text = ""
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            pass
        is_m3u8 = url.split("?")[0].lower().endswith(".m3u8") or any(x in ct for x in M3U8_CT) or text.lstrip().startswith("#EXTM3U")
        qualities = parse_hls_qualities(text) if is_m3u8 else []
        ok = 200 <= status < 400
        return {
            "url": url, "final_url": final, "ok": ok, "status": status,
            "content_type": ct, "is_hls": is_m3u8, "qualities": qualities,
            "elapsed_s": elapsed, "error": None,
        }
    except HTTPError as e:
        return {"url": url, "final_url": getattr(e, 'url', url), "ok": False, "status": e.code,
                "content_type": (e.headers.get("Content-Type") or "").lower() if e.headers else "",
                "is_hls": False, "qualities": [], "elapsed_s": round(time.time() - t0, 2),
                "error": f"HTTPError {e.code}"}
    except (URLError, TimeoutError, OSError, ValueError) as e:
        return {"url": url, "final_url": url, "ok": False, "status": None,
                "content_type": "", "is_hls": False, "qualities": [],
                "elapsed_s": round(time.time() - t0, 2), "error": str(e)}


def fmt_q(q):
    r = q.get("resolution") or "?"
    bw = q.get("bandwidth")
    if isinstance(bw, int):
        bw = f"{bw/1_000_000:.2f}Mbps" if bw >= 1_000_000 else f"{bw/1000:.0f}kbps"
    return f"{r}" + (f"@{bw}" if bw else "")


def main():
    ap = argparse.ArgumentParser(description="Check if stream URLs work")
    ap.add_argument("urls", nargs="*", help="Stream URLs")
    ap.add_argument("--json", help="JSON file with objects containing 'url'")
    ap.add_argument("--stdin", action="store_true", help="Read URLs from stdin")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--headers-json", help="Extra headers as JSON object")
    ap.add_argument("--out", help="Write full JSON results to file")
    args = ap.parse_args()

    items = []
    if args.json:
        data = json.load(open(args.json))
        if isinstance(data, dict) and "items" in data:
            data = data["items"]
        for x in data:
            url = x.get("url") if isinstance(x, dict) else x
            name = x.get("name") if isinstance(x, dict) else None
            hdrs = None
            if isinstance(x, dict):
                bh = x.get("behaviorHints") or {}
                hdrs = (bh.get("proxyHeaders") or {}).get("request")
            if url:
                items.append({"url": url, "name": name, "headers": hdrs})
    items += [{"url": u, "name": None, "headers": None} for u in args.urls]
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append({"url": line, "name": None, "headers": None})
    # dedupe by url preserving order
    seen = set(); deduped = []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"]); deduped.append(it)
    items = deduped
    if args.limit:
        items = items[:args.limit]
    if not items:
        ap.error("No URLs provided")

    extra = json.loads(args.headers_json) if args.headers_json else {}

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for it in items:
            h = dict(extra)
            if it.get("headers"):
                h.update(it["headers"])
            futs[ex.submit(check_url, it["url"], args.timeout, h or None)] = it
        for fut in as_completed(futs):
            it = futs[fut]
            r = fut.result()
            r["name"] = it.get("name")
            results.append(r)
            mark = "OK" if r["ok"] else "FAIL"
            q = ", ".join(fmt_q(x) for x in r["qualities"][:4]) if r["qualities"] else ("HLS" if r["is_hls"] else "")
            label = f" [{r['name']}]" if r.get("name") else ""
            extra_info = f" {r['status']}" if r.get("status") else ""
            err = f" ({r['error']})" if r.get("error") and not r["ok"] else ""
            print(f"[{mark}]{extra_info}{label} {r['url']}  {q}{err}")

    ok = sum(1 for r in results if r["ok"])
    with_q = sum(1 for r in results if r["qualities"])
    print(f"\nSummary: {ok}/{len(results)} OK, {with_q} with parsed HLS quality variants")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": {"total": len(results), "ok": ok, "with_quality": with_q}, "results": results}, f, indent=2)
        print(f"Wrote {args.out}")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
