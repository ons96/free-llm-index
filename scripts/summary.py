#!/usr/bin/env python3
"""Write a short markdown summary to $GITHUB_STEP_SUMMARY (no-op locally)."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    data = json.loads((ROOT / "free_llm_providers.json").read_text(encoding="utf-8"))
    counts = data["meta"]["counts"]
    lines = [
        "### Free LLM index",
        "",
        f"- scored: **{counts['ranked']}** · quota-only: {counts['quota_only']} · "
        f"unverified: {counts['unverified']} · offline: {counts['offline']}",
        f"- tier source: `{data['meta']['tier_source']}`",
        "",
        "| # | provider | best free model | check-in/day | score |",
        "|---|---|---|---|---|",
    ]
    for i, p in enumerate(data["providers"][:10], 1):
        usd = p.get("checkin_usd_equiv")
        checkin = f"${usd:.2f}" if usd else ("points" if p.get("checkin_currency") == "points" else "—")
        lines.append(f"| {i} | {p['provider']} | {p['best_free_model'] or '—'} | {checkin} | {p['score']:.1f} |")
    text = "\n".join(lines) + "\n"
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
