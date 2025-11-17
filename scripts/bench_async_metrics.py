import argparse
import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Tuple

import psutil

from async_unzip import unzipper


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Benchmark async-unzip with optional backend/worker combinations."
    )
    parser.add_argument(
        "--archives",
        nargs="+",
        required=True,
        help="List of ZIP paths to benchmark. Use `label:path` to override labels.",
    )
    parser.add_argument(
        "--workers",
        nargs="+",
        type=int,
        default=[1, 2, 4],
        help="Worker counts to test (default: 1 2 4).",
    )
    parser.add_argument(
        "--backend",
        choices=unzipper.AVAILABLE_BACKENDS,
        default=None,
        help="Backend override (default: stdlib zlib).",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="How many runs per combo (default: 3).",
    )
    return parser.parse_args()


def _normalize_archives(raw: Iterable[str]) -> Iterable[Tuple[str, Path]]:
    for item in raw:
        if ":" in item:
            label, path = item.split(":", 1)
        else:
            label = Path(item).name
            path = item
        zip_path = Path(os.path.expanduser(path)).resolve()
        yield label, zip_path


async def _extract(zip_path: Path, destination: Path, workers: int, backend: str):
    await unzipper.unzip(
        str(zip_path),
        path=destination,
        max_workers=workers,
        backend=backend,
    )


def _benchmark(zip_path: Path, workers: int, backend: str, samples: int):
    proc = psutil.Process()
    def run_once():
        tmp_dir = Path(tempfile.mkdtemp())
        cpu_samples = []
        mem_samples = []
        stop = threading.Event()

        def sampler():
            proc.cpu_percent(interval=None)
            while not stop.is_set():
                cpu_samples.append(proc.cpu_percent(interval=0.1))
                mem_samples.append(proc.memory_info().rss)

        sampler_thread = threading.Thread(target=sampler)
        sampler_thread.start()
        start = time.perf_counter()
        try:
            asyncio.run(_extract(zip_path, tmp_dir, workers, backend))
        finally:
            stop.set()
            sampler_thread.join()
            shutil.rmtree(tmp_dir, ignore_errors=True)
        duration = time.perf_counter() - start
        cpu_avg = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
        cpu_max = max(cpu_samples) if cpu_samples else 0.0
        mem_avg = (
            sum(mem_samples) / len(mem_samples) / (1024 * 1024)
            if mem_samples
            else 0.0
        )
        mem_max = max(mem_samples) / (1024 * 1024) if mem_samples else 0.0
        return duration, cpu_avg, cpu_max, mem_avg, mem_max

    durations = []
    cpu_avgs = []
    cpu_maxes = []
    mem_avgs = []
    mem_maxes = []
    for _ in range(samples):
        dur, cpu_avg, cpu_max, mem_avg, mem_max = run_once()
        durations.append(dur)
        cpu_avgs.append(cpu_avg)
        cpu_maxes.append(cpu_max)
        mem_avgs.append(mem_avg)
        mem_maxes.append(mem_max)

    return {
        "avg_seconds": sum(durations) / len(durations),
        "samples": durations,
        "cpu_avg": sum(cpu_avgs) / len(cpu_avgs),
        "cpu_max": max(cpu_maxes),
        "mem_avg_mb": sum(mem_avgs) / len(mem_avgs),
        "mem_max_mb": max(mem_maxes),
    }


def main():
    args = _parse_args()
    for label, path in _normalize_archives(args.archives):
        if not path.exists():
            raise SystemExit(f"Archive not found: {path}")
        size_mb = round(path.stat().st_size / 1_000_000, 2)
        for workers in args.workers:
            stats = _benchmark(path, workers, args.backend, args.samples)
            stats.update(
                {
                    "backend": unzipper.DECOMPRESS_BACKEND,
                    "max_workers": workers,
                    "dataset": label,
                    "dataset_size_mb": size_mb,
                }
            )
            print(json.dumps(stats))


if __name__ == "__main__":
    main()
