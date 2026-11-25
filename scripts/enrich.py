#!/usr/bin/env python3
"""Extract -> normalize -> dedupe -> rank -> publish.

Reads the cache written by scrape.py, never the network.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    DATA,
    DIST,
    ROOT,
    host_of,
    iso,
    load_sources,
    log,
    norm_url,
    parse_iso,
    read_json,
    registrable_domain,
    sha256,
    write_json,
    atomic_write,
)
from parse import ModelIndex, parse_document, _norm  # noqa: E402

PROMPT_VERSION = "2026-11-25.1"
CNY_USD_RATE = 0.1408          # 1 CNY = 0.1408 USD (7.10 CNY/USD), pinned monthly
BASELINE_PERCENTILE = 50       # MiniMax M3.1 Flash ~ 50th (M3 is 47th on modelbenchmark)

# Hosts that belong to the model vendor itself. A relay pointing at one of
# these is *not* a duplicate of another relay pointing at the same one: the
# vendor is shared, the quota is not.
FIRST_PARTY_SUFFIXES = (
    "openai.com", "anthropic.com", "googleapis.com", "google.com", "deepseek.com",
    "groq.com", "mistral.ai", "cohere.ai", "cohere.com", "x.ai", "huggingface.co",
    "together.ai", "fireworks.ai", "openrouter.ai", "siliconflow.cn", "aliyuncs.com",
    "moonshot.cn", "bigmodel.cn", "z.ai", "minimaxi.com", "minimax.io", "cerebras.ai",
    "sambanova.ai", "nebius.com", "ollama.com", "modelscope.cn", "baidu.com",
    "volces.com", "xfyun.cn", "sensetime.com", "stepfun.com", "01.ai", "baichuan-ai.com",
)

STATUS_LABEL = {"online": "online", "offline": "offline", "unverified": "unverified", "unknown": "unknown"}

CSV_COLUMNS = [
    "id", "provider", "endpoint_url", "backend_host", "dedupe_key", "models_free",
    "best_free_model", "tier_score", "tier_source", "checkin_raw", "checkin_daily_value",
    "checkin_currency", "checkin_usd_equiv", "checkin_confidence", "rate_limit_raw",
    "rpm", "rpd", "signup_method", "signup_score", "status", "sources", "first_seen",
    "last_seen", "notes",
]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

DOMAIN_IN_NAME = re.compile(r"\s*\(([a-z0-9-]+(?:\.[a-z0-9-]+)+)\)\s*$", re.I)


def clean_provider(name: str) -> str:
    name = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", name)  # emoji
    name = DOMAIN_IN_NAME.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" |·-—")
    return name[:60]


def is_first_party(host: str) -> bool:
    h = host.lower()
    return any(h == s or h.endswith("." + s) for s in FIRST_PARTY_SUFFIXES)


def dedupe_key(endpoint: str | None, site: str) -> tuple[str, str]:
    """Backend-host based identity. Returns (key, backend_host).

    The identity is the registrable domain of the backend the relay calls. When
    that backend belongs to the model vendor itself (api.openai.com), the quota
    belongs to the relay, so the relay's own domain is the identity instead.
    """
    backend = host_of(endpoint or "")
    site_host = host_of(site or "")
    if backend and not is_first_party(backend):
        dom = registrable_domain(backend)
    elif backend and is_first_party(backend):
        dom = registrable_domain(site_host or backend)
    else:
        dom = registrable_domain(site_host)
    return f"dom:{dom}", backend


def checkin_usd(amount: float | None, unit: str | None, host: str = "", raw: str = "") -> tuple[float | None, str]:
    """USD-equivalent daily value. Never invents a points->USD rate."""
    if amount is None or unit is None:
        return None, "none"
    if unit == "usd":
        if re.search(r"(注册|首日|一次性|one[\s-]?time|welcome|signup bonus|开户)", raw or "", re.I):
            return None, "one-time"
        conf = "high" if amount <= 20 else "medium"
        return round(amount, 4), conf
    if unit == "cny":
        if re.search(r"(注册|首日|一次性|one[\s-]?time|welcome)", raw or "", re.I):
            return None, "one-time"
        return round(amount * CNY_USD_RATE, 4), "medium"
    if unit == "points":
        ratios = read_json(DATA / "points_ratios.json", {}) or {}
        # global default rate, applied when no host-specific entry matches.
        # Set only when a provider publishes a 积分→USD/CNY conversion.
        default = ratios.get("_default", {}).get("points_per_usd")
        host_rate = None
        for key, spec in ratios.items():
            if key.startswith("_"):
                continue
            if key in (host or ""):
                host_rate = (spec or {}).get("points_per_usd")
                break
        per_usd = host_rate or default
        if per_usd:
            return round(amount / float(per_usd), 4), "medium"
        # no published rate anywhere: never fabricate one
        return None, "points"
    return None, "low"


def checkin_factor(usd: float | None) -> float:
    if not usd or usd <= 0:
        return 0.0
    return min(1.0, math.log10(1 + usd) / math.log10(6))


def rate_factor(rpm: int | None, rpd: int | None) -> float:
    if rpm is None:
        base = 0.72
    elif rpm >= 20:
        base = 1.0
    elif rpm < 4:
        base = 0.40
    else:
        base = 0.40 + 0.60 * (math.log10(rpm / 4) / math.log10(5))
    if rpd is not None and rpd < 200:
        base *= 0.85
    return round(min(1.0, max(0.25, base)), 3)


SIGNUP_SCORE = {"github": 1.0, "oauth": 0.95, "email": 0.9, "invite": 0.8,
                "phone": 0.7, "unknown": 0.75, "linuxdo": 0.6, "card": 0.6}

SIGNUP_MATCHERS = [
    ("github", re.compile(r"github", re.I)),
    ("oauth", re.compile(r"(google\s*(?:login|sign)|oauth)", re.I)),
    ("email", re.compile(r"(邮箱|电子邮件|\bemail\b)", re.I)),
    ("invite", re.compile(r"(邀请|invite)", re.I)),
    ("phone", re.compile(r"(手机|短信|phone)", re.I)),
    ("linuxdo", re.compile(r"(linux\.?\s*do)", re.I)),
    ("card", re.compile(r"(信用卡|credit card|银行卡)", re.I)),
]


def best_signup(raw: str | None) -> str:
    """A provider that offers both GitHub and linux.do should count as GitHub:
    the easiest available door is what a new user actually walks through."""
    if not raw:
        return "unknown"
    hits = [name for name, pat in SIGNUP_MATCHERS if pat.search(raw)]
    if not hits:
        return "unknown"
    return max(hits, key=lambda n: SIGNUP_SCORE.get(n, 0))


def signup_from_notes(notes: list[str], raw: str | None) -> str:
    method = best_signup(raw)
    if method != "unknown":
        return method
    for n in notes:
        if n.startswith("signup:"):
            return n.split(":", 1)[1]
    return "unknown"


# ---------------------------------------------------------------------------
# Load + parse
# ---------------------------------------------------------------------------

def load_documents() -> list[dict]:
    docs = []
    for path in sorted((ROOT / "scraped").glob("*.json")):
        doc = read_json(path)
        if doc and doc.get("body"):
            docs.append(doc)
    return docs


def main() -> int:
    sources = {norm_url(s.url): s for s in load_sources()}
    agg_hosts = {registrable_domain(host_of(s.url)) for s in sources.values()
                 if s.role in ("agg", "intel", "modelindex")}
    docs = load_documents()
    log(f"{len(docs)} cached documents, {len(sources)} registered sources")

    baked = read_json(DATA / "model_tiers.json", {}) or {}
    index = ModelIndex(baked)

    candidates = []
    corroboration: dict[str, int] = defaultdict(int)
    corr_names: dict[str, str] = {}
    live_tiers = None
    free_model_index: list[str] = []
    parsed_stats = []

    for doc in docs:
        url = norm_url(doc["url"])
        src = sources.get(url)
        kind = src.kind if src else ("markdown" if "raw.githubusercontent" in url else "html")
        role = src.role if src else "seed"
        res = parse_document(url, doc["body"], kind, role, index)
        parsed_stats.append({"url": url, "role": role, "candidates": len(res["candidates"]),
                             "domains": len(res["domains"]), "bytes": doc.get("bytes")})
        if res.get("tiers") and len(res["tiers"]["models"]) >= 40:
            live_tiers = res["tiers"]
        if res.get("free_models"):
            free_model_index = res["free_models"]
        for dom, count in res["domains"].items():
            corroboration[dom] += count
        for cand in res["candidates"]:
            candidates.append(cand)

    tier_source = "baked:modelbenchmark-2026-11-25"
    if live_tiers:
        write_json(DATA / "tier_index.json", {"generated_at": iso(), "source": "https://modelbenchmark.io/leaderboard",
                                              "models": live_tiers["models"]})
        index = ModelIndex({"models": live_tiers["models"]})
        tier_source = "live:modelbenchmark.io"
        log(f"tier index refreshed from live leaderboard: {len(live_tiers['models'])} models")

    log(f"{len(candidates)} raw candidates from seed sources; live tiers={tier_source}")

    # ---- normalize + score each candidate
    rows: dict[str, dict] = {}
    for cand in candidates:
        name = clean_provider(cand.provider)
        if not name:
            continue
        endpoint = norm_url(cand.endpoint_url) if cand.endpoint_url else None
        site = cand.site_url
        if not site:
            m = DOMAIN_IN_NAME.search(cand.provider)
            if m:
                site = f"https://{m.group(1).lower()}"
        key, backend = dedupe_key(endpoint, site)
        models = list(dict.fromkeys(cand.models))
        best_model, best_pct = index.best(models)
        usd, conf = checkin_usd(cand.checkin_amount, cand.checkin_unit, host_of(site) or backend, cand.checkin_raw or "")
        method = signup_from_notes(cand.notes, cand.signup_raw)
        signup_score = SIGNUP_SCORE.get(method, 0.75)
        rf = rate_factor(cand.rpm, cand.rpd)
        cf = checkin_factor(usd)
        ambiguous = any("ambiguous" in n for n in cand.notes)
        score = 0.0
        if best_pct is not None:
            score = best_pct * (0.55 + 0.45 * cf) * rf * signup_score * (0.9 if ambiguous else 1.0)
        row = {
            "id": sha256(key)[:12],
            "provider": name,
            "endpoint_url": endpoint or "",
            "backend_host": backend,
            "dedupe_key": key,
            "models_free": len(models),
            "best_free_model": best_model or "",
            "tier_score": best_pct,
            "tier_source": tier_source if best_pct is not None else "",
            "checkin_raw": cand.checkin_raw or "",
            "checkin_daily_value": cand.checkin_amount,
            "checkin_currency": cand.checkin_unit or "",
            "checkin_usd_equiv": usd,
            "checkin_confidence": conf,
            "rate_limit_raw": cand.rate_raw or "",
            "rpm": cand.rpm,
            "rpd": cand.rpd,
            "signup_method": method,
            "signup_score": signup_score,
            "status": STATUS_LABEL.get(cand.status, "unknown"),
            "sources": [cand.source],
            "first_seen": iso(),
            "last_seen": iso(),
            "notes": list(dict.fromkeys(cand.notes)),
            "_models": models,
            "_score": round(score, 4),
            "_confidence": cand.confidence,
            "_site_url": site,
            "_signup_raw": cand.signup_raw or "",
            "_models_count": cand.models_count,
            "_quota_score": round(100 * cf * rf * signup_score, 2),
        }
        prev = rows.get(key)
        if prev is None:
            rows[key] = row
            continue
        # merge: keep the richer record, union the facts
        merged = prev if (prev["_confidence"], prev["_score"]) >= (row["_confidence"], row["_score"]) else row
        other = row if merged is prev else prev
        merged["sources"] = list(dict.fromkeys(merged["sources"] + other["sources"]))
        merged["_models"] = list(dict.fromkeys(merged["_models"] + other["_models"]))
        merged["notes"] = list(dict.fromkeys(merged["notes"] + other["notes"]))
        for f in ("endpoint_url", "best_free_model", "checkin_raw", "rate_limit_raw"):
            if not merged[f] and other[f]:
                merged[f] = other[f]
        for f in ("tier_score", "checkin_usd_equiv", "checkin_daily_value", "rpm", "rpd"):
            if merged[f] is None and other[f] is not None:
                merged[f] = other[f]
        if merged["checkin_currency"] in ("", None) and other["checkin_currency"]:
            merged["checkin_currency"] = other["checkin_currency"]
        # a known signup path beats an unknown one, a cheaper path beats a harder one
        if not merged["_signup_raw"] and other["_signup_raw"]:
            merged["_signup_raw"] = other["_signup_raw"]
        if other["signup_method"] != "unknown" and (
            merged["signup_method"] == "unknown" or other["signup_score"] > merged["signup_score"]
        ):
            merged["signup_method"] = other["signup_method"]
            merged["signup_score"] = other["signup_score"]
        better = best_signup(merged["_signup_raw"])
        if better != "unknown" and SIGNUP_SCORE.get(better, 0) >= merged["signup_score"]:
            merged["signup_method"], merged["signup_score"] = better, SIGNUP_SCORE[better]
        if other["status"] in ("online",) and merged["status"] in ("unknown", "unverified"):
            merged["status"] = other["status"]
        # recompute the score with the unioned facts
        merged["_models"] = list(dict.fromkeys(merged["_models"]))
        bm, bp = index.best(merged["_models"])
        merged["best_free_model"] = merged["best_free_model"] or (bm or "")
        merged["tier_score"] = merged["tier_score"] if merged["tier_score"] is not None else bp
        merged["models_free"] = len(merged["_models"])
        # recompute the USD equivalent from the merged facts so a points
        # currency never inherits a USD value from a cny/usd candidate
        merged["checkin_usd_equiv"], merged["checkin_confidence"] = checkin_usd(
            merged["checkin_daily_value"], merged["checkin_currency"],
            host_of(merged["_site_url"]), merged["checkin_raw"] or "")
        if merged["tier_score"] is not None:
            merged["_score"] = round(
                merged["tier_score"]
                * (0.55 + 0.45 * checkin_factor(merged["checkin_usd_equiv"]))
                * rate_factor(merged["rpm"], merged["rpd"])
                * merged["signup_score"],
                4,
            )
        rows[key] = merged

    # ---- history (first_seen survives runs)
    seen = read_json(DATA / "seen.json", {}) or {}
    for key, row in rows.items():
        rec = seen.get(key)
        if rec:
            row["first_seen"] = rec.get("first_seen", row["first_seen"])
        seen[key] = {"first_seen": row["first_seen"], "last_seen": row["last_seen"], "provider": row["provider"]}
    write_json(DATA / "seen.json", seen)

    # ---- partition
    all_rows = list(rows.values())

    def has_quota(row: dict) -> bool:
        return bool(row["checkin_usd_equiv"]) or bool(row["checkin_daily_value"])

    offline = [r for r in all_rows if r["status"] == "offline"]
    scored, quota_only, unverified, dropped = [], [], [], []
    for r in all_rows:
        if r["status"] == "offline":
            continue
        if r["tier_score"] is not None:
            scored.append(r)
        elif has_quota(r):
            quota_only.append(r)
        elif plausible_provider(r, agg_hosts):
            unverified.append(r)
        else:
            dropped.append(r)

    for r in all_rows:
        if r["tier_score"] is not None and not r["tier_source"]:
            r["tier_source"] = tier_source
        # notes are unioned during merges; drop the ones that duplicate a column
        r["notes"] = [n for n in r["notes"] if not n.startswith("signup:")]
        if has_quota(r):
            r["notes"] = [n for n in r["notes"] if not n.startswith("navigation entry")]
        r["below_baseline"] = bool(r["tier_score"] is not None and r["tier_score"] < BASELINE_PERCENTILE)
        r["corroborations"] = corroboration.get(registrable_domain(r["backend_host"] or host_of(r["_site_url"]) or ""), 0)

    scored.sort(key=lambda r: (-r["_score"], -(r["tier_score"] or 0), r["provider"].lower()))
    quota_only.sort(key=lambda r: (-(r["_quota_score"] or 0), r["provider"].lower()))
    ranked = scored + quota_only
    unverified.sort(key=lambda r: r["provider"].lower())

    # ---- write
    write_outputs(ranked, scored, quota_only, unverified, offline, dropped, all_rows, parsed_stats,
                  tier_source, free_model_index, corroboration)
    log(f"ranked={len(scored)} quota_only={len(quota_only)} unverified={len(unverified)} offline={len(offline)}")
    top = ranked[0] if ranked else None
    if top:
        log(f"#1 {top['provider']} score={top['_score']} tier={top['tier_score']} checkin=${top['checkin_usd_equiv']}")
    return 0


def write_outputs(ranked, scored, quota_only, unverified, offline, dropped, all_rows, parsed_stats,
                  tier_source, free_models, corroboration):
    public = lambda r: {k: v for k, v in r.items() if not k.startswith("_")} | {
        "site_url": r["_site_url"], "models": r["_models"], "models_count": r["_models_count"],
        "score": r["_score"], "quota_score": r["_quota_score"]}
    write_json(ROOT / "free_llm_providers.json", {
        "meta": {
            "generated_at": iso(), "prompt_version": PROMPT_VERSION, "tier_source": tier_source,
            "cny_usd_rate": CNY_USD_RATE, "baseline_percentile": BASELINE_PERCENTILE,
            "counts": {"ranked": len(scored), "quota_only": len(quota_only), "unverified": len(unverified),
                       "offline": len(offline), "total": len(all_rows)},
            "openrouter_free_models": len(free_models),
        },
        "providers": [public(r) for r in ranked],
        "unverified": [public(r) for r in unverified],
        "offline": [public(r) for r in offline],
    })

    with open(ROOT / "free_llm_providers.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in ranked + unverified + offline:
            out = {k: r.get(k) for k in CSV_COLUMNS}
            out["models_free"] = len(r["_models"])
            out["sources"] = " ".join(r["sources"])
            out["notes"] = " | ".join(r["notes"])
            w.writerow(out)

    write_json(DATA / "unverified_providers.json", [public(r) for r in unverified])
    write_json(DATA / "corroboration.json", dict(sorted(corroboration.items(), key=lambda kv: -kv[1])[:400]))

    atomic_write(ROOT / "LEADERBOARD.md", render_markdown(scored, quota_only, unverified, tier_source))
    build_status(ranked, unverified, all_rows, parsed_stats, tier_source, free_models)
    write_dashboard(scored, quota_only, unverified, offline, tier_source)


def plausible_provider(row: dict, agg_hosts: set[str]) -> bool:
    """Drop navigation noise: numeric labels, aggregator self-links, empty sites."""
    name = row["provider"].strip()
    if len(name) < 3 or name.isdigit():
        return False
    site = row["_site_url"] or row["endpoint_url"] or ""
    if not site:
        return False
    host = registrable_domain(host_of(site))
    if not host or host in agg_hosts:
        return False
    label = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
    if label and re.sub(r"[^a-z0-9]", "", name.lower()) == label and len(name) <= 4:
        return False
    return True


def fmt_usd(r: dict) -> str:
    if r["checkin_usd_equiv"]:
        return f"${r['checkin_usd_equiv']:.2f}"
    if r["checkin_usd_equiv"] == 0:
        return "—"
    if r["checkin_daily_value"] is not None:
        unit = {"points": "pts", "cny": "CNY"}.get(r["checkin_currency"], r["checkin_currency"] or "")
        return f"{r['checkin_daily_value']:g} {unit}"
    if r["checkin_currency"] == "points":
        return "points"
    if r["checkin_confidence"] == "one-time":
        return "signup only"
    return "—"


def fmt_rate(r: dict) -> str:
    parts = []
    if r["rpm"]:
        parts.append(f"{r['rpm']} RPM")
    if r["rpd"]:
        parts.append(f"{r['rpd']} RPD")
    return " · ".join(parts) or (r["rate_limit_raw"][:28] if r["rate_limit_raw"] else "—")


def render_markdown(scored: list[dict], quota_only: list[dict], unverified: list[dict], tier_source: str) -> str:
    ranked = scored
    top = scored[:25]
    lines = [
        "# Free LLM API leaderboard",
        "",
        f"Auto-generated {iso()} by the GitHub Actions pipeline in this repo. "
        f"Tier scores: `{tier_source}`. Full data: `free_llm_providers.csv` / `.json`.",
        "",
        f"**{len(scored)}** ranked providers · **{len(quota_only)}** more with quota but no verified model tier · "
        f"**{len(unverified)}** unverified entries · baseline = MiniMax M3.1 Flash (p50).",
        "",
        "| # | Provider | Best free model | Tier | Daily check-in | Rate limit | Signup | Endpoint | Score |",
        "|---|----------|-----------------|------|----------------|------------|--------|----------|-------|",
    ]
    for i, r in enumerate(top, 1):
        ep = r["endpoint_url"] or r["_site_url"] or ""
        ep_md = f"[{re.sub(r'^https?://', '', ep).split('/')[0]}]({ep})" if ep else "—"
        model = r["best_free_model"] or "—"
        tier = f"{r['tier_score']}" if r["tier_score"] is not None else "—"
        flag = " ⚠️" if "ambiguous" in " ".join(r["notes"]) else ""
        lines.append(
            f"| {i} | **{r['provider']}** | {model} | {tier} | {fmt_usd(r)}{flag} | {fmt_rate(r)} | "
            f"{r['signup_method']} | {ep_md} | {r['_score']:.1f} |"
        )

    if quota_only:
        lines += ["", f"## Generous quotas, model tier not verifiable ({len(quota_only)})", "",
                  "These publish a daily quota but never name a benchmark-listed model, so they cannot be scored "
                  "on quality. Sorted by normalized check-in value; verify the model list before trusting one.", "",
                  "| Provider | Daily check-in | Models advertised | Rate limit | Signup | Endpoint |",
                  "|---|---|---|---|---|---|"]
        for r in quota_only[:15]:
            ep = r["endpoint_url"] or r["_site_url"] or ""
            ep_md = f"[{re.sub(r'^https?://', '', ep).split('/')[0]}]({ep})" if ep else "—"
            lines.append(f"| {r['provider']} | {fmt_usd(r)} | {r['_models_count'] or '—'} | {fmt_rate(r)} | "
                         f"{r['signup_method']} | {ep_md} |")

    below = [r for r in ranked if r["below_baseline"]]
    if below:
        lines += ["", f"## Below baseline ({len(below)})", "",
                  "Kept for coverage, hidden from the main table: their best free model scores under the "
                  "MiniMax M3.1 Flash baseline and they do not advertise exceptional speed.", "",
                  "| Provider | Best free model | Tier | Daily check-in |", "|---|---|---|---|"]
        for r in below[:15]:
            lines.append(f"| {r['provider']} | {r['best_free_model'] or '—'} | {r['tier_score']} | {fmt_usd(r)} |")

    lines += ["", "## How the score works", "",
              "`score = tier × (0.55 + 0.45·checkin) × rate × signup × ambiguity`",
              "",
              "- `tier` — percentile of the best free model on modelbenchmark.io (0–100).",
              "- `checkin` — `log10(1+USD)/log10(6)`, so $5/day saturates and $0 cannot zero out a provider.",
              "- `rate` — 0.40 under 4 RPM, 1.00 at 20+ RPM, log-scaled between; unknown = 0.72.",
              "- `signup` — GitHub 1.0, email 0.9, invite 0.8, phone 0.7, linux.do-only 0.6, card 0.6.",
              "- Points-only check-ins are never converted to USD without a published rate.",
              "",
              "Data is scraped from public welfare lists and provider pages; quotas change without notice. "
              "This is an index, not an endorsement — never send secrets or production code to an unvetted relay.",
              ""]
    return "\n".join(lines)


def build_status(ranked, unverified, all_rows, parsed_stats, tier_source, free_models):
    health = read_json(DATA / "source_health.json", {}) or {}
    src = health.get("sources", [])
    ok = [s for s in src if s.get("ok")]
    failed = [s for s in src if not s.get("ok")]
    scored = [r for r in ranked if r["tier_score"] is not None]
    write_json(ROOT / "status.json", {
        "last_updated": iso(),
        "prompt_version": PROMPT_VERSION,
        "coverage": {
            "sources_configured": len(src),
            "sources_ok": len(ok),
            "sources_failed": len(failed),
            "providers_total": len(all_rows),
            "providers_ranked": len(scored),
            "providers_quota_only": len(ranked) - len(scored),
            "providers_unverified": len(unverified),
            "openrouter_free_models": len(free_models),
        },
        "tier_source": tier_source,
        "parsed": parsed_stats,
        "failed_sources": [{"url": s["url"], "error": s.get("error") or s.get("notes")} for s in failed],
    })


def _dash_row(r):
    return ({k: v for k, v in r.items() if not k.startswith("_")} | {
        "site_url": r["_site_url"], "models": r["_models"], "models_count": r["_models_count"],
        "score": r["_score"], "quota_score": r["_quota_score"], "below_baseline": r["below_baseline"],
    })


def write_dashboard(scored, quota_only, unverified, offline, tier_source):
    payload = json.dumps({
        "generated_at": iso(),
        "tier_source": tier_source,
        "scored": [_dash_row(r) for r in scored],
        "quota_only": [_dash_row(r) for r in quota_only],
        "unverified": [_dash_row(r) for r in unverified],
        "offline": [_dash_row(r) for r in offline],
    }, ensure_ascii=False)
    html = DASHBOARD_TEMPLATE.replace("/*__DATA__*/null", payload)
    atomic_write(DIST / "index.html", html)


DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Free LLM API leaderboard</title>
<style>
:root{--bg:#0b0d12;--card:#151922;--ink:#e8ecf4;--dim:#8b94a7;--acc:#63e6be;--warn:#ffd43b;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--dim);margin:0 0 14px;font-size:13px}
.tabs{display:flex;gap:6px;margin:0 0 12px;flex-wrap:wrap}
.tabs button{background:#121722;border:1px solid #263041;color:var(--dim);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:13px}
.tabs button.active{background:var(--acc);color:#0b0d12;border-color:var(--acc);font-weight:600}
input{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #263041;background:#0f131b;color:var(--ink);margin-bottom:12px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid #1e2431;text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:600;position:sticky;top:0;background:var(--bg);cursor:pointer;white-space:nowrap}
tr:hover td{background:#121722}.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.chip{font-size:11px;padding:1px 6px;border-radius:999px;background:#1d2532;color:var(--dim)}
.bar{height:6px;border-radius:3px;background:linear-gradient(90deg,#63e6be,#4dabf7);min-width:2px;display:inline-block}
.below{color:var(--warn)} .offline{color:var(--bad)}
.models{color:var(--dim);font-size:12px}
</style></head><body><main>
<h1>Free LLM API leaderboard</h1>
<p class="sub" id="sub"></p>
<div class="tabs" id="tabs"></div>
<input id="q" placeholder="Filter by provider, model, endpoint, signup…">
<table><thead><tr>
<th data-k="rank">#</th><th data-k="provider">Provider</th><th data-k="best_free_model">Best free model</th>
<th data-k="tier_score" class="num">Tier</th><th data-k="checkin_usd_equiv" class="num">Check-in/day</th>
<th data-k="rpm">Rate</th><th data-k="signup_method">Signup</th><th data-k="endpoint_url">Endpoint</th>
<th data-k="score" class="num">Score</th>
</tr></thead><tbody id="rows"></tbody></table>
<p class="sub" style="margin-top:18px">Quotas change without notice. Index only — not an endorsement. Full data in <code>free_llm_providers.csv</code> / <code>.json</code>.</p>
</main><script>
const D=/*__DATA__*/null;
let sortK="score",asc=false,current="scored";
const TABS=[{k:"scored",label:"Scored"},{k:"quota_only",label:"Quota only"},{k:"unverified",label:"Unverified"},{k:"offline",label:"Offline"}];
const fmtU=v=>v==null?"—":"$"+v.toFixed(2);
const fmtR=r=>[r.rpm?r.rpm+" RPM":null,r.rpd?r.rpd+" RPD":null].filter(Boolean).join(" · ")||"—";
const host=u=>(u||"").replace(/^https?:\\/\\//,"").split("/")[0];
document.getElementById("sub").textContent=
  D.scored.length+" scored · "+D.quota_only.length+" quota-only · "+D.unverified.length+" unverified · "+D.offline.length+" offline · tiers: "+D.tier_source+" · updated "+new Date(D.generated_at).toLocaleString();
const tabs=document.getElementById("tabs");
TABS.forEach(t=>{const b=document.createElement("button");b.textContent=t.label+" ("+D[t.k].length+")";b.dataset.k=t.k;
  b.addEventListener("click",()=>{current=t.k;document.querySelectorAll(".tabs button").forEach(x=>x.classList.remove("active"));b.classList.add("active");rows();});tabs.appendChild(b);});
tabs.querySelector("button").classList.add("active");
function rows(){
 const q=document.getElementById("q").value.toLowerCase();
 let rs=(D[current]||[]).filter(r=>!q||JSON.stringify(r).toLowerCase().includes(q));
 rs=rs.slice().sort((a,b)=>{let x=a[sortK],y=b[sortK];
   if(x==null)return 1;if(y==null)return -1;
   return (typeof x==="string"?x.localeCompare(y):x-y)*(asc?1:-1);});
 document.getElementById("rows").innerHTML=rs.map((r,i)=>{
   const ep=r.endpoint_url||r.site_url||"";
   const hp=r.site_url||ep;
   const name=hp?('<a href="'+hp+'" target="_blank" rel="noopener">'+r.provider+'</a>'):r.provider;
   const epLink=ep?('<a href="'+ep+'" target="_blank" rel="noopener">'+host(ep)+'</a>'):"—";
   const models=(r.models||[]).slice(0,4).join(", ");
   const bar=current==="scored"?('<span class="bar" style="width:'+Math.round((r.score||0)/1.2)+'px"></span> '+(r.score||0).toFixed(1)):"—";
   return `<tr><td class="num">${i+1}</td><td><b>${name}</b>${models?'<div class="models">'+models+'</div>':''}</td>
   <td>${r.best_free_model||"—"}</td><td class="num">${r.tier_score??"—"}</td>
   <td class="num">${fmtU(r.checkin_usd_equiv)}</td><td>${fmtR(r)}</td>
   <td><span class="chip">${r.signup_method}</span></td>
   <td>${epLink}</td><td class="num">${bar}</td></tr>`;
 }).join("");
}
document.getElementById("q").addEventListener("input",rows);
document.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("click",()=>{
 const k=th.dataset.k;if(k==="rank"){sortK="score";asc=false}else{asc=(sortK===k)?!asc:false;sortK=k}rows();}));
rows();
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
