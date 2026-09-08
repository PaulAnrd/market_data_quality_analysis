#!/usr/bin/env python3
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def _reexec_into_venv() -> None:
    if not VENV_PYTHON.exists():
        return
    try:
        already_in_venv = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    except OSError:
        return
    if not already_in_venv:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def _expand(patterns: list[str], resolve_inputs) -> list[Path]:
    matches = []
    for pattern in patterns:
        matches.extend(sorted(glob.glob(pattern)))
    return resolve_inputs(matches)


def main() -> None:
    _reexec_into_venv()

    if len(sys.argv) != 2:
        print("Usage: mdsq.py <path-to-config-file>", file=sys.stderr)
        raise SystemExit(2)

    config_path = Path(sys.argv[1])
    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        raise SystemExit(2)

    os.environ["MDSQ_CONFIG_PATH"] = str(config_path.resolve())
    sys.path.insert(0, str(SRC))

    import config
    import logging_setup
    import memory_guard
    import outlier_treatment
    import main as pipeline

    logging_setup.configure_logging()
    memory_guard.apply_process_memory_limit()

    if config.OUTLIER_REPLACEMENT_MODE not in outlier_treatment.VALID_MODES:
        pipeline.fatal(
            f"config.ini [outliers] replacement_mode={config.OUTLIER_REPLACEMENT_MODE} is invalid; "
            f"must be one of {sorted(outlier_treatment.VALID_MODES)}"
        )

    if not config.RUN_REPORT_INPUTS and not config.RUN_COMPARE_INPUTS:
        pipeline.fatal("config [run] has neither report_inputs nor compare_inputs set -- nothing to do")

    failed = False

    if config.RUN_REPORT_INPUTS:
        paths = _expand(config.RUN_REPORT_INPUTS, pipeline.resolve_inputs)
        if not paths:
            pipeline.logger.warning("report_inputs matched no files: %s", config.RUN_REPORT_INPUTS)
        else:
            try:
                pipeline.run_report_mode(paths, config.RUN_REPORT_DIR)
            except SystemExit:
                failed = True

    if config.RUN_COMPARE_INPUTS:
        paths = _expand(config.RUN_COMPARE_INPUTS, pipeline.resolve_inputs)
        if not paths:
            pipeline.logger.warning("compare_inputs matched no files: %s", config.RUN_COMPARE_INPUTS)
        else:
            try:
                pipeline.run_compare_mode(paths, config.RUN_REPORT_DIR)
            except SystemExit:
                failed = True

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
