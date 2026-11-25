# Handoff

State as of 2026-11-25 (first full CI run).

## Live

- Repo: https://github.com/ons96/free-llm-index (public — unlimited Actions minutes)
- Workflow: `scrape-free-llm-providers`, cron `0 6,18 * * *` + manual dispatch
- First run: [45700991841](https://github.com/ons96/free-llm-index/actions/runs/45700991841) — **25/25 sources healthy, 45 s** end to end, data committed
- Third run: [45702941185](https://github.com/ons96/free-llm-index/actions/runs/45702941185) — cache hit, **~10 s**, Pages deploy green
- Dashboard: https://ons96.github.io/free-llm-index/ (Pages enabled with `build_type=workflow`)

## Verified working

| item | evidence |
|---|---|
| curl_cffi transport | 22 of 25 sources fetched directly, including raw GitHub and Chinese Vue/Next sites |
| Obscura browser tier | `linux.do` topic fetched with `obscura` at 200 in 4.1 s on `ubuntu-latest` (it 403s plain curl) — the tier that could not be tested locally works in CI |
| r.jina.ai reader tier | `grandet.ai` (429 to curl) returned 10 KB of rendered markdown |
| Tier index | live parse of modelbenchmark.io → 91 models, written to `data/tier_index.json` |
| Deterministic extraction | 277 raw candidates → 29 scored, 14 quota-only, 116 unverified, 3 offline, 0 crashes |
| Dedupe | same backend domain merges across sources; vendor endpoints keep relays separate |
| Commit-back | `chore(data): refresh leaderboard [skip ci]` pushed automatically |
| Tests | 29 unit tests pass (`python tests/test_parse.py`) |

## Next action

1. ~~Confirm the Pages deploy step~~ — done: it was racing the artifact upload; a 10 s wait plus
   one retry fixed it and the deploy is green. https://ons96.github.io/free-llm-index/ returns 200.
2. Let the 18:00 UTC cron run once unattended and check `status.json` for flapping sources.
3. Worth doing when there is time: promote `scraped/` to a committed, gzipped snapshot if
   reproducibility matters more than repo size (today it lives in the Actions cache).

## Known gaps (not blockers)

- **Coverage is quota-skewed.** Most scored providers come from one Chinese welfare list;
  first-party free tiers (Groq, OpenRouter, Gemini, Cerebras, NVIDIA) appear but with fewer
  fields because their public pages state limits differently. A dedicated parser per
  first-party vendor page would raise the scored count.
- **Points quotas stay unconverted** (by design). `data/points_ratios.json` exists as an
  opt-in escape hatch; nothing is in it, so no fabricated rates.
- **116 unverified nav entries** are kept for coverage, not ranked. They come from
  navigation repos whose rows carry no quota data.
- **Aggregators contribute only domain corroboration.** Price aggregators list paid
  resellers; scraping them as providers would pollute the ranking.
- **`china-ai-arbitrage.xyz` card layout** is parsed with a heuristic `<article>` splitter.
  It survives today's markup; a redesign there will silently drop that source (it will show
  up as `0 candidates` in `status.json`).

## Unresolved / dead sources

| source | status | note |
|---|---|---|
| `relaywatch.online/models` | dead | HTTP 522, origin unreachable for 20 s; disabled in `sources.txt` |
| `cheahjs/free-llm-api-resources` | dead | repo 404; active mirrors are Llama-3 era |
| `kiro.dev` | dropped | marketing page, no public API or quota data |
| `new.sharedchat.cc/list` | dropped | JS shell with no public quota data |
| `linux.do` | works via reader | 403 to curl and curl_cffi; Obscura or r.jina.ai required |

## Operational notes

- The Cloudflare Worker relay is **not deployed** (no secrets in the repo). The pipeline runs
  without it; adding `RELAY_URL` + `RELAY_SECRET` enables the tier with no code change.
- Rate budgets: r.jina.ai is throttled to ~17 req/min by `scrape.py`; GitHub raw and the
  provider sites are hit once per run with an 18 h cache.
- A committed failure ledger (`scraped_failures/`) makes a dead URL cost one attempt per
  7 days, not one per run. Delete a file there to retry immediately.
