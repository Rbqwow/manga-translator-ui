"""Benchmark how GPU cache cleanup from translation workers slows a GPU stage.

Before commit 93b69fd every concurrent translation worker called
``_cleanup_gpu_memory(aggressive=True)`` (torch.cuda.synchronize +
empty_cache + ipc_collect) after each request, while the inpainting thread
ran on the GPU. This harness runs an inpainting-sized convolution workload in
one thread and reports its per-iteration latency percentiles in two modes:

* ``none``: nothing else touches the GPU (current behaviour);
* ``workers``: N threads call ``release_gpu_memory(aggressive=True)`` every
  ``--interval`` seconds each, like N translation workers finishing requests.

Requires a CUDA GPU. ``--repo`` selects the checkout providing
``manga_translator.utils.gpu_memory``.

    python test/bench_cuda_cleanup.py --iterations 300 --json cuda.json

Not collected by pytest; run it directly.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
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
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--iterations", type=int, default=300, help="timed workload iterations per mode")
    parser.add_argument("--size", type=int, default=1024, help="input side length (inpainting_size)")
    parser.add_argument("--workers", type=int, default=3, help="translation workers (translation_concurrency)")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between cleanups per worker")
    parser.add_argument("--rounds", type=int, default=2, help="alternating none/workers rounds")
    parser.add_argument("--device", default="cuda", help="cuda (default) or cpu to dry-run the harness")
    parser.add_argument("--json", help="write samples to this file")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.repo).resolve()))
    import torch

    from manga_translator.utils.gpu_memory import release_gpu_memory

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        print("CUDA is not available; this benchmark needs an NVIDIA GPU (use --device cpu to dry-run).")
        return 2
    device = torch.device(args.device)
    torch.backends.cudnn.benchmark = True
    layers = []
    channels = [3, 64, 128, 128, 64, 3]
    for c_in, c_out in zip(channels, channels[1:]):
        layers += [torch.nn.Conv2d(c_in, c_out, 3, padding=1), torch.nn.ReLU()]
    model = torch.nn.Sequential(*layers[:-1]).to(device).eval()

    def iteration():
        with torch.no_grad():
            image = torch.rand(1, 3, args.size, args.size, device=device)
            out = model(image)
            # Allocate and free intermediate buffers the way per-page inference does.
            mask = (out.mean(dim=1, keepdim=True) > 0.5).float()
            result = out * mask + image * (1 - mask)
            _ = result.sum().item()  # forces completion, like copying a page back to the CPU

    def run_mode(mode):
        stop = threading.Event()
        cleanups = [0]

        def cleaner():
            while not stop.wait(args.interval):
                release_gpu_memory(str(device), aggressive=True)
                cleanups[0] += 1

        threads = [threading.Thread(target=cleaner, daemon=True) for _ in range(args.workers if mode == "workers" else 0)]
        for _ in range(10):
            iteration()
        for thread in threads:
            thread.start()
        samples = []
        for _ in range(args.iterations):
            start = time.perf_counter()
            iteration()
            samples.append(time.perf_counter() - start)
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        return samples, cleanups[0]

    name = torch.cuda.get_device_name(device) if device.type == "cuda" else "cpu"
    print(f"device={name}  size={args.size}  workers={args.workers}  interval={args.interval}s  "
          f"iterations={args.iterations}  rounds={args.rounds}")
    report = {"device": name, "args": vars(args), "modes": {"none": [], "workers": []}, "cleanups": 0}
    for _ in range(args.rounds):
        for mode in ("none", "workers"):
            samples, cleanups = run_mode(mode)
            report["modes"][mode].extend(samples)
            report["cleanups"] += cleanups
    print(f"{'mode':<10}{'p50 ms':>9}{'p90 ms':>9}{'p99 ms':>9}{'max ms':>9}{'mean ms':>9}")
    for mode, samples in report["modes"].items():
        ms = [s * 1000 for s in samples]
        print(f"{mode:<10}{percentile(ms, 50):>9.2f}{percentile(ms, 90):>9.2f}{percentile(ms, 99):>9.2f}"
              f"{max(ms):>9.2f}{sum(ms) / len(ms):>9.2f}")
    print(f"cleanup calls during 'workers' mode: {report['cleanups']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
