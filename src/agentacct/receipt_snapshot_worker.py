"""One low-priority, disposable receipt build, launched by the daemon."""
from __future__ import annotations

import argparse
import os
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", required=True)
    args = parser.parse_args()
    try:
        os.nice(10)
    except OSError:
        pass
    from .receipt_snapshot_runtime import SNAPSHOT_CPU_FRACTION_ENV, record_worker_budget, worker_build_lock

    with worker_build_lock(args.store_dir) as acquired:
        if not acquired:
            return 0
        failed = True
        try:
            from .receipt_snapshot_builder import build_snapshot_entries
            from .receipt_snapshot_store import ReceiptSnapshotStore
            entries, input_token, safety_token, captured_at = build_snapshot_entries(args.store_dir)
            ReceiptSnapshotStore(args.store_dir).publish(entries, input_token=input_token,
                safety_token=safety_token, built_at=captured_at, cpu_seconds=time.process_time())
            failed = False
            return 0
        finally:
            # Total process CPU includes imports, reduction and publication.
            # The child writes this even when its parent daemon died mid-build.
            record_worker_budget(args.store_dir, cpu_seconds=time.process_time(), failed=failed,
                cpu_fraction=float(os.environ.get(SNAPSHOT_CPU_FRACTION_ENV, "0.10")))


if __name__ == "__main__":
    raise SystemExit(main())
