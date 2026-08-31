"""Bounded, ordered parallelism for offline work only, never instrument I/O."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from itertools import islice
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator


logger = logging.getLogger(__name__)
_ENVIRONMENT_LOCK = threading.RLock()


def worker_count(requested: int, *, auto_limit: int = 8) -> int:
    """Keep an automatic run modest; explicit counts can use larger machines."""

    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
        raise ValueError("worker count must be an integer >= 0 (0=auto)")
    count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    # Windows ProcessPoolExecutor supports at most 61 workers.
    limit = 61 if sys.platform == "win32" else count
    if requested:
        return max(1, min(requested, count, limit))
    return max(1, min(count - 1, auto_limit, limit))


def process_workers(requested: int, *, auto_limit: int = 8) -> int:
    count = worker_count(requested, auto_limit=auto_limit)
    if count <= 1:
        return 1
    main = sys.modules.get("__main__")
    filename = getattr(main, "__file__", None)
    if multiprocessing.current_process().name != "MainProcess":
        return 1  # no nested pools
    if not filename or not Path(filename).is_file():
        logger.info("Интерактивный запуск: анализ выполняется без дочерних процессов")
        return 1
    return count


@contextmanager
def process_pool(count: int) -> Iterator[ProcessPoolExecutor]:
    """Use spawn on every OS, with one BLAS thread and Agg in each child.

    Environment changes only affect newly spawned interpreters. Existing
    parent-side numerical libraries and GUI settings are not reconfigured.
    Restore all variables even when spawning or a task fails.
    """

    limits = {
        name: "1" for name in (
            "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS",
        )
    }
    limits["MPLBACKEND"] = "Agg"
    with _ENVIRONMENT_LOCK:
        previous = {name: os.environ.get(name) for name in limits}
        os.environ.update(limits)
        try:
            with ProcessPoolExecutor(
                max_workers=count, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                yield pool
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def ordered_bounded_map(pool: Any, function: Callable, items: Iterable,
                        *, pending_limit: int) -> Iterator[Any]:
    """Unlike pre-3.14 Executor.map, never enqueue an entire large experiment."""

    pending: deque = deque()
    try:
        for item in items:
            pending.append(pool.submit(function, item))
            if len(pending) >= pending_limit:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
    finally:
        for future in pending:
            future.cancel()


def _run_batch(job: tuple[Callable, tuple]) -> list:
    function, items = job
    return [function(item) for item in items]


def map_analysis_groups(function: Callable, jobs: Callable[[], Iterable], *,
                        total: int, settings: Any, label: str) -> list:
    """Batch pure calculations and keep row order identical to serial mode."""

    start = time.perf_counter()
    count = min(process_workers(settings.workers), max(total, 1))
    if total < settings.parallel_min_groups:
        count = 1
    logger.info("%s: %d групп, процессов %d", label, total, count)
    if count == 1:
        result = []
        last_bucket = -1
        for item in jobs():
            result.append(function(item))
            bucket = len(result) * 10 // max(total, 1)
            if total >= 256 and bucket > last_bucket:
                last_bucket = bucket
                logger.info("%s: %.0f%% (%d/%d)", label, 100 * len(result) / total, len(result), total)
    else:
        def batches():
            source = iter(jobs())
            while batch := tuple(islice(source, settings.parallel_batch_size)):
                yield function, batch

        result = []
        last_bucket = -1
        try:
            with process_pool(count) as pool:
                for batch in ordered_bounded_map(pool, _run_batch, batches(), pending_limit=2 * count):
                    result.extend(batch)
                    bucket = len(result) * 10 // max(total, 1)
                    if bucket > last_bucket:
                        last_bucket = bucket
                        logger.info("%s: %.0f%% (%d/%d)", label, 100 * len(result) / total, len(result), total)
        except BrokenProcessPool:
            # Only pure analysis is retried. Invalid data and ordinary worker
            # exceptions propagate; they are not disguised as successful fits.
            logger.warning("%s: дочерний процесс завершился; повтор без параллелизма", label)
            result = [function(item) for item in jobs()]
    logger.info("%s завершен: %.2f с", label, time.perf_counter() - start)
    return result


def map_readonly_files(function: Callable, items: Iterable, *, workers: int) -> list:
    """Parallel file reads, no writes and no device objects in worker threads."""

    count = worker_count(workers)
    total = len(items) if hasattr(items, "__len__") else 0

    def collect(values):
        result = []
        last_bucket = -1
        for value in values:
            result.append(value)
            bucket = len(result) * 10 // max(total, 1)
            if total >= 128 and bucket > last_bucket:
                last_bucket = bucket
                logger.info("Чтение CSV: %.0f%% (%d/%d)", 100 * len(result) / total, len(result), total)
        return result

    if count == 1:
        return collect(function(item) for item in items)
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="analysis-csv") as pool:
        return collect(ordered_bounded_map(pool, function, items, pending_limit=2 * count))
