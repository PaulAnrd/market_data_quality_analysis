from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import config

logger = logging.getLogger(__name__)

MAX_MEMORY_BUDGET_BYTES = int(config.MAX_MEMORY_BUDGET_GB * 1024 ** 3)
MEMORY_OVERHEAD_MULTIPLIER = config.MEMORY_OVERHEAD_MULTIPLIER


class FileTooLargeError(Exception):
    pass


class MemoryBudgetExceededError(Exception):
    pass


def check_file_is_safe_to_load(path: Path) -> None:
    file_size = os.path.getsize(path)
    estimated_peak_bytes = file_size * MEMORY_OVERHEAD_MULTIPLIER

    if estimated_peak_bytes > MAX_MEMORY_BUDGET_BYTES:
        raise FileTooLargeError(
            f"{path}: {file_size / 1024**3:.1f} GB on disk, estimated peak memory "
            f"{estimated_peak_bytes / 1024**3:.1f} GB (x{MEMORY_OVERHEAD_MULTIPLIER:g} "
            f"overhead) exceeds the {MAX_MEMORY_BUDGET_BYTES / 1024**3:.0f} GB budget "
            f"(config.ini [general] max_memory_budget_gb) -- rejected before loading "
            f"to protect the system."
        )


def apply_process_memory_limit() -> None:
    try:
        import resource
    except ImportError:
        logger.warning(
            "resource module unavailable on this platform (POSIX only) -- "
            "RLIMIT_AS not applied, relying on the pre-flight check and the "
            "runtime memory watchdog instead."
        )
        return

    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    new_hard = MAX_MEMORY_BUDGET_BYTES if hard == resource.RLIM_INFINITY else min(MAX_MEMORY_BUDGET_BYTES, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BUDGET_BYTES, new_hard))
    except (ValueError, OSError) as exc:
        logger.warning(
            "Could not apply hard memory limit (RLIMIT_AS unsupported on this "
            "platform: %s) -- relying on the pre-flight check and the runtime "
            "memory watchdog instead.",
            exc,
        )
        return
    logger.info("Process memory hard-capped at %.1f GB (RLIMIT_AS)", MAX_MEMORY_BUDGET_BYTES / 1024**3)


def _current_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return max_rss if sys.platform == "darwin" else max_rss * 1024


def check_memory_budget(context: str = "") -> None:
    used_bytes = _current_rss_bytes()
    if used_bytes is None:
        return
    if used_bytes > MAX_MEMORY_BUDGET_BYTES:
        where = f" while {context}" if context else ""
        raise MemoryBudgetExceededError(
            f"Process memory ({used_bytes / 1024**3:.2f} GB) exceeded the "
            f"{MAX_MEMORY_BUDGET_BYTES / 1024**3:.0f} GB budget{where} "
            f"(config.ini [general] max_memory_budget_gb)."
        )
