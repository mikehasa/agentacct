#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentacct.deep_provider_validation import SUPPORTED_DEEP_PROVIDERS, run_deep_provider_validation
from agentacct.provider_smoke import ProviderSmokeError


def main() -> int:
    parser = argparse.ArgumentParser(description="Run controlled real-money deep provider validation.")
    parser.add_argument("--providers", nargs="+", choices=SUPPORTED_DEEP_PROVIDERS, required=True)
    parser.add_argument("--max-provider-usd", required=True, type=float, help="Local estimated per-provider preflight cap. Must be <= 1.0.")
    parser.add_argument("--store-dir", type=Path, default=None)
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="Optional delay between cases to avoid provider rate limits.")
    parser.add_argument(
        "--i-understand-this-spends-real-money",
        action="store_true",
        help="Required acknowledgement. This runs real provider calls.",
    )
    args = parser.parse_args()

    if not args.i_understand_this_spends_real_money:
        print("Refusing to run without --i-understand-this-spends-real-money", file=sys.stderr)
        return 2

    try:
        result = run_deep_provider_validation(
            list(args.providers),
            max_provider_usd=args.max_provider_usd,
            store_dir=args.store_dir,
            delay_seconds=args.delay_seconds,
        )
    except ProviderSmokeError as exc:
        print(f"deep provider validation failed before/around requests: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("overall_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
