"""Deterministic extraction from cached documents.

No LLM, no API keys: every field is parsed with a pattern that can be
inspected and unit-tested. Anything ambiguous is kept verbatim and marked
`needs_review` instead of being guessed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
TIERS_FILE = ROOT / "data" / "model_tiers.json"

# ---------------------------------------------------------------------------
# Model index
# ---------------------------------------------------------------------------

MODEL_FAMILY_PERCENTILE = {
    # conservative fallbacks for model families whose exact version is unknown
    "gpt-6": 97, "gpt-5": 80, "gpt-4": 40, "gpt-oss": 43,
    "claude-opus": 90, "claude-sonnet": 80, "claude-haiku": 70, "claude": 60,
    "gemini-3": 85, "gemini-2.5": 55, "gemini": 50,
    "deepseek": 75, "kimi": 70, "glm": 65, "qwen": 65, "minimax": 55,
    "grok": 78, "mistral": 55, "llama": 30, "o3": 70, "o4": 50,
}


def _norm(s: str) -> str:
    """Lowercase, strip diacritics/punctuation so 'GLM-5.3' == 'glm 5 3'."""
    s = unicodedata.normalize("NFKD", s)
    s = s.replace("–", "-").replace("—", "-").replace("·", " ")
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())


@dataclass
class ModelEntry:
    model: str
    percentile: int
    lab: str
    rank: int


class ModelIndex:
    """Maps free-text model mentions to a benchmark percentile."""

    def __init__(self, tiers: dict | None = None):
        self.entries: list[ModelEntry] = []
        self.alias: dict[str, ModelEntry] = {}
        if tiers:
            for m in tiers.get("models", []):
                if m.get("percentile") is None:
                    continue
                entry = ModelEntry(m["model"], int(m["percentile"]), m.get("lab", ""), int(m.get("rank", 0)))
                self.entries.append(entry)
                self.alias[_norm(entry.model)] = entry
                alias_id = re.sub(r"[^a-z0-9.-]+", "-", entry.model.lower()).strip("-")
                self.alias.setdefault(_norm(alias_id), entry)
        # longest alias first so 'Claude Opus 5.5' wins over 'Claude Opus 5'
        self._keys = sorted(self.alias, key=len, reverse=True)

    @classmethod
    def load(cls) -> "ModelIndex":
        if TIERS_FILE.exists():
            try:
                return cls(json.loads(TIERS_FILE.read_text(encoding="utf-8")))
            except ValueError:
                pass
        return cls()

    def find(self, text: str) -> list[ModelEntry]:
        flat = _norm(text)
        found: list[ModelEntry] = []
        consumed = flat
        for key in self._keys:
            if len(key) < 3:
                continue
            if key in consumed:
                entry = self.alias[key]
                if all(entry.model != e.model for e in found):
                    found.append(entry)
                consumed = consumed.replace(key, " ")
        return found

    def percentile_for(self, model_name: str) -> int | None:
        e = self.alias.get(_norm(model_name))
        if e:
            return e.percentile
        for fam, pct in MODEL_FAMILY_PERCENTILE.items():
            if _norm(fam) in _norm(model_name):
                return pct
        return None

    def best(self, models: Iterable[str]) -> tuple[str | None, int | None]:
        best_name, best_pct = None, None
        for name in models:
            pct = self.percentile_for(name)
            if pct is not None and (best_pct is None or pct > best_pct):
                best_name, best_pct = name, pct
        return best_name, best_pct


# ---------------------------------------------------------------------------
# Candidate record
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    provider: str
    site_url: str = ""
    endpoint_url: str | None = None
    models: list[str] = field(default_factory=list)
    models_count: int | None = None
    checkin_raw: str | None = None
    checkin_amount: float | None = None
    checkin_unit: str | None = None
    checkin_period: str | None = None
    signup_raw: str | None = None
    rate_raw: str | None = None
    rpm: int | None = None
    rpd: int | None = None
    status: str = "unknown"
    source: str = ""
    role: str = "seed"
    confidence: float = 0.5
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

USD_RE = re.compile(r"(?:US)?\$\s*([\d,]+(?:\.\d+)?)", re.I)
CNY_RE = re.compile(r"(?:[¥￥]\s*([\d,]+(?:\.\d+)?))|(?:([\d,]+(?:\.\d+)?)\s*(?:元|CNY|RMB))", re.I)
POINTS_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:积\s*分|点数|point(?:s)?|额度|credit(?:s)?|币)", re.I)
CHECKIN_CUE = re.compile(r"(每日签到|每天签到|签到|check[\s-]?in|daily\s+(?:free\s+)?(?:credit|quota|allowance|refresh))", re.I)
AMBIGUOUS_CUE = re.compile(r"(随机|待确认|随机额度|varies|random|unknown|未公开|视情况)", re.I)
PERIOD_DAY = re.compile(r"(每日|每天|/天|/日|daily|per day|each day|every day|次/天)", re.I)


def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_amount(text: str) -> tuple[float | None, str | None]:
    """First money-ish amount in a snippet -> (value, unit)."""
    if not text:
        return None, None
    usd = USD_RE.search(text)
    cny = CNY_RE.search(text)
    pts = POINTS_RE.search(text)
    best = min([m for m in (usd, cny, pts) if m], key=lambda m: m.start(), default=None)
    if best is None:
        return None, None
    if best is usd:
        return _num(best.group(1)), "usd"
    if best is cny:
        return _num(best.group(1) or best.group(2)), "cny"
    return _num(best.group(1)), "points"


def parse_checkin(text: str) -> dict:
    """Return raw wording + normalized value for a daily check-in."""
    if not text:
        return {}
    m = CHECKIN_CUE.search(text)
    if not m:
        # a bare "$2 per day" still counts
        if PERIOD_DAY.search(text) and (USD_RE.search(text) or CNY_RE.search(text) or POINTS_RE.search(text)):
            cue = PERIOD_DAY.search(text)
        else:
            return {}
    else:
        cue = m
    # the sentence around the cue is the honest quote: no invented context
    snippet = _best_segment(text, cue.start())
    # the amount that belongs to the cue is the first one AFTER it
    amount, unit = parse_amount(text[cue.start(): cue.start() + 80])
    if amount is None:
        amount, unit = parse_amount(snippet)
    ambiguous = bool(AMBIGUOUS_CUE.search(snippet))
    return {
        "checkin_raw": snippet[:160],
        "checkin_amount": amount,
        "checkin_unit": unit,
        "checkin_period": "day",
        "checkin_ambiguous": ambiguous or amount is None,
    }


def parse_checkin_cell(text: str) -> dict:
    """A cell that is *already* the daily check-in column (e.g. '✅ $25')."""
    clean = strip_md(text or "")
    if not clean or re.fullmatch(r"[❌✖️x\-—–/]+", clean):
        return {"checkin_raw": clean or None, "checkin_amount": 0.0, "checkin_unit": "usd",
                "checkin_period": "day", "checkin_ambiguous": False}
    if AMBIGUOUS_CUE.search(clean):
        return {"checkin_raw": clean, "checkin_amount": None, "checkin_unit": None,
                "checkin_period": "day", "checkin_ambiguous": True}
    amount, unit = parse_amount(clean)
    if amount is None:
        return {}
    return {"checkin_raw": clean[:160], "checkin_amount": amount, "checkin_unit": unit,
            "checkin_period": "day", "checkin_ambiguous": False}


def _best_segment(text: str, pos: int) -> str:
    """Shortest sentence containing position `pos`, collapsing whitespace."""
    spans = [m.span() for m in re.finditer(r"[^\n。；;•·]+", text)]
    for start, end in spans:
        if start <= pos < end:
            seg = text[start:end].strip(" -*>`")
            return re.sub(r"\s+", " ", seg)
    start = max(0, pos - 40)
    return re.sub(r"\s+", " ", text[start: pos + 90]).strip()


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

RPM_PATTERNS = [
    re.compile(r"(\d[\d,]*)\s*(?:RPM|rpm|req(?:uest)?s?\s*(?:per|/)\s*min(?:ute)?)", re.I),
    re.compile(r"每分钟\s*(\d[\d,]*)\s*次"),
    re.compile(r"(\d[\d,]*)\s*次\s*/\s*分(?:钟)?"),
]
RPD_PATTERNS = [
    re.compile(r"(\d[\d,]*)\s*(?:RPD|rpd|req(?:uest)?s?\s*(?:per|/)\s*day)", re.I),
    re.compile(r"每(?:日|天)\s*(\d[\d,]*)\s*次"),
    re.compile(r"(\d[\d,]*)\s*次\s*/\s*(?:天|日)"),
]
RPS_PATTERNS = [re.compile(r"(\d[\d,]*)\s*(?:RPS|req(?:uest)?s?\s*(?:per|/)\s*sec)", re.I)]


def parse_rates(text: str) -> dict:
    out = {"rpm": None, "rpd": None, "rate_raw": None}
    if not text:
        return out
    hits: list[tuple[int, str]] = []
    for pat in RPM_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.start(), f"rpm:{m.group(1)}"))
    for pat in RPD_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.start(), f"rpd:{m.group(1)}"))
    for pat in RPS_PATTERNS:
        for m in pat.finditer(text):
            v = _num(m.group(1))
            if v:
                hits.append((m.start(), f"rpm:{int(v * 60)}"))
    if not hits:
        return out
    hits.sort()
    for _, hit in hits:
        kind, val = hit.split(":")
        num = _num(val)
        if kind == "rpm" and out["rpm"] is None and num is not None and num < 100000:
            out["rpm"] = int(num)
        elif kind == "rpd" and out["rpd"] is None and num is not None:
            out["rpd"] = int(num)
    start = max(0, hits[0][0] - 10)
    out["rate_raw"] = re.sub(r"\s+", " ", text[start: start + 90]).strip()
    return out


# ---------------------------------------------------------------------------
# Signup method
# ---------------------------------------------------------------------------

SIGNUP_PATTERNS = [
    ("card", re.compile(r"(信用卡|credit card|银行卡|充值才|需充值|card required)", re.I)),
    ("linuxdo", re.compile(r"(linux\.?\s*do|LINUX DO)", re.I)),
    ("github", re.compile(r"github[\s_-]*(?:oauth|登录|登陆|login|sign)", re.I)),
    ("phone", re.compile(r"(手机号|手机验证|短信验证|phone\s*(?:number|verification))", re.I)),
    ("email", re.compile(r"(邮箱|电子邮件|email)", re.I)),
    ("invite", re.compile(r"(邀请|invite|invitation|邀请码)", re.I)),
    ("oauth", re.compile(r"(oauth|google\s*(?:login|sign)|第三方登录)", re.I)),
]

SIGNUP_SCORE = {
    "github": 1.0, "oauth": 0.95, "email": 0.9, "invite": 0.8,
    "phone": 0.7, "unknown": 0.75, "linuxdo": 0.6, "card": 0.6,
}


def parse_signup(text: str) -> tuple[str, str | None]:
    if not text:
        return "unknown", None
    for name, pat in SIGNUP_PATTERNS:
        m = pat.search(text)
        if m:
            return name, re.sub(r"\s+", " ", text[max(0, m.start() - 20): m.start() + 60]).strip()[:160]
    return "unknown", None


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"'`\)\]\|，。、；]+", re.I)
ENDPOINT_HINT = re.compile(r"(/v1\b|/api\b|/v1beta|/openai|/anthropic|api\.|/chat/completions|/messages)", re.I)
NON_PROVIDER_HOSTS = {
    "github.com", "githubusercontent.com", "twitter.com", "x.com", "discord.gg", "discord.com",
    "t.me", "telegram.org", "youtube.com", "bilibili.com", "juejin.cn", "zhihu.com",
    "npmjs.com", "pypi.org", "google.com", "googleapis.com", "gstatic.com", "cloudflare.com",
    "w3.org", "schema.org", "mozilla.org", "apple.com", "microsoft.com", "jsdelivr.net",
    "fonts.googleapis.com", "googletagmanager.com", "clarity.ms", "sentry.io",
    "linux.do", "modelbenchmark.io", "openrouter.ai", "huggingface.co", "reddit.com",
    "stackoverflow.com", "medium.com", "wikipedia.org", "notion.site", "vercel.app",
}
DOC_PATH = re.compile(r"(docs?|documentation|/help|/faq|/blog|/wiki|/about|/terms|/privacy)", re.I)


def is_provider_url(url: str) -> bool:
    host = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", url, re.I) or [None, ""])[1].lower()) if url else ""
    if not host:
        return False
    for bad in NON_PROVIDER_HOSTS:
        if host == bad or host.endswith("." + bad):
            return False
    return True


def clean_url(url: str) -> str:
    url = url.rstrip(".,;:)）】」”\"'")
    # drop tracking params without importing common (parse.py stays standalone)
    if "?" in url:
        base, _, query = url.partition("?")
        keep = [p for p in query.split("&") if p and not re.match(r"(utm_|aff|ref|invite|code|i=|share)", p, re.I)]
        url = base + ("?" + "&".join(keep) if keep else "")
    return url


def pick_endpoint(urls: Iterable[str]) -> str | None:
    cands = [clean_url(u) for u in urls if is_provider_url(u)]
    scored = []
    for u in cands:
        score = 0
        if ENDPOINT_HINT.search(u):
            score += 2
        if re.search(r"/v1/?$", u):
            score += 2
        if DOC_PATH.search(u):
            score -= 3
        scored.append((score, len(u), u))
    scored.sort(reverse=True)
    return scored[0][2] if scored and scored[0][0] > 0 else None


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^)\s]+)\)")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
SEP_ROW = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def md_cells(line: str) -> list[str]:
    return [c.strip() for c in TABLE_ROW.match(line).group(1).split("|")]


def strip_md(text: str) -> str:
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = re.sub(r"[*_`>#]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def iter_md_tables(body: str) -> Iterable[tuple[list[str], list[list[str]]]]:
    lines = body.splitlines()
    i = 0
    while i < len(lines) - 1:
        if TABLE_ROW.match(lines[i]) and SEP_ROW.match(lines[i + 1]):
            header = [strip_md(c) for c in md_cells(lines[i])]
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and TABLE_ROW.match(lines[j]):
                rows.append(md_cells(lines[j]))
                j += 1
            yield header, rows
            i = j
        else:
            i += 1


def col(header: list[str], *names: str) -> int | None:
    for idx, h in enumerate(header):
        hn = _norm(h)
        for n in names:
            if _norm(n) in hn:
                return idx
    return None


def cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return strip_md(row[idx])


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def parse_status(text: str) -> str:
    if re.search(r"(🔴|离线|offline|停运|已关闭|关闭|dead|down)", text):
        return "offline"
    if re.search(r"(🟢|🟡|在线|online|running|正常)", text):
        return "online"
    return "unknown"


# ---------------------------------------------------------------------------
# Parser: welfare README (table + per-site sections)
# ---------------------------------------------------------------------------

def merge_into(dst: Candidate, src: Candidate) -> Candidate:
    """Fill only empty fields, keep the richer value, never downgrade confidence."""
    for f in ("site_url", "endpoint_url", "checkin_raw", "signup_raw", "rate_raw"):
        if not getattr(dst, f) and getattr(src, f):
            setattr(dst, f, getattr(src, f))
    for f in ("checkin_amount", "checkin_unit", "checkin_period", "rpm", "rpd", "models_count"):
        if getattr(dst, f) is None and getattr(src, f) is not None:
            setattr(dst, f, getattr(src, f))
    if dst.status == "unknown" and src.status != "unknown":
        dst.status = src.status
    for m in src.models:
        if m not in dst.models:
            dst.models.append(m)
    dst.notes = list(dict.fromkeys(dst.notes + src.notes))
    dst.confidence = max(dst.confidence, src.confidence)
    return dst


def parse_welfare_md(body: str, source: str, models: ModelIndex) -> list[Candidate]:
    out: dict[str, Candidate] = {}

    # 1) summary tables: only the one carrying quota columns is authoritative
    for header, rows in iter_md_tables(body):
        i_name = col(header, "站点", "site", "provider", "名称")
        if i_name is None:
            continue
        i_status = col(header, "状态", "status")
        i_checkin = col(header, "每日签到", "签到", "check-in", "daily")
        i_quota = col(header, "额度构成", "首日")
        i_models = col(header, "模型数", "models")
        i_proto = col(header, "协议", "protocol")
        i_link = col(header, "注册链接", "官网链接", "地址", "链接", "link", "url")
        if i_checkin is None and i_quota is None and i_status is None:
            # navigation table (name + link only): keep as unverified coverage
            if i_link is None:
                continue
            for row in rows:
                raw_name = row[i_name] if i_name < len(row) else ""
                link = LINK_RE.search(raw_name) or (LINK_RE.search(row[i_link]) if i_link < len(row) else None)
                name = strip_md(raw_name)
                if not name or len(name) > 60 or not link:
                    continue
                cand = Candidate(provider=name, site_url=clean_url(link.group(2)), source=source,
                                 confidence=0.45, status="unverified")
                cand.notes.append("navigation entry: no quota data published")
                key = _norm(name)
                out[key] = merge_into(out.get(key), cand) if key in out else cand
            continue
        for row in rows:
            raw_name = row[i_name] if i_name < len(row) else ""
            link = LINK_RE.search(raw_name)
            name = strip_md(raw_name)
            site = clean_url(link.group(2)) if link else ""
            if not name or len(name) > 60:
                continue
            cand = Candidate(provider=name, site_url=site, source=source, confidence=0.8)
            cand.status = parse_status(cell(row, i_status))
            if i_checkin is not None:
                info = parse_checkin_cell(cell(row, i_checkin))
                if info:
                    cand.checkin_raw = info.get("checkin_raw")
                    cand.checkin_amount = info.get("checkin_amount")
                    cand.checkin_unit = info.get("checkin_unit")
                    cand.checkin_period = info.get("checkin_period")
                    if info.get("checkin_ambiguous"):
                        cand.notes.append("check-in amount ambiguous")
                        cand.confidence = min(cand.confidence, 0.6)
            if cand.checkin_amount is None and i_quota is not None:
                info = parse_checkin(cell(row, i_quota))
                if info and info.get("checkin_amount") is not None:
                    cand.checkin_raw = cand.checkin_raw or info.get("checkin_raw")
                    cand.checkin_amount = info.get("checkin_amount")
                    cand.checkin_unit = info.get("checkin_unit")
                    cand.checkin_period = "day"
            mc = re.search(r"(\d+)", cell(row, i_models) or "")
            if mc:
                cand.models_count = int(mc.group(1))
            proto = cell(row, i_proto)
            if proto and proto not in ("—", "-"):
                cand.notes.append(f"protocols: {proto}")
            key = _norm(name)
            out[key] = merge_into(out.get(key), cand) if key in out else cand

    # 2) per-site detail sections
    sections = re.split(r"\n(?=###\s*\d+[.、]\s*)", body)
    for sec in sections:
        m = re.match(r"###\s*\d+[.、]\s*(.+)", sec.strip())
        if not m:
            continue
        name = strip_md(m.group(1)).strip()
        key = _norm(name)
        cand = out.get(key)
        if cand is None:
            cand = Candidate(provider=name, source=source, confidence=0.75)
            out[key] = cand
        else:
            cand.confidence = min(0.95, cand.confidence + 0.15)

        urls = [clean_url(u) for u in URL_RE.findall(sec)]
        api_urls = [u for u in urls if re.search(r"(/v1|/api|api\.)", u) and is_provider_url(u)]
        endpoint = pick_endpoint(api_urls)
        if endpoint:
            cand.endpoint_url = endpoint
        if not cand.site_url:
            home = [u for u in urls if is_provider_url(u) and not DOC_PATH.search(u)]
            if home:
                cand.site_url = home[0]

        sm = re.search(r"注册方式[：:]\s*(.+)", sec)
        if not sm:
            sm = re.search(r"注册[：:]\s*(.+)", sec)
        if sm:
            cand.signup_raw = re.sub(r"\s+", " ", sm.group(1)).strip()[:160]
            method, _ = parse_signup(cand.signup_raw)
            cand.notes.append(f"signup:{method}")

        if cand.checkin_amount is None:
            quota_block = sec
            mq = re.search(r"(免费额度|额度与续领|每日福利|签到)", sec)
            if mq:
                quota_block = sec[mq.start(): mq.start() + 900]
            info = parse_checkin(quota_block)
            if info and info.get("checkin_amount") is not None:
                cand.checkin_raw = cand.checkin_raw or info.get("checkin_raw")
                cand.checkin_amount = info.get("checkin_amount")
                cand.checkin_unit = info.get("checkin_unit")
                cand.checkin_period = info.get("checkin_period")

        rates = parse_rates(sec)
        if rates["rpm"] or rates["rpd"]:
            cand.rpm, cand.rpd, cand.rate_raw = rates["rpm"], rates["rpd"], rates["rate_raw"]

        found = models.find(sec)
        for entry in found:
            if entry.model not in cand.models and len(cand.models) < 12:
                cand.models.append(entry.model)

        if cand.status == "unknown":
            head = sec.split("\n\n")[0]
            if re.search(r"(🔴|🟢|离线|在线|offline|online)", head):
                cand.status = parse_status(head)

    return [c for c in out.values() if c.provider]


# ---------------------------------------------------------------------------
# Parser: bullet-list README
# ---------------------------------------------------------------------------

def parse_bullet_md(body: str, source: str, models: ModelIndex) -> list[Candidate]:
    out: list[Candidate] = []
    for line in body.splitlines():
        if not re.match(r"^\s*[-*+]\s+", line):
            continue
        if "http" not in line:
            continue
        link = LINK_RE.search(line)
        if not link:
            continue
        name = strip_md(link.group(1)).strip()
        url = clean_url(link.group(2))
        desc = strip_md(line[line.find(link.group(0)) + len(link.group(0)):])
        if not name or len(name) > 60 or len(desc) < 12:
            continue
        cand = Candidate(provider=name, site_url=url, source=source, confidence=0.7)
        info = parse_checkin(desc)
        if info:
            cand.checkin_raw = info.get("checkin_raw")
            cand.checkin_amount = info.get("checkin_amount")
            cand.checkin_unit = info.get("checkin_unit")
            cand.checkin_period = info.get("checkin_period")
            if info.get("checkin_ambiguous"):
                cand.notes.append("check-in amount ambiguous")
        rates = parse_rates(desc)
        if rates["rpm"] or rates["rpd"]:
            cand.rpm, cand.rpd, cand.rate_raw = rates["rpm"], rates["rpd"], rates["rate_raw"]
        ep = pick_endpoint(URL_RE.findall(line)[1:]) if len(URL_RE.findall(line)) > 1 else None
        if ep:
            cand.endpoint_url = ep
        method, raw = parse_signup(desc)
        if method != "unknown":
            cand.signup_raw = raw
            cand.notes.append(f"signup:{method}")
        for entry in models.find(desc + " " + name):
            if entry.model not in cand.models:
                cand.models.append(entry.model)
        out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Parser: "free tier" tables (cheahjs style)
# ---------------------------------------------------------------------------

def parse_free_table_md(body: str, source: str, models: ModelIndex) -> list[Candidate]:
    out: list[Candidate] = []
    for header, rows in iter_md_tables(body):
        i_name = col(header, "provider", "model", "name", "service")
        i_free = col(header, "free tier", "free", "limits", "quota")
        i_models = col(header, "models", "model")
        if i_name is None or i_free is None:
            continue
        for row in rows:
            name = cell(row, i_name)
            if not name:
                continue
            free = cell(row, i_free)
            cand = Candidate(provider=name, source=source, confidence=0.7)
            link = LINK_RE.search(row[i_name]) if i_name < len(row) else None
            if link:
                cand.site_url = clean_url(link.group(2))
            info = parse_checkin(free)
            if info:
                cand.checkin_raw = info.get("checkin_raw")
                cand.checkin_amount, cand.checkin_unit = info.get("checkin_amount"), info.get("checkin_unit")
                cand.checkin_period = info.get("checkin_period")
            rates = parse_rates(free)
            cand.rpm, cand.rpd, cand.rate_raw = rates["rpm"], rates["rpd"], rates["rate_raw"]
            for entry in models.find(" ".join(filter(None, [cell(row, i_models), free, name]))):
                if entry.model not in cand.models:
                    cand.models.append(entry.model)
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Parser: card layout HTML (china-ai-arbitrage style)
# ---------------------------------------------------------------------------

def html_to_lines(body: str) -> list[str]:
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|td|section|article)>", "\n", body)
    body = re.sub(r"(?i)</t[dh]>", " | ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    return [l for l in lines if l and len(l) < 400]


CARD_SPLIT = re.compile(r"(?:Visit\s*→|访问|前往|立即领取|立即访问|领取入口|→\s*Visit)", re.I)
NAME_DOMAIN = re.compile(r"^([A-Za-z0-9][\w .+\-&'’/]{1,45}?)\s*\(([^)]{0,60})\)\s*$", re.I)
DOMAIN_IN_PARENS = re.compile(r"([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,10})", re.I)


def parse_cards_html(body: str, source: str, models: ModelIndex) -> list[Candidate]:
    lines = html_to_lines(body)
    out: list[Candidate] = []
    buf: list[str] = []
    cards: list[list[str]] = []
    for line in lines:
        if CARD_SPLIT.search(line):
            if buf:
                cards.append(buf)
            buf = []
        else:
            buf.append(line)
    if buf:
        cards.append(buf)

    for card in cards:
        name, site = None, ""
        idx = None
        for i, line in enumerate(card[:6]):
            m = NAME_DOMAIN.match(line)
            if not m:
                continue
            paren = m.group(2)
            dom = DOMAIN_IN_PARENS.search(paren)
            if not dom:
                continue  # "(official)" alone is a badge, not a domain
            name = m.group(1).strip()
            site = f"https://{dom.group(1).lower()}"
            idx = i
            break
        if not name:
            first = next((i for i, l in enumerate(card[:6]) if 2 < len(l) < 60 and not l.startswith("Visit")), None)
            link = next((LINK_RE.search(l) for l in card if LINK_RE.search(l)), None)
            if link:
                name = strip_md(link.group(1))
                site = clean_url(link.group(2))
                idx = 0
            elif first is not None:
                name = card[first]
                idx = first
        if not name or len(name) > 60:
            continue
        text = " ".join(card[idx + 1:] if idx is not None else card)
        cand = Candidate(provider=name, site_url=site, source=source, confidence=0.7)
        info = parse_checkin(text)
        if info:
            cand.checkin_raw = info.get("checkin_raw")
            cand.checkin_amount, cand.checkin_unit = info.get("checkin_amount"), info.get("checkin_unit")
            cand.checkin_period = info.get("checkin_period")
            if info.get("checkin_ambiguous"):
                cand.notes.append("check-in amount ambiguous")
        rates = parse_rates(text)
        cand.rpm, cand.rpd, cand.rate_raw = rates["rpm"], rates["rpd"], rates["rate_raw"]
        method, raw = parse_signup(text)
        if method != "unknown":
            cand.signup_raw = raw
            cand.notes.append(f"signup:{method}")
        for entry in models.find(text + " " + name):
            if entry.model not in cand.models:
                cand.models.append(entry.model)
        out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Parser: machine-checkable model indexes
# ---------------------------------------------------------------------------

def parse_openrouter_free(body: str) -> tuple[list[str], int]:
    """Return free model ids and total count from the public models JSON."""
    try:
        doc = json.loads(body)
    except ValueError:
        return [], 0
    free: list[str] = []
    data = doc.get("data", [])
    for m in data:
        pricing = m.get("pricing", {}) or {}
        prompt = pricing.get("prompt")
        try:
            is_free = m.get("id", "").endswith(":free") or (prompt is not None and float(prompt) == 0)
        except (TypeError, ValueError):
            is_free = m.get("id", "").endswith(":free")
        if is_free:
            free.append(m.get("id", ""))
    return free, len(data)


def parse_article_cards(body: str, source: str, models: ModelIndex) -> list[Candidate]:
    """Card grids (<article> lists) used by offer trackers."""
    parts = [p for p in re.split(r"(?i)(?=<article\b)", body) if p.lstrip().lower().startswith("<article")]
    out: list[Candidate] = []
    for chunk in parts:
        end = re.search(r"(?i)</article>", chunk)
        card_html = chunk[: end.end()] if end else chunk
        lines = html_to_lines(card_html)
        if len(lines) < 2:
            continue
        name = ""
        for cls in ("name", "title"):
            m = re.search(rf'(?is)<(?:span|h[1-4]|div)[^>]*class="[^"]*{cls}[^"]*"[^>]*>(.*?)</', card_html)
            if m:
                name = strip_md(re.sub(r"(?is)<[^>]+>", " ", m.group(1))).strip()
                if 1 < len(name) <= 60:
                    break
                name = ""
        if not name:
            name = next((l for l in lines if 2 < len(l) < 50 and not re.match(r"(?i)(active|upcoming|new|official|third)", l)), "")
        if not name or len(name) > 60:
            continue
        text = " ".join(lines)
        status = "online" if re.search(r"(?i)\bactive\b|在线", text) else (
            "offline" if re.search(r"(?i)\b(ended|expired|upcoming)\b|已结束", text) else "unknown")
        hrefs = [clean_url(h) for h in re.findall(r'href="(https?://[^"]+)"', card_html)]
        site = next((h for h in hrefs if is_provider_url(h)), "")
        cand = Candidate(provider=name, site_url=site, source=source, confidence=0.6, status=status)
        info = parse_checkin(text)
        if info and info.get("checkin_amount") is not None:
            cand.checkin_raw = info.get("checkin_raw")
            cand.checkin_amount = info.get("checkin_amount")
            cand.checkin_unit = info.get("checkin_unit")
            cand.checkin_period = info.get("checkin_period")
        rates = parse_rates(text)
        cand.rpm, cand.rpd, cand.rate_raw = rates["rpm"], rates["rpd"], rates["rate_raw"]
        method, raw = parse_signup(text)
        if method != "unknown":
            cand.signup_raw = raw
            cand.notes.append(f"signup:{method}")
        for entry in models.find(text + " " + name):
            if entry.model not in cand.models and len(cand.models) < 12:
                cand.models.append(entry.model)
        out.append(cand)
    return out


def parse_model_benchmark(body: str) -> dict:
    """Parse the modelbenchmark.io leaderboard table into the tier format."""
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", body)
    models = []
    for r in rows:
        cells = [re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", c)).strip()
                 for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", r)]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        pct = re.search(r"(\d+)(?:st|nd|rd|th)", cells[3])
        price = cells[6] if len(cells) > 6 else ""
        models.append({
            "rank": int(cells[0]),
            "model": cells[1],
            "lab": cells[2],
            "percentile": int(pct.group(1)) if pct else None,
            "out_price_usd_per_m": _price(price),
        })
    return {"models": models}


def _price(raw: str) -> float | None:
    if not raw or raw in ("—", "-"):
        return None
    if raw.strip().lower() == "free":
        return 0.0
    try:
        return float(raw.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Corroboration sources (aggregators / forum intel)
# ---------------------------------------------------------------------------

def extract_domains(body: str) -> dict[str, int]:
    """Domain -> mention count, for cross-checking a provider's existence."""
    counts: dict[str, int] = {}
    for url in URL_RE.findall(body):
        if not is_provider_url(url):
            continue
        host = re.match(r"https?://([^/:]+)", url, re.I)
        if not host:
            continue
        h = re.sub(r"^www\.", "", host.group(1).lower())
        counts[h] = counts.get(h, 0) + 1
    return counts


def extract_names(body: str) -> set[str]:
    return {_norm(strip_md(m.group(1))) for m in LINK_RE.finditer(body) if len(m.group(1)) < 50}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def parse_document(url: str, body: str, kind: str, role: str, models: ModelIndex) -> dict:
    """Parse one cached document into candidates + side data."""
    result = {"candidates": [], "domains": {}, "names": set(), "tiers": None, "free_models": None, "total_models": 0}
    if not body:
        return result

    if kind == "json" and "openrouter" in url:
        free, total = parse_openrouter_free(body)
        result["free_models"], result["total_models"] = free, total
        return result

    if role == "modelindex" or "modelbenchmark" in url:
        tiers = parse_model_benchmark(body)
        if tiers["models"]:
            result["tiers"] = tiers
        return result

    if role in ("agg", "intel"):
        result["domains"] = extract_domains(body)
        result["names"] = extract_names(body)
        return result

    if kind == "markdown":
        cands = parse_welfare_md(body, url, models)
        cands += parse_bullet_md(body, url, models)
        if not cands:
            cands = parse_free_table_md(body, url, models)
        result["candidates"] = cands
        return result

    # HTML seeds: article grids are richer than prose cards when both exist
    cands = parse_article_cards(body, url, models)
    if len(cands) < 3:
        cands = parse_cards_html(body, url, models)
    result["domains"] = extract_domains(body)
    result["candidates"] = cands
    return result
