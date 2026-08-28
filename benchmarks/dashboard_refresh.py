#!/usr/bin/env python3
"""Reproducible large-ledger benchmark for the native app refresh fan-out."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agentacct.api import create_local_api_app
from agentacct.event_log import RawEventLog, serialize_event

ROUTES = (
    "/v1/glance",
    "/v1/tasks?limit=50",
    "/v1/plan?days=7",
    "/usage/summary?days=7",
)


def _seed(store: Path, event_count: int) -> None:
    store.mkdir(parents=True)
    log = RawEventLog(store / "events.sqlite3")
    log.replace_all(
        serialize_event(
            {
                "event_id": f"evt_benchmark_{index}",
                "event_type": "note",
                "created_at": float(index),
                "metadata": {"client": "benchmark"},
            }
        )
        for index in range(event_count)
    )
    (store / "events.authoritative").write_text("1\n", encoding="utf-8")


def _refresh(client: TestClient, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    downloaded = 0
    for route in ROUTES:
        response = client.get(route, headers=headers if route.startswith("/v1/") else None)
        response.raise_for_status()
        downloaded += len(response.content)
    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=50_000)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.events < 1 or args.rounds < 1:
        parser.error("--events and --rounds must be positive")

    os.environ["AGENTACCT_EVIDENCE_V2"] = "0"
    token = "benchmark-token"
    with tempfile.TemporaryDirectory(prefix="agentacct-dashboard-benchmark-") as directory:
        store = Path(directory) / "store"
        _seed(store, args.events)
        app = create_local_api_app(
            store_dir=store,
            v1_auth_token=token,
            extra_allowed_hosts=("testserver",),
        )
        with TestClient(app) as client:
            started = time.perf_counter()
            downloaded = _refresh(client, token)
            cold = time.perf_counter() - started

            warm_samples = []
            for _ in range(args.rounds):
                started = time.perf_counter()
                _refresh(client, token)
                warm_samples.append(time.perf_counter() - started)

    print(
        json.dumps(
            {
                "schema": "agentacct.dashboard-refresh-benchmark.v1",
                "events": args.events,
                "routes": list(ROUTES),
                "downloaded_bytes": downloaded,
                "cold_seconds": round(cold, 6),
                "warm_seconds": [round(value, 6) for value in warm_samples],
                "warm_median_seconds": round(statistics.median(warm_samples), 6),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
