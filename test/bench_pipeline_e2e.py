"""End-to-end batch translation benchmark with local models and no API calls.

Runs the real CLI (``python -m manga_translator local``) of a checkout on a
folder of pages with the concurrent pipeline, ``translator=original`` (keeps
the source text, so detection, OCR, inpainting, rendering and saving all run
but nothing is sent to an API). Per-page completion times are taken from the
output files as they appear. Reports wall time, time to first page, pages per
minute, and percentiles of the interval between finished pages (steady-state
cost per page).

    python test/bench_pipeline_e2e.py --repo /path/to/old/checkout --pages 30 --use-gpu --json before.json
    python test/bench_pipeline_e2e.py --pages 30 --use-gpu --json after.json

The config comes from the measured checkout's config/config-example.json with
the overrides below, so both revisions run the same settings. Pass
``--format jpg`` to take lossless encoding out of the comparison.

Not collected by pytest; run it directly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_PAGES = ("0012.png", "20012.png", "110012.png")


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
                        help="checkout whose CLI is run (default: this one)")
    parser.add_argument("--python", default=sys.executable, help="interpreter with the project's dependencies")
    parser.add_argument("--pages", type=int, default=30, help="pages to translate (showcase pages repeated)")
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--format", default=None, help="output format override (default: config-example value)")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--translation-concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--keep", action="store_true", help="keep the temporary work folder")
    parser.add_argument("--json", help="write results to this file")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    work = Path(tempfile.mkdtemp(prefix="mt-e2e-"))
    src, out = work / "input", work / "output"
    src.mkdir()
    out.mkdir()
    sources = [repo / "doc" / "images" / name for name in DEFAULT_PAGES]
    for index in range(args.pages):
        source = sources[index % len(sources)]
        shutil.copyfile(source, src / f"{index:04d}{source.suffix}")

    config = json.loads((repo / "config" / "config-example.json").read_text(encoding="utf-8"))
    config["translator"]["translator"] = "original"
    config["translator"]["enable_post_translation_check"] = False
    config["cli"].update(batch_concurrent=True, batch_size=args.batch_size,
                         translation_concurrency=args.translation_concurrency,
                         overwrite=True, use_gpu=bool(args.use_gpu))
    if args.format:
        config["cli"]["format"] = args.format
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    command = [args.python, "-m", "manga_translator", "local", "-i", str(src), "-o", str(out),
               "--config", str(config_path), "--overwrite", "--concurrent",
               "--batch-size", str(args.batch_size),
               "--translation-concurrency", str(args.translation_concurrency)]
    if args.use_gpu:
        command.append("--use-gpu")

    finished = {}
    stop = threading.Event()

    def watch():
        image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
        while not stop.is_set():
            for path in out.rglob("*"):
                if path.suffix.lower() in image_suffixes and path.stem not in finished and not path.name.endswith(".tmp"):
                    finished[path.stem] = time.perf_counter()
            stop.wait(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    log_path = work / "cli.log"
    print(f"repo={repo}  pages={args.pages}  gpu={args.use_gpu}  format={config['cli'].get('format')}  "
          f"lossless={config['cli'].get('save_lossless')}  effort={config['cli'].get('compression_effort')}")
    start = time.perf_counter()
    watcher.start()
    with open(log_path, "w", encoding="utf-8") as log:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        result = subprocess.run(command, cwd=str(repo), stdout=log, stderr=subprocess.STDOUT,
                                timeout=args.timeout, env=env)
    wall = time.perf_counter() - start
    time.sleep(0.2)
    stop.set()
    watcher.join(timeout=2)

    times = sorted(t - start for t in finished.values())
    intervals = [b - a for a, b in zip(times, times[1:])]
    report = {
        "repo": str(repo), "pages": args.pages, "gpu": args.use_gpu, "returncode": result.returncode,
        "wall_s": wall, "finished_pages": len(times), "first_page_s": times[0] if times else math.nan,
        "pages_per_min": (len(times) / wall * 60) if wall else 0.0,
        "interval_s": {q: percentile(intervals, q) for q in (50, 90, 99)},
        "completion_times_s": times, "config": config["cli"], "log": str(log_path),
    }
    iv = report["interval_s"]
    print(f"exit={result.returncode}  finished={len(times)}/{args.pages}  wall={wall:.1f}s  "
          f"first page={report['first_page_s']:.1f}s  {report['pages_per_min']:.1f} pages/min")
    print(f"per-page interval  p50={iv[50]:.2f}s  p90={iv[90]:.2f}s  p99={iv[99]:.2f}s")
    # Success means every page was written; a crash while the interpreter
    # shuts down afterwards is reported but does not invalidate the timings.
    complete = len(times) == args.pages
    report["complete"] = complete
    if result.returncode != 0 or not complete:
        print(f"CLI log: {log_path}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    if not args.keep and complete:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
