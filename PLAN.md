# PLAN — Free LLM provider scraper (GitHub Actions only)

Status: Phase 0 complete, built 2026-11-25.

## 1. Feasibility smoke test (real results, run from the build machine)

| # | Test | Command / tool | Result |
|---|------|----------------|--------|
| 1 | Priority-1 GitHub raw README (`wynx1123/ai-welfare-hub`) | `curl` | **200**, 139 KB, 0.49 s — parseable markdown tables + per-site policy sections |
| 2 | Priority-1 GitHub raw README (`oniondas/Awesome-Hidden-AI-Credits-Free`) | `curl` | **200**, 7.3 KB — bullet list with `[Name](link)` + prose limits |
| 3 | Priority-1 site `china-ai-arbitrage.xyz/free-tokens` | `curl` | **200**, 200 KB server-rendered HTML |
| 4 | Priority-1 site `relaywatch.online/models` | `curl`, `curl_cffi`, `r.jina.ai` | **522** (Cloudflare cannot reach origin), reader timed out → **dead, excluded** |
| 5 | JS-heavy source `linux.do/t/topic/3105499/19` | `curl` → 403; `curl_cffi` impersonate → 403; `r.jina.ai` → **200**, 13.6 KB rendered markdown | Works through the free reader tier |
| 6 | Cloudflare-protected (`grandet.ai`) | `curl` → 429 | Needs stealth tier; retried with backoff, excluded on repeated 429 |
| 7 | Obscura v0.2.7 binary | downloaded `obscura-aarch64-linux-stealth.tar.gz` (73 MB) | **Cannot exec locally**: build machine is Alpine/musl, releases target Ubuntu 22.04 glibc (`ld-linux-aarch64.so.1` missing, `gcompat` lacks `__res_init`/`mallopt`). Verified by reading the v0.2.7 release API + CLI docs; validated for real on the first GitHub Actions run (ubuntu-latest, glibc 2.39, x86_64 asset). |
| 8 | `scrapling` + `StealthyFetcher` | `pip install scrapling` | Installs (0.5.1) but `import` fails: hard dependency on `patchright`, and this mirror only serves a 0.0.1 placeholder. On CI it also pulls ~150 MB Chromium per run. **Demoted to optional tier** (guarded import). |
| 9 | Tier-score source `modelbenchmark.io/leaderboard` | `curl` + regex | **200**; clean `<table>` of 91 ranked models with percentile + price → used as the live tier index |

Nothing in the plan is blocked: every source that failed has a free fallback, and the two that are genuinely down (relaywatch, grandet when rate-limited) are marked exhausted instead of retried.

## 2. Confirmed tool stack (with swaps)

| Layer | Prompt recommendation | Confirmed | Why |
|---|---|---|---|
| Orchestration | GitHub Actions cron | **GitHub Actions**, `ubuntu-latest`, 1 job | fits in minutes, free on public repos |
| Static fetch | `curl` + `scrapling.Fetcher` | **`curl_cffi`** (`impersonate="chrome"`) + stdlib `urllib` fallback | browser TLS fingerprint clears several bot walls that plain curl did not; zero browser download |
| JS rendering | `Obscura` via CDP / `npx obscura-cli` | **Obscura CLI** `obscura fetch --dump html`, `obscura scrape --concurrency` | the npm package `obscura-cli` **does not exist** (404); the real product is a Rust binary with a batch subcommand — no Node, no Playwright |
| Anti-bot | `scrapling.StealthyFetcher` | **optional last tier** (only if importable), plus **`r.jina.ai`** reader tier | scrapling needs patchright + Chromium; the reader is free, keyless and already proved it clears Cloudflare |
| Reader / extractor | `r.jina.ai/URL` | **`r.jina.ai`** with rate-limit backoff (20 RPM) | verified working on the blocked linux.do topic |
| Egress relay | Cloudflare Worker + secret | **Cloudflare Worker** (`worker.js`), used only on 403/429/503 and only when secrets exist | free 100k req/day; pipeline degrades gracefully when it is not configured |
| Dedupe/enrichment | regex + free LLM for ambiguity | **deterministic only, no LLM** | keeps the pipeline key-free and reproducible; ambiguous strings are flagged `needs_review` instead of guessed |
| Tier scores | Artificial Analysis / BenchLM / SWE-bench APIs | **modelbenchmark.io leaderboard** (live) + baked `data/model_tiers.json` fallback | all three named sources are keyed/paid or JS-gated; this one is free HTML with a 91-model composite |
| Publish | CSV/MD + optional dashboard | **CSV + JSON + LEADERBOARD.md + static `dist/index.html`** | GitHub Pages-ready, no server |

Every swap is mirrored in `decisions.md`.

## 3. Workflow structure

One job (`scrape`), `timeout-minutes: 90`, expected **1–3 min** (measured 45 s on the first real run):

1. `actions/checkout` (with `contents: write` so it can commit back)
2. `actions/setup-python@v5` 3.12 + `actions/cache` on `scraped/` and the Obscura binary
3. `scripts/fetch_obscura.sh` — download the pinned release asset (cached)
4. `pip install -r requirements.txt`
5. `python scripts/scrape.py --max-age 6h` — tiered fetch, cache-first, per-source failure ledger
6. `python scripts/enrich.py` — extract → normalize → dedupe → rank → write outputs
7. `python scripts/build_dashboard.py` — `dist/index.html`
8. commit `scraped/`, `scraped_failures/`, `data/`, `dist/`, outputs; push (skips on no-op)
9. `upload-pages-artifact` + `deploy-pages` (optional, non-fatal)

Cron: `0 6,18 * * *` (twice daily) + `workflow_dispatch` with `force` / `only` inputs. Runtime is far below the 6-hour job cap, so no job splitting is needed — the split-by-priority logic exists inside the scraper (`priority` in `sources.txt`) instead.

## 4. Data schema

`free_llm_providers.csv` (stable column order):

```
id, provider, endpoint_url, backend_host, dedupe_key, models_free, best_free_model,
tier_score, tier_source, checkin_raw, checkin_daily_value, checkin_currency,
checkin_usd_equiv, checkin_confidence, rate_limit_raw, rpm, rpd, signup_method,
signup_score, status, sources, first_seen, last_seen, notes
```

`free_llm_providers.json` — same records plus `meta` (generated_at, source count, coverage, prompt version). `status.json` — run stats, per-source health, cache hits.

## 5. Ranking formula

```
score = tier_score
      * (0.55 + 0.45 * checkin_factor)      # check-in can never zero a provider out
      * rate_factor                          # RPM: <4 → 0.40 … ≥20 → 1.00 (log-scaled between)
      * signup_score                         # GitHub OAuth 1.0, email 0.9, invite 0.8, phone 0.7, linux.do-only 0.6, card 0.6
      * 0.9 if flagged ambiguous check-in
```

- `tier_score` = percentile (0–100) of the provider's **best free model**, from the live modelbenchmark table, else the baked catalog, else `unscored` (ranked last, not dropped).
- `checkin_factor` = `min(1, log10(1 + usd_equiv) / log10(1 + 5))` so $0 → 0, $1 → 0.39, $5+ → 1.0 — diminishing returns instead of a linear blow-up.
- Currency handling: USD verbatim; CNY converted with a **pinned** rate constant (`CNY_USD_RATE`, documented, monthly-refreshed); points/积分/额度 kept separate and converted only when the source states a ratio; "随机/待确认" → `checkin_confidence=low` and no USD guess.
- Baseline: **MiniMax M3.1 Flash ≈ 50th percentile** (modelbenchmark lists MiniMax M3 at 47th, M3.1 Flash is its direct successor). Below-baseline providers are kept but flagged `below_baseline` and hidden from the main table unless they clear the "exceptional speed" exception (≥60 RPM advertised).

Deviation from the prompt (logged): the prompt's pure product `tier × checkin × rate × signup` collapses to 0 for the majority of free relays that have **no** daily check-in, which would make the leaderboard meaningless. The check-in term is therefore compressed into a bounded multiplier.

## 6. Seed sources

Priority-1/2 GitHub READMEs and P1 sites are the workhorses; P3 aggregators are cross-check/sanity only (they list *prices*, not free quotas); P4 is community intel.

Added beyond the prompt (evidence-based, all free):
- `https://modelbenchmark.io/leaderboard` — tier index (moved from "P4 intel" to a first-class enrichment input)
- `https://api.github.com/repos/.../commits?path=README.md&per_page=1` — freshness stamp per welfare repo
- `https://openrouter.ai/api/v1/models` — public JSON of free (`:free` suffix, price 0) models, no key needed: a hard, machine-checkable free-model source
- `https://raw.githubusercontent.com/cheahjs/free-llm-api-resources/main/README.md` — maintained free-tier list (widely mirrored)

Excluded after testing: `relaywatch.online` (522), `new.sharedchat.cc/list` (JS shell, no public quota data), `kiro.dev` (product marketing page, no API quota), `linux.do` direct (403 → only through reader).

`sources.txt` carries `# tier:`, `# priority:`, `# kind: markdown|html|json` and `# enabled: false` markers so new sources need no code change.

## 7. Risks / open questions

1. **Obscura on CI** — highest-risk item; mitigated by the 4-tier fallback and by the run failing soft (a source that cannot be fetched is recorded, not fatal).
2. **Affiliate links** — every welfare README is full of `?aff=`/`?ref=` referral URLs. We strip tracking params before storing endpoints, so the DB is not an affiliate funnel (and we never auto-click a register link).
3. **Points vs USD** — many Chinese relays quote 积分 with no published rate. We refuse to invent a conversion; those stay `points` with `checkin_usd_equiv = null`.
4. **Source drift** — README table formats change. The extractor is defensive (section-based + table-based + generic URL sweep) and records `unparsed_sources` in `status.json`.
5. **Rate limits** — `r.jina.ai` 20 RPM and GitHub raw limits are respected with a global token-bucket and exponential backoff.
6. **No login-walled data** — anything requiring an account is skipped by design.
