#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentacct.provider_smoke import ProviderSmokeError, run_provider_smoke


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one tiny real-money provider smoke test with a local preflight budget cap.")
    parser.add_argument("--provider", required=True, choices=["openai", "anthropic", "gemini", "deepseek"])
    parser.add_argument("--max-usd", required=True, type=float, help="Local estimated preflight budget cap for this one smoke request. Must be <= 0.05.")
    parser.add_argument("--store-dir", type=Path, default=None, help="Optional Agent Chronicle store dir for the smoke ledger.")
    parser.add_argument("--model", default=None, help="Optional model override for the selected provider.")
    parser.add_argument(
        "--i-understand-this-spends-real-money",
        action="store_true",
        help="Required safety acknowledgement. The request is tiny, but it is still a real provider call.",
    )
    args = parser.parse_args()

    if not args.i_understand_this_spends_real_money:
        print("Refusing to run without --i-understand-this-spends-real-money", file=sys.stderr)
        return 2

    try:
        summary = run_provider_smoke(
            args.provider,
            max_usd=args.max_usd,
            store_dir=args.store_dir,
            model=args.model,
        )
    except ProviderSmokeError as exc:
        print(f"provider smoke failed before/around request: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("decision") == "forwarded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
