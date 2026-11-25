# free-llm-index

A fully free, scheduled pipeline that builds a ranked, deduplicated index of
**free LLM API providers** — relays and first-party free tiers — from public
welfare lists, provider pages and a model-quality leaderboard.

Everything runs on GitHub Actions. No servers, no paid proxies, no paid CAPTCHA
solvers, no API keys, no logins.

- **Leaderboard:** [`LEADERBOARD.md`](LEADERBOARD.md)
- **Full data:** [`free_llm_providers.csv`](free_llm_providers.csv) · [`free_llm_providers.json`](free_llm_providers.json)
- **Live dashboard:** `dist/index.html`, published by the workflow to GitHub Pages when Pages is enabled
- **Run stats:** [`status.json`](status.json)

## What it collects

For every provider it can verify: API base URL, models on the free tier, the
best free model's benchmark percentile, daily check-in wording and normalized
USD-equivalent, rate limits, signup method, online status, and which public
sources back the record.

Two things are deliberately *not* done:

1. **No invented numbers.** Points-only quotas ("1000 积分") are kept as points;
   we cannot convert them without a published rate. Ambiguous wording
   ("随机 1–999 额度") is flagged, not guessed.
2. **No affiliate links.** Every `?aff=`, `?ref=` and `utm_*` parameter is
   stripped before a URL is stored.

## How it works

```
sources.txt
   │
   ▼
scripts/scrape.py   tiered fetch, cache-first, robots-aware
   │  curl_cffi (browser TLS) → Cloudflare Worker relay → Obscura (real browser)
   │  → r.jina.ai reader → scrapling StealthyFetcher (optional)
   ▼
scripts/parse.py    deterministic extraction (regex + table/section parsers)
   ▼
scripts/enrich.py   normalize → dedupe by backend host → score → publish
   │
   ├── free_llm_providers.csv / .json
   ├── LEADERBOARD.md
   ├── status.json
   └── dist/index.html
```

The workflow runs at **06:00 and 18:00 UTC** (`.github/workflows/scrape.yml`),
takes roughly 10 minutes, and commits the refreshed outputs back to the branch
with `[skip ci]`.

### Ranking

```
score = tier × (0.55 + 0.45·checkin) × rate × signup × ambiguity
```

| term | meaning |
|---|---|
| `tier` | percentile (0–100) of the provider's best free model on [modelbenchmark.io](https://modelbenchmark.io/leaderboard), refreshed every run; falls back to a baked `data/model_tiers.json` |
| `checkin` | `log10(1+USD)/log10(6)` — $5/day saturates, $0 does not nullify the score |
| `rate` | 0.40 below 4 RPM → 1.00 at 20+ RPM (log scale); no published limit = 0.72 |
| `signup` | GitHub 1.0 · Google/OAuth 0.95 · email 0.9 · invite 0.8 · phone 0.7 · linux.do-only 0.6 · card 0.6 |

Providers whose best free model scores under **MiniMax M3.1 Flash** (≈ p50) are
kept but listed separately, as are providers that publish a quota but never
name a model we can score.

## Adding a source

Append one line to [`sources.txt`](sources.txt):

```
https://example.com/free-tiers   # tier:2 kind:html role:seed
```

| attribute | values | meaning |
|---|---|---|
| `tier` | 1–4 | fetch order; 1 = cheap static, 4 = browser/reader first |
| `kind` | `markdown` `html` `json` | parser family |
| `role` | `seed` `agg` `intel` `modelindex` | `seed` creates providers, the others only corroborate or index |
| `enabled` | `false` | keep the line, skip it |

Optional additions:

- `data/points_ratios.json` — `{"host-substring": {"points_per_usd": 100}}` lets a
  points quota be converted *when a provider publishes its rate*.
- Repository secrets `RELAY_URL` / `RELAY_SECRET` enable the optional
  [Cloudflare Worker relay](worker.js) for datacenter-blocked sites.

## Running locally

```bash
python3 -m pip install -r requirements.txt
python3 scripts/scrape.py --max-age 6h      # or --force, --only <substring>
python3 scripts/enrich.py
python3 tests/test_parse.py
open dist/index.html
```

Scraped bodies land in `scraped/<sha256>.json` (git-ignored, restored from the
Actions cache), failures in `scraped_failures/` where a URL is skipped for
7 days after 6 failed attempts.

## Limits and honesty

- Quotas and model lists change without notice; a free relay can vanish in a
  day. `status.json` records which sources were healthy on the last run.
- Sources that block datacenter IPs sometimes cannot be fetched even with a
  browser; those are logged, not faked.
- Everything here is public information about public services. No scraping
  behind logins, robots.txt is honoured where it is reachable, and request
  rates are throttled.
- This index is not an endorsement. Do not send production keys, private code
  or personal data to an unvetted relay.

MIT licensed.
