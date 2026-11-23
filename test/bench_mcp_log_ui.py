"""Benchmark the GUI-thread cost of MCP live-log forwarding.

McpService receives every log record in the process. A background thread feeds
records into it the way the logging listener thread does, while the GUI thread
runs a 10 ms heartbeat timer. For each scenario the harness reports:

* GUI CPU: CPU time the GUI thread spent while the records were handled;
* catch-up: time until the log view shows the last record (or, when nothing
  needs showing, until feeding ends);
* heartbeat lag p50/p90/p99/max: how late the GUI thread serviced the 10 ms
  timer, i.e. how long the UI was unresponsive.

Scenarios cover the MCP page visible and hidden (another page is open, the
usual case), with a burst of records and with a steady logging rate.

    python test/bench_mcp_log_ui.py --repo /path/to/old/checkout --json before.json
    python test/bench_mcp_log_ui.py --json after.json

Not collected by pytest; run it directly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

HEARTBEAT_MS = 10


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
    parser.add_argument("--repeats", type=int, default=3, help="runs per scenario")
    parser.add_argument("--burst", type=int, default=20000, help="records fed as fast as possible")
    parser.add_argument("--steady-rate", type=int, default=2000, help="records per second for the steady scenario")
    parser.add_argument("--steady-seconds", type=float, default=5.0)
    parser.add_argument("--json", help="write per-run results to this file")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    for path in (repo / "desktop_qt_ui", repo):
        sys.path.insert(0, str(path))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:  # Same import order as the app and tests (torch before PyQt6).
        import torch  # noqa: F401
    except ImportError:
        pass

    from PyQt6.QtCore import QEventLoop, Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from core.config_models import AppSettings
    from services.log_service import unsubscribe_log_records
    from services.mcp_service import McpService
    from ui.main_page.pages import mcp_page

    workdir = tempfile.TemporaryDirectory()
    service = McpService(root_dir=workdir.name)
    unsubscribe_log_records(service._on_log_entry)  # only the harness feeds records
    mcp_page.get_mcp_service = lambda: service
    mcp_page.get_config_service = lambda: SimpleNamespace(get_config=AppSettings)
    mcp_page.get_log_service = lambda: SimpleNamespace(get_recent_logs=lambda limit=1000: [])
    batching = hasattr(service, "drain_log_records")

    def settle(seconds):
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    def run_once(visible, records, rate):
        panel = mcp_page.McpPagePanel(
            controller=SimpleNamespace(update_single_config=lambda *a, **k: None, open_output_folder=lambda *a: None),
            t_func=lambda key, **_kwargs: key,
        )
        shown = {"text": ""}
        original_append = panel.log_view.appendPlainText

        def counting_append(text):
            shown["text"] = text
            original_append(text)

        panel.log_view.appendPlainText = counting_append
        if visible:
            panel.resize(1000, 800)
            panel.show()
        settle(0.3)
        if batching:
            service.drain_log_records()

        last_message = f"line {records - 1}"
        must_show = visible or not batching  # the old page rendered even while hidden
        feeding_done = threading.Event()

        def feed():
            start = time.perf_counter()
            for index in range(records):
                service._on_log_entry({
                    "timestamp": "2026-11-23T10:00:00", "level": "INFO",
                    "module": "manga_translator", "message": f"line {index}",
                })
                if rate:
                    delay = start + (index + 1) / rate - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
            feeding_done.set()

        beats = []
        result = {}
        loop = QEventLoop()

        def heartbeat():
            beats.append(time.perf_counter())

        def check_done():
            caught_up = (not must_show) or shown["text"].endswith(last_message)
            timed_out = time.perf_counter() - wall_start > 120
            if (feeding_done.is_set() and caught_up) or timed_out:
                result["gui_cpu_s"] = time.thread_time() - cpu_start
                result["catch_up_s"] = time.perf_counter() - wall_start
                result["timed_out"] = timed_out
                loop.quit()

        beat_timer = QTimer()
        beat_timer.setTimerType(Qt.TimerType.PreciseTimer)
        beat_timer.setInterval(HEARTBEAT_MS)
        beat_timer.timeout.connect(heartbeat)
        done_timer = QTimer()
        done_timer.setInterval(5)
        done_timer.timeout.connect(check_done)

        feeder = threading.Thread(target=feed, daemon=True)
        cpu_start = time.thread_time()
        wall_start = time.perf_counter()
        beats.append(wall_start)
        beat_timer.start()
        done_timer.start()
        feeder.start()
        loop.exec()
        beat_timer.stop()
        done_timer.stop()
        feeder.join(timeout=5)
        beats.append(time.perf_counter())

        lags_ms = [max(0.0, (b - a) * 1000 - HEARTBEAT_MS) for a, b in zip(beats, beats[1:])]
        panel.shutdown()
        panel.close()
        panel.deleteLater()
        settle(0.2)
        result["lags_ms"] = lags_ms
        return result

    scenarios = [
        ("visible, burst", True, args.burst, 0),
        ("hidden, burst", False, args.burst, 0),
        ("visible, steady", True, int(args.steady_rate * args.steady_seconds), args.steady_rate),
        ("hidden, steady", False, int(args.steady_rate * args.steady_seconds), args.steady_rate),
    ]
    report = {"repo": str(repo), "batching": batching, "heartbeat_ms": HEARTBEAT_MS, "scenarios": {}}
    print(f"repo={repo}  batching={batching}  repeats={args.repeats}  heartbeat={HEARTBEAT_MS}ms")
    print(f"{'scenario':<18}{'records':>8}{'GUI CPU ms':>12}{'catch-up ms':>13}"
          f"{'lag p50':>9}{'p90':>8}{'p99':>8}{'max':>9}")
    for name, visible, records, rate in scenarios:
        runs = [run_once(visible, records, rate) for _ in range(args.repeats)]
        lags = [lag for run in runs for lag in run["lags_ms"]]
        cpu_ms = sorted(run["gui_cpu_s"] * 1000 for run in runs)
        catch_ms = sorted(run["catch_up_s"] * 1000 for run in runs)
        summary = {
            "records": records,
            "rate": rate,
            "gui_cpu_ms_median": percentile(cpu_ms, 50),
            "catch_up_ms_median": percentile(catch_ms, 50),
            "lag_ms": {q: percentile(lags, q) for q in (50, 90, 99)} | {"max": max(lags), "n": len(lags)},
            "timed_out": any(run["timed_out"] for run in runs),
        }
        report["scenarios"][name] = {"summary": summary, "runs": runs}
        lag = summary["lag_ms"]
        print(f"{name:<18}{records:>8}{summary['gui_cpu_ms_median']:>12.0f}{summary['catch_up_ms_median']:>13.0f}"
              f"{lag[50]:>9.1f}{lag[90]:>8.1f}{lag[99]:>8.1f}{lag['max']:>9.1f}"
              + ("  TIMED OUT" if summary["timed_out"] else ""))

    service.shutdown()
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
