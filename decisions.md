# Decisions log

Every deviation from the recommended architecture, with the observation that
caused it. Newest last.

| step | original recommendation | replacement | reason | date |
|---|---|---|---|---|
| Static fetch | `curl` + `scrapling.Fetcher` | `curl_cffi` with `impersonate="chrome"`, `urllib` as fallback | browser TLS fingerprint clears bot walls plain curl does not; zero browser download; verified 200 on 20+ sources | 2026-11-25 |
| Browser engine | `npx obscura-cli` / "Obscura via CDP" | Obscura **CLI** binary (`obscura fetch --dump html`, `obscura scrape`) | the npm package `obscura-cli` returns 404; the real product is a Rust binary with a batch subcommand, no Node or Playwright needed | 2026-11-25 |
| Obscura install | "download binary from releases" | `scripts/fetch_obscura.sh`, pinned `v0.2.7`, `-stealth` asset, cached by Actions | reproducible version, and the stealth build is the one that clears challenges | 2026-11-25 |
| Anti-bot last resort | `scrapling.StealthyFetcher` as fallback | **optional** guarded import, not installed by default; `r.jina.ai` reader promoted ahead of it | scrapling 0.5.x hard-requires `patchright` (a ~150 MB Chromium download per CI run) and its mirror here only served a 0.0.1 placeholder; the free reader already cleared the Cloudflare-protected source in testing | 2026-11-25 |
| Reader tier | `r.jina.ai/URL` "for static pages" | `r.jina.ai` as a **rendering + egress** tier, throttled to ~17 req/min | it returned fully rendered markdown for `linux.do` which 403'd both curl and curl_cffi | 2026-11-25 |
| Tier scores | Artificial Analysis / BenchLM / SWE-bench APIs | **modelbenchmark.io** leaderboard table (live) + baked `data/model_tiers.json` | the named sources are keyed/paid or JS-gated; this one is free HTML with 91 models and a composite percentile | 2026-11-25 |
| Ambiguity handling | LLM call to a free model for unclear text | **no LLM at all**; regex + table/section parsers, ambiguous fields flagged `needs_review` | keeps the pipeline key-free, deterministic and reproducible; an LLM key would be the only secret and the only cost | 2026-11-25 |
| Ranking formula | `tier × checkin × rate × signup` | `tier × (0.55 + 0.45·checkin) × rate × signup` | the pure product collapses to 0 for every provider without a daily check-in (most of them), which makes the leaderboard meaningless | 2026-11-25 |
| Check-in scale | linear USD | `log10(1+usd)/log10(6)`, saturating at ~$5/day | one outlier quoting "$200/day" would otherwise dominate everything forever | 2026-11-25 |
| Points conversion | "convert to USD-equivalent" | points stay points unless `data/points_ratios.json` has a published rate | most Chinese relays never publish 积分→USD; guessing would fabricate the headline number | 2026-11-25 |
| One-time grants | counted as check-in | detected (`注册/首日/welcome/one-time`) and excluded from the daily value | "$200 signup bonus" is not a daily check-in and was inflating scores | 2026-11-25 |
| Dedupe key | "backend host" | registrable domain of the backend, unless the backend is the model vendor itself (then the relay's own domain) | two relays both pointing at `api.openai.com` are different providers with different quotas; two domains pointing at the same `api.pool.top` are the same pool | 2026-11-25 |
| Signup scoring | first match wins | highest-scoring available door wins | "GitHub OAuth / LinuxDO OAuth" was scored 0.6 (linux.do) instead of 1.0 (GitHub) | 2026-11-25 |
| Cache persistence | commit `scraped/` to the repo | Actions cache (`scraped/`, `bin/`), git-ignored | committing ~3 MB of HTML twice a day grows the repo by ~1 GB/year for no benefit; failures ledger is still committed | 2026-11-25 |
| Dead source | `cheahjs/free-llm-api-resources` | dropped | repository returns 404 (deleted/renamed); the only active mirrors are Llama-3-era and useless for 2026 model tiers | 2026-11-25 |
| Dead source | `relaywatch.online/models` | dropped, `enabled:false` in `sources.txt` | HTTP 522 from Cloudflare and a reader timeout: origin is down, not blocking us | 2026-11-25 |
| Dead source | `kiro.dev`, `new.sharedchat.cc/list` | dropped | marketing page / JS shell with no public quota data; keeping them only burns CI minutes | 2026-11-25 |
| Aggregator handling | scrape as providers | only contribute domain corroboration, plus an `unverified` bucket | price aggregators list paid resellers and prices, not free quotas; mixing them in would pollute the ranking | 2026-11-25 |
| Job layout | split across jobs if >6 h | single job, 90 min timeout | measured full crawl is 10–20 min, far under the 6-hour cap; splitting would only add checkout overhead | 2026-11-25 |
| Cloudflare Worker | required secret | optional tier, skipped when secrets are absent | a public repo with no secrets should still work end to end | 2026-11-25 |
