"""Benchmark the per-task overhead of the server's run_in_translator_thread.

Every server translation task runs through ``run_in_translator_thread``. The
model usage gate held there (shared while a task runs, exclusive while models
are unloaded) must not add measurable latency. The harness times a no-op task
sequentially and with concurrent callers and reports latency percentiles.

    python test/bench_model_gate.py --repo /path/to/old/checkout
    python test/bench_model_gate.py

Not collected by pytest; run it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path


def percentile(samples, q):
    """Linear-interpolated percentile (numpy's default method)."""
    ordered = sorted(samples)
    if not ordered:
        return math.nan
    rank = (len(ordered) - 1) * q / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                        help="checkout whose code is measured (default: this one)")
    parser.add_argument("--calls", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=3, help="concurrent callers (= max_concurrent_tasks)")
    parser.add_argument("--json", help="write samples to this file")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.repo).resolve()))
    from manga_translator.server.core import task_manager

    task_manager.server_config["max_concurrent_tasks"] = args.concurrency
    task_manager.init_semaphore()

    def task():
        return None

    async def timed(samples):
        start = time.perf_counter()
        await task_manager.run_in_translator_thread(task)
        samples.append(time.perf_counter() - start)

    async def run():
        warmup = []
        for _ in range(200):
            await timed(warmup)
        sequential = []
        for _ in range(args.calls):
            await timed(sequential)
        concurrent = []

        async def caller(count):
            for _ in range(count):
                await timed(concurrent)

        await asyncio.gather(*(caller(args.calls // args.concurrency) for _ in range(args.concurrency)))
        return sequential, concurrent

    sequential, concurrent = asyncio.run(run())
    report = {"repo": str(Path(args.repo).resolve()), "has_gate": hasattr(task_manager, "_model_gate")}
    print(f"repo={report['repo']}  model_gate={report['has_gate']}  calls={args.calls}")
    print(f"{'mode':<22}{'p50 us':>9}{'p90 us':>9}{'p99 us':>9}{'mean us':>9}")
    for name, samples in (("sequential", sequential), (f"{args.concurrency} concurrent callers", concurrent)):
        micro = [s * 1e6 for s in samples]
        stats = {q: percentile(micro, q) for q in (50, 90, 99)}
        stats["mean"] = sum(micro) / len(micro)
        report[name] = {"summary_us": stats, "samples_s": samples}
        print(f"{name:<22}{stats[50]:>9.1f}{stats[90]:>9.1f}{stats[99]:>9.1f}{stats['mean']:>9.1f}")
    task_manager.shutdown_executor()
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
