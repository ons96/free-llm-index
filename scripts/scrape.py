#!/usr/bin/env python3
"""Tiered, cache-first scraper.

Fallback chain per source (cheapest first):
    curl_cffi (browser TLS) -> urllib -> Cloudflare relay -> Obscura (real browser)
    -> r.jina.ai reader -> scrapling StealthyFetcher (only if installed)

Rules:
  * a document younger than --max-age is reused, no network at all
  * a URL that failed 6 times inside 7 days is skipped (dead is dead)
  * robots.txt is honoured where it is reachable
  * everything is written atomically, so a killed run never corrupts the cache
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DATA,
    HAVE_CURL_CFFI,
    Source,
    backoff,
    cache_path,
    fetch_curl_cffi,
    fetch_obscura,
    fetch_reader,
    fetch_relay,
    fetch_stealthy,
    fetch_urllib,
    iso,
    load_cache,
    load_sources,
    log,
    obscura_bin,
    recent_failure,
    robots_allows,
    save_cache,
    save_failure,
    write_json,
    FetchResult,
)

READER_MIN_GAP = 3.5  # r.jina.ai free tier ~20 requests/minute
_last_reader = 0.0


def reader_throttle() -> None:
    global _last_reader
    gap = time.time() - _last_reader
    if gap < READER_MIN_GAP:
        time.sleep(READER_MIN_GAP - gap)
    _last_reader = time.time()


def looks_like_js_shell(body: str) -> bool:
    """A page that is only a framework bootstrap: no useful text yet."""
    stripped = body.strip()
    if len(stripped) < 900:
        return True
    low = stripped.lower()
    markers = low.count("<script") + low.count("__next_f") + low.count("window.__")
    text_len = len("".join(ch for ch in stripped if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"))
    return markers > 3 and text_len / max(len(stripped), 1) < 0.12


def cheap_chain(url: str, tier: int) -> FetchResult:
    """Everything that does not need a browser."""
    order = [fetch_urllib, fetch_curl_cffi] if not HAVE_CURL_CFFI else [fetch_curl_cffi, fetch_urllib]
    last = FetchResult(False, None, tool="none", error="no transport")
    for fn in order:
        res = fn(url)
        if res.ok and res.body.strip():
            return res
        last = res
        # 403/429/503 -> stop hammering, escalate immediately
        if res.status in (403, 429, 503):
            break
    return last


def full_chain(src: Source, force_relay: bool = False) -> tuple[FetchResult, list[str]]:
    notes: list[str] = []
    url = src.url

    res = cheap_chain(url, src.tier)
    if res.ok and not looks_like_js_shell(res.body):
        return res, notes
    if res.ok:
        notes.append("cheap transport returned a JS shell")

    # relay for anything datacenter-blocked
    if res.status in (401, 403, 429, 451, 503) or force_relay or not res.ok:
        rel = fetch_relay(url)
        if rel.ok and not looks_like_js_shell(rel.body):
            return rel, notes
        if not rel.ok:
            notes.append(f"relay: {rel.error}")

    # real browser
    if src.tier <= 3 or src.kind != "json":
        obsc = fetch_obscura(url)
        if obsc.ok and not looks_like_js_shell(obsc.body):
            return obsc, notes
        if not obsc.ok:
            notes.append(f"obscura: {obsc.error}")

    # free reader (renders JS, different egress)
    reader_throttle()
    rd = fetch_reader(url)
    if rd.ok and len(rd.body.strip()) > 400:
        return rd, notes
    if not rd.ok:
        notes.append(f"reader: {rd.error}")

    # optional stealth browser
    st = fetch_stealthy(url)
    if st.ok and st.body.strip():
        return st, notes
    notes.append(f"stealthy: {st.error}")

    return res if res.ok else FetchResult(False, res.status, tool="chain", error="all tiers failed: " + "; ".join(notes)), notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age", default="12h", help="cache freshness, e.g. 6h / 2d")
    ap.add_argument("--force", action="store_true", help="ignore cache and failure ledger")
    ap.add_argument("--only", default="", help="substring filter on source URLs")
    ap.add_argument("--tier", type=int, default=0, help="only sources of this tier")
    ap.add_argument("--jobs", type=int, default=4, help="parallel fetches for the browser tier")
    args = ap.parse_args()

    unit = args.max_age.strip().lower()
    mult = {"h": 3600, "d": 86400, "m": 60}.get(unit[-1], 3600)
    max_age = timedelta(seconds=float(unit[:-1] or 1) * mult)

    sources = [s for s in load_sources() if s.enabled]
    if args.only:
        sources = [s for s in sources if args.only in s.url]
    if args.tier:
        sources = [s for s in sources if s.tier == args.tier]

    health = []
    pending: list[Source] = []
    reused = 0

    for src in sources:
        cached = None if args.force else load_cache(src.url, max_age)
        if cached:
            reused += 1
            health.append({"url": src.url, "ok": True, "tool": cached.get("tool"), "status": cached.get("status"),
                           "bytes": cached.get("bytes"), "cache": "hit", "fetched_at": cached.get("fetched_at")})
            continue
        if not args.force:
            fail = recent_failure(src.url)
            if fail and fail.get("exhausted"):
                health.append({"url": src.url, "ok": False, "tool": fail.get("tool"), "status": fail.get("status"),
                               "error": f"skipped, exhausted ({fail.get('attempts')} attempts): {fail.get('error')}",
                               "cache": "skip"})
                continue
        allowed, why = robots_allows(src.url)
        if not allowed:
            health.append({"url": src.url, "ok": False, "tool": "robots", "error": why, "cache": "skip"})
            continue
        pending.append(src)

    log(f"{len(sources)} sources | {reused} cached | {len(pending)} to fetch | curl_cffi={HAVE_CURL_CFFI} "
        f"| obscura={obscura_bin() or 'none'}")

    # JSON documents never need a browser; fetch them serially and cheaply.
    json_sources = [s for s in pending if s.kind == "json"]
    other = [s for s in pending if s.kind != "json"]

    for src in json_sources:
        res = cheap_chain(src.url, src.tier)
        health.append(_record(src, res, []))
        log(f"  {_icon(res)} {res.tool:<18} {res.status} {len(res.body):>8}B {src.url}")

    def work(src: Source):
        res, notes = full_chain(src)
        return src, res, notes

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(work, src): src for src in other}
        done = 0
        for fut in as_completed(futures):
            src, res, notes = fut.result()
            done += 1
            rec = _record(src, res, notes)
            health.append(rec)
            log(f"  [{done}/{len(other)}] {_icon(res)} {res.tool:<18} {res.status} {len(res.body):>8}B {src.url}"
                + (f"  ({'; '.join(notes)})" if notes and not res.ok else ""))

    write_json(DATA / "source_health.json", {"generated_at": iso(), "sources": health})
    ok = sum(1 for h in health if h.get("ok"))
    log(f"done: {ok}/{len(health)} sources healthy")
    return 0


def _icon(res: FetchResult) -> str:
    return "OK " if res.ok else "ERR"


def _record(src: Source, res: FetchResult, notes: list[str]) -> dict:
    if res.ok and res.body.strip():
        save_cache(src.url, res.body, res.tool, res.status or 200, res.content_type)
        return {"url": src.url, "ok": True, "tool": res.tool, "status": res.status,
                "bytes": len(res.body), "elapsed": round(res.elapsed, 2), "cache": "miss", "notes": notes}
    fail = save_failure(src.url, res.tool, res.status, res.error)
    return {"url": src.url, "ok": False, "tool": res.tool, "status": res.status,
            "error": res.error, "notes": notes, "attempts": fail["attempts"], "cache": "miss"}


if __name__ == "__main__":
    raise SystemExit(main())
