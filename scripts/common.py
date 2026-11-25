"""Shared helpers: paths, cache, atomic writes, URL hygiene, fetch transport.

No third-party dependency is required. `curl_cffi` is used when it is
installed because its browser TLS fingerprint gets through several bot walls
that plain curl cannot; otherwise we fall back to urllib.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRAPED = ROOT / "scraped"
FAILURES = ROOT / "scraped_failures"
DATA = ROOT / "data"
DIST = ROOT / "dist"
SOURCES_FILE = ROOT / "sources.txt"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BOT_UA = "FreeLLMIndexBot/1.0 (+https://github.com/{repo}; research crawler)"

MAX_BODY = 12 * 1024 * 1024  # hard cap per document, keeps the cache small

# Tracking / affiliate params stripped from every URL we store, so the
# database never becomes a referral funnel.
TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|aff|aff_id|affiliate|ref|referral|ref_code|invite|invite_code|"
    r"code|from|share|channel|source|spm|promo|coupon|i|ic|inviter|share_code|"
    r"clickid|gclid|fbclid|yclid|msclkid)$",
    re.I,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """Write via .tmp + rename so a killed run never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
# URL hygiene
# --------------------------------------------------------------------------

def strip_tracking(url: str) -> str:
    """Remove affiliate/tracking query params and the fragment."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(k)
    ]
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, "")
    )


MULTI_SUFFIX = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "co.uk", "org.uk", "ac.uk",
    "com.au", "co.jp", "co.kr", "com.br", "com.tw", "com.hk", "co.nz", "com.sg",
    "com.my", "co.in", "com.tr", "com.ru", "com.ua", "co.za", "com.mx", "com.ar",
}


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host or re.fullmatch(r"[\d.]+", host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def norm_url(url: str) -> str:
    return strip_tracking(url).rstrip("/")


# --------------------------------------------------------------------------
# Source registry
# --------------------------------------------------------------------------

@dataclass
class Source:
    url: str
    tier: int = 2
    kind: str = "html"
    role: str = "seed"
    enabled: bool = True


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    out: list[Source] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, _, comment = line.partition("#")
        url = url.strip()
        if not url.startswith("http"):
            continue
        attrs = dict(re.findall(r"(\w+):\s*([^\s#]+)", comment))
        src = Source(url=norm_url(url))
        src.tier = int(attrs.get("tier", 2))
        src.kind = attrs.get("kind", "html")
        src.role = attrs.get("role", "seed")
        src.enabled = attrs.get("enabled", "true").lower() != "false"
        out.append(src)
    return out


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

CACHE_TTL = timedelta(hours=18)
FAILURE_TTL = timedelta(days=7)


def cache_path(url: str) -> Path:
    return SCRAPED / f"{sha256(url)}.json"


def failure_path(url: str) -> Path:
    return FAILURES / f"{sha256(url)}.json"


def load_cache(url: str, max_age: timedelta = CACHE_TTL) -> dict | None:
    doc = read_json(cache_path(url))
    if not doc or "body" not in doc:
        return None
    try:
        age = now_utc() - parse_iso(doc["fetched_at"])
    except (KeyError, ValueError):
        return None
    if age > max_age:
        return None
    return doc


def recent_failure(url: str, max_age: timedelta = FAILURE_TTL) -> dict | None:
    doc = read_json(failure_path(url))
    if not doc:
        return None
    try:
        if now_utc() - parse_iso(doc["last_attempt"]) <= max_age:
            return doc
    except (KeyError, ValueError):
        return None
    return None


def save_cache(url: str, body: str, tool: str, status: int, content_type: str) -> dict:
    doc = {
        "url": url,
        "fetched_at": iso(),
        "tool": tool,
        "status": status,
        "content_type": content_type,
        "bytes": len(body.encode("utf-8")),
        "body": body[: MAX_BODY],
    }
    write_json(cache_path(url), doc)
    fp = failure_path(url)
    if fp.exists():
        fp.unlink()
    return doc


def save_failure(url: str, tool: str, status: int | None, error: str) -> dict:
    prev = read_json(failure_path(url), {}) or {}
    attempts = int(prev.get("attempts", 0)) + 1
    doc = {
        "url": url,
        "attempts": attempts,
        "last_attempt": iso(),
        "first_attempt": prev.get("first_attempt", iso()),
        "tool": tool,
        "status": status,
        "error": error[:500],
        "exhausted": attempts >= 6,
    }
    write_json(failure_path(url), doc)
    return doc


# --------------------------------------------------------------------------
# robots.txt (best effort, cached for the run)
# --------------------------------------------------------------------------

_ROBOTS: dict[str, list[str]] = {}


def robots_allows(url: str) -> tuple[bool, str]:
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _ROBOTS:
        rules: list[str] = []
        try:
            req = urllib.request.Request(
                base + "/robots.txt", headers={"User-Agent": BOT_UA.format(repo="free-llm-index")}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read(200_000).decode("utf-8", "replace")
            agent = None
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                if not line or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key, value = key.strip().lower(), value.strip()
                if key == "user-agent":
                    agent = value
                elif key == "disallow" and value and agent in ("*", "freellmindxbot"):
                    rules.append(value)
        except Exception:
            rules = []
        _ROBOTS[base] = rules
    path = parts.path or "/"
    for rule in _ROBOTS[base]:
        prefix = rule.rstrip("*")
        if prefix and path.startswith(prefix):
            return False, f"robots.txt disallows {prefix}"
    return True, ""


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    body: str = ""
    content_type: str = ""
    tool: str = ""
    error: str = ""
    elapsed: float = 0.0


def _have_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


HAVE_CURL_CFFI = _have_curl_cffi()


def fetch_curl_cffi(url: str, timeout: int = 30, impersonate: str = "chrome") -> FetchResult:
    from curl_cffi import requests as creq  # type: ignore

    t0 = time.time()
    try:
        r = creq.get(
            url,
            impersonate=impersonate,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
        )
        ctype = r.headers.get("content-type", "")
        text = r.text if "text" in ctype or "json" in ctype or not ctype else r.content[:MAX_BODY].decode("utf-8", "replace")
        return FetchResult(
            ok=200 <= r.status_code < 300,
            status=r.status_code,
            body=text[:MAX_BODY],
            content_type=ctype,
            tool=f"curl_cffi:{impersonate}",
            error="" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}",
            elapsed=time.time() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, tool="curl_cffi", error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - t0)


def fetch_urllib(url: str, timeout: int = 30) -> FetchResult:
    t0 = time.time()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY)
            ctype = resp.headers.get("content-type", "")
            body = raw.decode("utf-8", "replace")
            return FetchResult(True, resp.status, body, ctype, "urllib", "", time.time() - t0)
    except urllib.error.HTTPError as exc:
        return FetchResult(False, exc.code, "", "", "urllib", f"HTTP {exc.code}", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, "", "", "urllib", f"{type(exc).__name__}: {exc}", time.time() - t0)


def fetch_relay(url: str, timeout: int = 40) -> FetchResult:
    """Cloudflare Worker relay. Only used when the secrets are configured."""
    relay = os.environ.get("RELAY_URL", "").strip()
    secret = os.environ.get("RELAY_SECRET", "").strip()
    if not relay or not secret:
        return FetchResult(False, None, tool="relay", error="relay not configured")
    target = f"{relay.rstrip('/')}/?url={urllib.parse.quote(url, safe='')}"
    t0 = time.time()
    req = urllib.request.Request(
        target,
        headers={"Authorization": f"Bearer {secret}", "User-Agent": BOT_UA.format(repo="free-llm-index")},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY).decode("utf-8", "replace")
            return FetchResult(True, resp.status, body, resp.headers.get("content-type", ""), "relay", "", time.time() - t0)
    except urllib.error.HTTPError as exc:
        return FetchResult(False, exc.code, "", "", "relay", f"HTTP {exc.code}", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, "", "", "relay", f"{type(exc).__name__}: {exc}", time.time() - t0)


def fetch_reader(url: str, timeout: int = 60) -> FetchResult:
    """r.jina.ai free reader: renders JS and clears several bot walls. ~20 RPM."""
    t0 = time.time()
    req = urllib.request.Request(
        "https://r.jina.ai/" + url,
        headers={"User-Agent": USER_AGENT, "X-Return-Format": "markdown"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY).decode("utf-8", "replace")
            return FetchResult(True, resp.status, body, "text/plain", "reader", "", time.time() - t0)
    except urllib.error.HTTPError as exc:
        return FetchResult(False, exc.code, "", "", "reader", f"HTTP {exc.code}", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, "", "", "reader", f"{type(exc).__name__}: {exc}", time.time() - t0)


def obscura_bin() -> str | None:
    for candidate in (os.environ.get("OBSCURA_BIN", ""), shutil.which("obscura") or "", str(ROOT / "bin" / "obscura")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def fetch_obscura(url: str, timeout: int = 45, wait: str = "load") -> FetchResult:
    """Real browser (JS + stealth). Bulk `obscura scrape` is used by scrape.py."""
    binary = obscura_bin()
    if not binary:
        return FetchResult(False, None, tool="obscura", error="obscura binary not found")
    t0 = time.time()
    cmd = [binary, "fetch", url, "--dump", "html", "--timeout", str(timeout), "--wait-until", wait, "--quiet"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
        if proc.returncode != 0:
            return FetchResult(False, None, tool="obscura", error=(proc.stderr or "")[:300], elapsed=time.time() - t0)
        return FetchResult(True, 200, proc.stdout[:MAX_BODY], "text/html", "obscura", "", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, tool="obscura", error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - t0)


def fetch_stealthy(url: str, timeout: int = 60) -> FetchResult:
    """Optional last resort. Only works if scrapling+patchright are installed."""
    t0 = time.time()
    try:
        from scrapling.fetchers import StealthyFetcher  # type: ignore

        page = StealthyFetcher.fetch(url, headless=True, timeout=timeout, network_idle=True)
        return FetchResult(True, getattr(page, "status", 200), page.body.decode("utf-8", "replace")
                           if isinstance(page.body, bytes) else str(page.body), "text/html", "stealthy", "", time.time() - t0)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, tool="stealthy", error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - t0)


def backoff(attempt: int) -> None:
    time.sleep(min(30, (2 ** attempt) + random.uniform(0, 1.5)))


def log(msg: str) -> None:
    print(f"[{iso()}] {msg}", flush=True)
