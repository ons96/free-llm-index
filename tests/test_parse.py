#!/usr/bin/env python3
"""Unit tests for the parsers and the ranking math.

These run with plain `python3 tests/test_parse.py` (no pytest needed) and are
also collected by pytest in CI. They use real snippets copied from the seed
sources, so a change in source formatting shows up as a red test rather than a
silently empty leaderboard.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import enrich  # noqa: E402
import parse  # noqa: E402


class TestAmounts(unittest.TestCase):
    def test_usd_cell(self):
        info = parse.parse_checkin_cell("✅ $25")
        self.assertEqual(info["checkin_amount"], 25.0)
        self.assertEqual(info["checkin_unit"], "usd")
        self.assertFalse(info["checkin_ambiguous"])

    def test_points_cell(self):
        info = parse.parse_checkin_cell("✅ 2000 积分")
        self.assertEqual(info["checkin_amount"], 2000.0)
        self.assertEqual(info["checkin_unit"], "points")

    def test_disabled_checkin(self):
        info = parse.parse_checkin_cell("❌")
        self.assertEqual(info["checkin_amount"], 0.0)

    def test_ambiguous_cell(self):
        info = parse.parse_checkin_cell("✅ 金额待确认")
        self.assertTrue(info["checkin_ambiguous"])
        self.assertIsNone(info["checkin_amount"])

    def test_checkin_sentence(self):
        info = parse.parse_checkin("注册赠送 $100 + 邀请奖励 $50 + 每日签到 $25")
        self.assertEqual(info["checkin_amount"], 25.0)
        self.assertIn("$25", info["checkin_raw"])

    def test_cny(self):
        info = parse.parse_checkin("每日签到 ¥10")
        self.assertEqual(info["checkin_unit"], "cny")


class TestRates(unittest.TestCase):
    def test_rpm_rpd(self):
        r = parse.parse_rates("free tier: 30 requests per minute and 14,400 requests per day")
        self.assertEqual(r["rpm"], 30)
        self.assertEqual(r["rpd"], 14400)

    def test_chinese_rpm(self):
        self.assertEqual(parse.parse_rates("每分钟 4 次")["rpm"], 4)

    def test_rps_converted(self):
        self.assertEqual(parse.parse_rates("5 RPS")["rpm"], 300)

    def test_none(self):
        self.assertIsNone(parse.parse_rates("no limits published")["rpm"])


class TestSignup(unittest.TestCase):
    def test_github_beats_linuxdo(self):
        # the easiest door is the one users actually walk through
        self.assertEqual(enrich.best_signup("GitHub OAuth / LinuxDO OAuth"), "github")

    def test_email(self):
        self.assertEqual(enrich.best_signup("邮箱 + 密码"), "email")

    def test_card_required(self):
        self.assertEqual(enrich.best_signup("注册需要绑定信用卡"), "card")

    def test_unknown(self):
        self.assertEqual(enrich.best_signup(""), "unknown")


class TestModelIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = parse.ModelIndex.load()

    def test_longest_match_wins(self):
        found = [e.model for e in self.index.find("Claude Opus 5.5 and Claude Sonnet 5")]
        self.assertIn("Claude Opus 5.5", found)
        self.assertNotIn("Claude Opus 5", found)

    def test_best_model(self):
        name, pct = self.index.best(["GPT-6 Astra", "GLM 4.5"])
        self.assertEqual(name, "GPT-6 Astra")
        self.assertGreaterEqual(pct, 90)

    def test_family_fallback(self):
        self.assertEqual(self.index.percentile_for("claude-opus-4-1"), 90)


class TestWelfareTable(unittest.TestCase):
    BODY = """
| 站点 | 状态 | 首日理论上限 | 登记的额度构成 | 每日签到 | 协议 | 模型数 |
|---|---|---|---|---|---|---|
| [AgentRouter](https://agentrouter.org/register?aff=NARV) | 🟢 在线 | $175 | 注册赠送 $100 + 邀请奖励 $50 + 每日签到 $25 | ✅ $25 | anthropic / openai | — |
| [DeadOne](https://dead.example.com/) | 🔴 离线 | — | — | ❌ | — | — |

### 1. AgentRouter

- 注册方式：GitHub OAuth / LinuxDO OAuth

**接口、模型与接入政策**

- **anthropic**：`https://agentrouter.org`
- **openai**：`https://agentrouter.org/v1`

### 2. DeadOne

- 注册方式：邮箱
"""

    def test_parse(self):
        cands = parse.parse_welfare_md(self.BODY, "test", parse.ModelIndex.load())
        by = {c.provider: c for c in cands}
        ar = by["AgentRouter"]
        self.assertEqual(ar.checkin_amount, 25.0)
        self.assertEqual(ar.status, "online")
        self.assertEqual(ar.endpoint_url, "https://agentrouter.org/v1")
        self.assertIn("NARV", "NARV")  # affiliate params are stripped below
        self.assertNotIn("aff", ar.site_url)
        self.assertEqual(by["DeadOne"].status, "offline")

    def test_affiliate_params_stripped(self):
        cands = parse.parse_welfare_md(self.BODY, "test", parse.ModelIndex.load())
        ar = next(c for c in cands if c.provider == "AgentRouter")
        self.assertNotIn("aff=", ar.site_url)


class TestDedupe(unittest.TestCase):
    def test_same_backend_merges(self):
        a, ha = enrich.dedupe_key("https://api.pool-one.top/v1", "https://relay-a.com")
        b, hb = enrich.dedupe_key("https://api.pool-one.top/api/v1", "https://relay-b.net")
        self.assertEqual(a, b)
        self.assertEqual(ha, hb)

    def test_vendor_backend_keeps_relays_apart(self):
        a, _ = enrich.dedupe_key("https://api.openai.com/v1", "https://relay-a.com")
        b, _ = enrich.dedupe_key("https://api.openai.com/v1", "https://relay-b.net")
        self.assertNotEqual(a, b)

    def test_no_endpoint_uses_site(self):
        key, _ = enrich.dedupe_key(None, "https://www.example.com/register")
        self.assertEqual(key, "dom:example.com")


class TestScoring(unittest.TestCase):
    def test_checkin_saturates(self):
        self.assertAlmostEqual(enrich.checkin_factor(5), 1.0, places=3)
        self.assertAlmostEqual(enrich.checkin_factor(500), 1.0, places=3)

    def test_zero_checkin_is_not_fatal(self):
        self.assertEqual(enrich.checkin_factor(0), 0.0)

    def test_rate_scale(self):
        self.assertEqual(enrich.rate_factor(2, None), 0.40)
        self.assertEqual(enrich.rate_factor(20, None), 1.0)
        self.assertLess(enrich.rate_factor(8, None), 1.0)

    def test_one_time_grant_is_not_daily(self):
        usd, conf = enrich.checkin_usd(100.0, "usd", "x.com", "注册赠送 $100")
        self.assertIsNone(usd)
        self.assertEqual(conf, "one-time")

    def test_points_never_invented(self):
        usd, conf = enrich.checkin_usd(100.0, "points", "x.com", "每日签到 100 积分")
        self.assertIsNone(usd)
        self.assertEqual(conf, "points")

    def test_score_ordering(self):
        strong = 90 * (0.55 + 0.45 * enrich.checkin_factor(5)) * 1.0 * 1.0
        weak = 60 * (0.55 + 0.45 * 0.0) * 0.4 * 0.75
        self.assertGreater(strong, weak)


class TestOpenRouter(unittest.TestCase):
    def test_free_filter(self):
        body = ('{"data":[{"id":"qwen/qwen3:free","pricing":{"prompt":"0"}},'
                '{"id":"openai/gpt-5","pricing":{"prompt":"0.000001"}}]}')
        free, total = parse.parse_openrouter_free(body)
        self.assertEqual(free, ["qwen/qwen3:free"])
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
