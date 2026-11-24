"""Benchmark lossless output encoding the way the pipeline's save step runs it.

Calls ``manga_translator.utils.generic.save_pil_image`` on real manga pages
(``doc/images``) repeatedly and reports per-call latency percentiles, output
size, and whether the encoded file decodes back to the exact source pixels.

Point ``--repo`` at another checkout to measure a different revision with the
same harness, for example before/after a change::

    python test/bench_lossless_save.py --repo /path/to/old/checkout --runs 10
    python test/bench_lossless_save.py --runs 10 --json after.json

Not collected by pytest; run it directly.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import platform
import sys
import time
from pathlib import Path

DEFAULT_PAGES = ("0012.png", "20012.png", "110012.png")


def percentile(samples, q):
    """Linear-interpolated percentile (numpy's default method)."""
    ordered = sorted(samples)
    if not ordered:
        return math.nan
    rank = (len(ordered) - 1) * q / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize(samples):
    return {
        "n": len(samples),
        "mean": sum(samples) / len(samples),
        "p50": percentile(samples, 50),
        "p90": percentile(samples, 90),
        "p99": percentile(samples, 99),
        "min": min(samples),
        "max": max(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                        help="checkout whose code is measured (default: this one)")
    parser.add_argument("--runs", type=int, default=10, help="timed encodes per page")
    parser.add_argument("--format", default="webp", help="output format (webp, png, avif, heic)")
    parser.add_argument("--effort", type=int, default=100, help="cli.compression_effort (default: 100)")
    parser.add_argument("--pages", nargs="*", help="images to encode (default: doc/images showcase pages)")
    parser.add_argument("--json", help="write raw samples and summaries to this file")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    sys.path.insert(0, str(repo))

    import numpy as np
    from PIL import Image

    from manga_translator.utils.generic import save_pil_image

    pages = [Path(p) for p in args.pages] if args.pages else [repo / "doc" / "images" / n for n in DEFAULT_PAGES]
    fmt = args.format.upper().replace("HEIC", "HEIF")

    # Load encoder plugins before timing anything.
    with Image.new("RGB", (8, 8)) as tiny:
        save_pil_image(tiny, io.BytesIO(), format=fmt, lossless=True, compression_effort=args.effort)

    report = {
        "repo": str(repo),
        "format": fmt,
        "effort": args.effort,
        "runs_per_page": args.runs,
        "python": sys.version.split()[0],
        "machine": platform.processor() or platform.machine(),
        "pages": {},
    }
    all_samples = []
    print(f"repo={repo}  format={fmt}  effort={args.effort}  runs/page={args.runs}")
    print(f"{'page':<14}{'size':>12}{'p50 s':>9}{'p90 s':>9}{'p99 s':>9}{'mean s':>9}{'bytes':>11}  exact")
    for path in pages:
        with Image.open(path) as source:
            source.load()
            # Translated outputs of JPEG sources are RGB; an opaque RGBA page is too.
            opaque = source.mode == "RGBA" and source.getextrema()[3] == (255, 255)
            image = source.convert("RGB") if opaque else source.copy()
        samples = []
        data = b""
        for _ in range(args.runs):
            buffer = io.BytesIO()
            start = time.perf_counter()
            save_pil_image(image, buffer, format=fmt, lossless=True, compression_effort=args.effort)
            samples.append(time.perf_counter() - start)
            data = buffer.getvalue()
        with Image.open(io.BytesIO(data)) as decoded:
            exact = bool(np.array_equal(np.asarray(decoded.convert(image.mode)), np.asarray(image)))
        stats = summarize(samples)
        report["pages"][path.name] = {"samples": samples, "summary": stats, "bytes": len(data), "pixel_exact": exact}
        all_samples.extend(samples)
        print(f"{path.name:<14}{image.width:>6}x{image.height:<5}{stats['p50']:>9.2f}{stats['p90']:>9.2f}"
              f"{stats['p99']:>9.2f}{stats['mean']:>9.2f}{len(data):>11}  {exact}")
        image.close()

    overall = summarize(all_samples)
    report["overall"] = overall
    print(f"{'ALL':<14}{'':>12}{overall['p50']:>9.2f}{overall['p90']:>9.2f}{overall['p99']:>9.2f}"
          f"{overall['mean']:>9.2f}   (n={overall['n']}, min={overall['min']:.2f}, max={overall['max']:.2f})")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
