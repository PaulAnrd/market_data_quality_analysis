from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import compare
import config
import continuity
import dashboard
import filename_metadata
import io_utils
import liquidity
import loader
import memory_guard
import outlier_treatment
import plots
import returns_stats
import structural

RECORDING_EXTENSIONS = {".csv", ".txt"}

logger = logging.getLogger(__name__)


def fatal(msg: str) -> None:
    logger.error(msg)
    raise SystemExit(2)


def _export_report(name: str, content: str, report_dir: Path | str) -> None:
    json_dir = Path(report_dir) / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    io_utils.atomic_write_text(json_dir / f"{name}.json", content)

    if config.GRAFANA_EXPORT_DIR:
        try:
            external_dir = Path(config.GRAFANA_EXPORT_DIR)
            external_dir.mkdir(parents=True, exist_ok=True)
            io_utils.atomic_write_text(external_dir / f"{name}.json", content)
        except Exception:
            logger.exception(
                "Could not write Grafana export copy for %s to config.ini "
                "[general] grafana_export_dir=%s", name, config.GRAFANA_EXPORT_DIR,
            )


INDEX_MAX_ENTRIES = 2000
_INDEX_FIELDS = ("name", "kind", "status", "generated_at", "date", "product",
                 "n_sources", "n_pairs", "n_divergences", "max_abs_diff_bps",
                 "n_exceptions", "n_checks", "error")


def _index_entry(**kwargs) -> dict:
    return {field: kwargs.get(field) for field in _INDEX_FIELDS}


def _write_index_copy(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    io_utils.atomic_write_text(path, content)


def _update_index(entry: dict, report_dir: Path | str) -> None:
    index_path = Path(report_dir) / "json" / "index.json"
    try:
        existing = json.loads(index_path.read_text()) if index_path.exists() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    key = (entry["name"], entry["kind"])
    existing = [e for e in existing if (e.get("name"), e.get("kind")) != key]
    existing.append(entry)
    existing.sort(key=lambda e: e.get("generated_at") or "")
    existing = existing[-INDEX_MAX_ENTRIES:]
    content = json.dumps(existing, indent=2)

    _write_index_copy(index_path, content)
    if config.GRAFANA_EXPORT_DIR:
        try:
            _write_index_copy(Path(config.GRAFANA_EXPORT_DIR) / "index.json", content)
        except Exception:
            logger.exception("Could not write Grafana index copy to %s", config.GRAFANA_EXPORT_DIR)


def run(input_path: Path, output_dir: Path, report_dir: Path) -> dict:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    memory_guard.check_file_is_safe_to_load(input_path)

    df_raw = loader.load_raw(str(input_path))
    if len(df_raw) < config.MIN_ROWS:
        raise ValueError(
            f"Recording has only {len(df_raw)} rows; need at least "
            f"{config.MIN_ROWS} for a meaningful assessment "
            f"(see config.ini [general] min_rows)."
        )
    df = loader.enrich(df_raw)
    memory_guard.check_memory_budget(f"loading {input_path.name}")

    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"

    report = {
        "source_file": input_path.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "structural": structural.check_structural(df_raw),
        "continuity": continuity.check_continuity(df),
        "price_level": returns_stats.check_price_level(df),
        "returns": returns_stats.check_returns(df),
        "liquidity": liquidity.check_liquidity(df),
    }
    memory_guard.check_memory_budget(f"computing statistics for {input_path.name}")

    plot_paths = plots.generate_all_plots(df, str(plots_dir))
    report["plots"] = [str(Path(p).relative_to(output_dir)) for p in plot_paths]
    memory_guard.check_memory_budget(f"generating plots for {input_path.name}")

    if config.OUTLIER_REPLACEMENT_MODE != outlier_treatment.MODE_NONE:
        cleaned = outlier_treatment.treat_outliers(df, config.OUTLIER_REPLACEMENT_MODE)
        cleaned_name = "cleaned_data.csv"
        io_utils.atomic_write_text(output_dir / cleaned_name, cleaned[loader.raw_schema_columns(cleaned)].to_csv(index=False))
        report["cleaned_data_file"] = cleaned_name
        memory_guard.check_memory_budget(f"writing cleaned data for {input_path.name}")

    io_utils.atomic_write_text(output_dir / "quality_report.json", json.dumps(report, indent=2))
    dashboard.write_dashboard(report, report["plots"], output_dir / "dashboard.html")

    _export_report(input_path.stem, json.dumps(report, indent=2), report_dir)
    n_exceptions, n_checks = dashboard.count_exceptions(report)
    _update_index(_index_entry(
        name=input_path.stem, kind="report", generated_at=report["generated_at"],
        n_exceptions=n_exceptions, n_checks=n_checks,
    ), report_dir)

    return report


def resolve_inputs(raw_paths: list[str]) -> list[Path]:
    resolved = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            resolved.extend(sorted(
                f for f in path.iterdir()
                if f.is_file() and f.suffix.lower() in RECORDING_EXTENSIONS
            ))
        else:
            resolved.append(path)
    return resolved


def _label_sources(paths: list[Path]) -> dict[str, Path]:
    stems = [p.stem for p in paths]
    labels: dict[str, Path] = {}
    for path, stem in zip(paths, stems):
        label = stem if stems.count(stem) == 1 else f"{path.parent.name}_{stem}"
        if label in labels and labels[label] != path:
            raise ValueError(
                f"Source label '{label}' is ambiguous between {labels[label]} and {path} "
                f"-- rename one of the files (or its parent directory) so compare_inputs can "
                f"tell them apart."
            )
        labels[label] = path
    return labels


def _run_comparison_core(
    labeled_paths: dict[str, Path],
    output_dir: Path,
    report_dir: Path,
    metadata_by_label: dict[str, filename_metadata.FeedMetadata] | None = None,
    instrument: dict | None = None,
) -> dict:
    downsample_points_per_day = config.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY
    effective_staleness_s = config.COMPARISON_MAX_STALENESS_S
    if downsample_points_per_day > 0:
        effective_staleness_s = max(effective_staleness_s, compare.SECONDS_PER_DAY / downsample_points_per_day)

    sources = {}
    raw_row_counts = {}
    for label, path in labeled_paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Input file not found: {path}")
        memory_guard.check_file_is_safe_to_load(path)
        df_raw = loader.load_raw(str(path))
        if len(df_raw) < config.MIN_ROWS:
            raise ValueError(
                f"{path}: only {len(df_raw)} rows; need at least {config.MIN_ROWS} "
                f"(see config.ini [general] min_rows)."
            )
        n_raw = len(df_raw)
        df_raw = compare.downsample(df_raw, downsample_points_per_day, config.COMPARISON_DOWNSAMPLE_MIN_ROWS)
        if len(df_raw) < n_raw:
            raw_row_counts[label] = n_raw
            logger.info(
                "Downsampled %s: %d -> %d ticks (target ~%d/day)",
                path.name, n_raw, len(df_raw), downsample_points_per_day,
            )
        sources[label] = loader.enrich(df_raw)
    memory_guard.check_memory_budget("loading comparison sources")

    report, aligned = compare.compare_sources(sources, max_staleness_s=effective_staleness_s)
    memory_guard.check_memory_budget("aligning comparison sources")

    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in report["sources"]:
        label = s["source"]
        s["file"] = labeled_paths[label].name
        if label in raw_row_counts:
            s["raw_rows"] = raw_row_counts[label]
        if metadata_by_label:
            m = metadata_by_label[label]
            s["metadata"] = {"date": m.date, "product": m.product, "market": m.market,
                              "server": m.server, "source": m.source}

    if metadata_by_label:
        report["attribution"] = compare.attribute_divergence(report["pairs"], metadata_by_label)
    if instrument:
        report["instrument"] = instrument
    report["status"] = compare.compute_status(report)

    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plot_paths = plots.generate_all_comparison_plots(
        aligned, list(sources.keys()), config.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, str(plots_dir)
    )
    report["plots"] = [str(Path(p).relative_to(output_dir)) for p in plot_paths]
    memory_guard.check_memory_budget("generating comparison plots")

    io_utils.atomic_write_text(output_dir / "comparison_report.json", json.dumps(report, indent=2))
    dashboard.write_comparison_dashboard(report, report["plots"], output_dir / "comparison_dashboard.html")

    _export_report(output_dir.name, json.dumps(report, indent=2), report_dir)
    n_divergences = sum(p.get("n_divergences") or 0 for p in report["pairs"])
    pair_diffs = [p["max_abs_diff_bps"] for p in report["pairs"] if p.get("max_abs_diff_bps") is not None]
    _update_index(_index_entry(
        name=output_dir.name, kind="compare", status=report["status"], generated_at=report["generated_at"],
        date=(instrument or {}).get("date"), product=(instrument or {}).get("product"),
        n_sources=len(report["sources"]), n_pairs=len(report["pairs"]), n_divergences=n_divergences,
        max_abs_diff_bps=max(pair_diffs) if pair_diffs else None,
    ), report_dir)

    return report


def run_comparison(input_paths: list[Path], output_dir: Path, report_dir: Path) -> dict:
    return _run_comparison_core(_label_sources(input_paths), output_dir, report_dir)


def run_comparison_group(members: list[filename_metadata.FeedMetadata], output_dir: Path, report_dir: Path) -> dict:
    labeled_paths: dict[str, Path] = {}
    metadata_by_label: dict[str, filename_metadata.FeedMetadata] = {}
    for m in members:
        label = m.label
        if label in labeled_paths and labeled_paths[label] != m.path:
            raise ValueError(
                f"Ambiguous recording label '{label}' within {m.date}/{m.product}: "
                f"{labeled_paths[label]} and {m.path} -- market+server+source should be "
                f"unique per instrument per day."
            )
        labeled_paths[label] = m.path
        metadata_by_label[label] = m
    instrument = {"date": members[0].date, "product": members[0].product}
    return _run_comparison_core(labeled_paths, output_dir, report_dir,
                                 metadata_by_label=metadata_by_label, instrument=instrument)


def _write_export_error(name: str, report_dir: str, error: BaseException, kind: str) -> None:
    try:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {"status": "ERROR", "generated_at": generated_at, "error": str(error)}
        _export_report(name, json.dumps(record, indent=2), report_dir)
        _update_index(_index_entry(
            name=name, kind=kind, status="ERROR", generated_at=generated_at, error=str(error),
        ), report_dir)
    except Exception:
        logger.exception("Could not write failure export for %s", name)


def run_report_mode(input_paths: list[Path], report_dir: str) -> None:
    logger.info(
        "Run started: report mode | %d input(s) | gap_threshold_s=%s alpha=%s hampel_window=%s "
        "hampel_threshold=%s outlier_replacement_mode=%s min_rows=%s max_memory_budget_gb=%s",
        len(input_paths), config.GAP_THRESHOLD_S, config.SIGNIFICANCE_LEVEL,
        config.HAMPEL_WINDOW, config.HAMPEL_THRESHOLD, config.OUTLIER_REPLACEMENT_MODE,
        config.MIN_ROWS, config.MAX_MEMORY_BUDGET_GB,
    )

    batch_start = time.perf_counter()
    failures = []
    for input_path in input_paths:
        output_dir = Path(report_dir) / input_path.stem
        file_start = time.perf_counter()
        try:
            report = run(input_path, output_dir, Path(report_dir))
        except Exception as exc:
            logger.exception("FAILED processing %s", input_path)
            _write_export_error(input_path.stem, report_dir, exc, kind="report")
            failures.append(input_path)
            continue
        elapsed = time.perf_counter() - file_start
        n_exceptions, n_checks = dashboard.count_exceptions(report)
        logger.info(
            "OK %s -> %s (%d/%d data-integrity exceptions, %.1fs)",
            input_path, output_dir / "dashboard.html", n_exceptions, n_checks, elapsed,
        )

    total_elapsed = time.perf_counter() - batch_start
    n_ok = len(input_paths) - len(failures)
    logger.info("Run finished: %d/%d recordings processed successfully in %.1fs",
                n_ok, len(input_paths), total_elapsed)

    if failures:
        logger.error("%d recording(s) failed: %s", len(failures), [str(p) for p in failures])
        raise SystemExit(1)


def _check_group_size(members: list, group_name: str) -> bool:
    if len(members) > config.COMPARISON_MAX_SOURCES:
        logger.error(
            "FAILED %s: %d recordings exceeds config.ini [comparison] max_sources=%d "
            "(pairs grow as n*(n-1)/2) -- narrow the group or raise max_sources.",
            group_name, len(members), config.COMPARISON_MAX_SOURCES,
        )
        return False
    return True


def run_compare_mode(input_paths: list[Path], report_dir: str) -> None:
    if config.COMPARISON_ALIGNMENT_FREQ_S <= 0:
        fatal(
            f"config.ini [comparison] alignment_freq_s={config.COMPARISON_ALIGNMENT_FREQ_S} "
            f"must be positive"
        )

    parsed, unmatched = [], []
    for path in input_paths:
        meta = filename_metadata.parse(path)
        if meta is None:
            unmatched.append(path)
        else:
            parsed.append(meta)

    if parsed:
        for path in unmatched:
            logger.warning(
                "Excluding %s from auto-grouping: doesn't match the "
                "YYYYMMDD_product_market_server_source naming convention", path,
            )
        _run_compare_grouped(parsed, report_dir)
    else:
        _run_compare_legacy(input_paths, report_dir)


def _run_compare_grouped(metas: list[filename_metadata.FeedMetadata], report_dir: str) -> None:
    groups = filename_metadata.group_by_instrument(metas)
    comparable = {key: members for key, members in groups.items() if len(members) >= 2}
    singletons = {key: members for key, members in groups.items() if len(members) < 2}

    logger.info(
        "Run started: compare mode (auto-grouped by date+product) | %d file(s) -> %d "
        "instrument(s) (%d comparable, %d with only 1 recording) | alignment_freq_s=%s "
        "max_staleness_s=%s price_diff_threshold_bps=%s downsample_points_per_day=%s",
        len(metas), len(groups), len(comparable), len(singletons),
        config.COMPARISON_ALIGNMENT_FREQ_S, config.COMPARISON_MAX_STALENESS_S,
        config.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, config.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY,
    )
    for date, product in singletons:
        logger.warning("Skipping %s/%s: only 1 recording, nothing to compare it against", date, product)

    if not comparable:
        fatal("no instrument (date+product) has 2+ recordings to compare -- see the warnings above")

    batch_start = time.perf_counter()
    failures = []
    for (date, product), members in comparable.items():
        group_name = f"{date}/{product}"
        export_name = f"{date}_{product}"
        if not _check_group_size(members, group_name):
            _write_export_error(export_name, report_dir, ValueError(f"{len(members)} recordings exceeds max_sources"), kind="compare")
            failures.append(group_name)
            continue
        group_output_dir = Path(report_dir) / export_name
        start = time.perf_counter()
        try:
            run_comparison_group(members, group_output_dir, Path(report_dir))
        except Exception as exc:
            logger.exception("FAILED comparing %s (%d recordings)", group_name, len(members))
            _write_export_error(export_name, report_dir, exc, kind="compare")
            failures.append(group_name)
            continue
        logger.info(
            "OK %s (%d recordings) -> %s (%.1fs)",
            group_name, len(members), group_output_dir / "comparison_dashboard.html",
            time.perf_counter() - start,
        )

    total_elapsed = time.perf_counter() - batch_start
    logger.info("Run finished: %d/%d instrument(s) compared successfully in %.1fs",
                len(comparable) - len(failures), len(comparable), total_elapsed)
    if failures:
        logger.error("%d instrument(s) failed: %s", len(failures), failures)
        raise SystemExit(1)


def _run_compare_legacy(input_paths: list[Path], report_dir: str) -> None:
    if len(input_paths) < 2:
        fatal("compare_inputs needs at least 2 recordings to compare")
    export_name = f"compare_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if not _check_group_size(input_paths, "this comparison"):
        _write_export_error(export_name, report_dir, ValueError(f"{len(input_paths)} recordings exceeds max_sources"), kind="compare")
        raise SystemExit(1)

    logger.info(
        "Run started: compare mode | %d source(s) -- none match the YYYYMMDD_product naming "
        "convention, so this run can't be named by date+product | alignment_freq_s=%s "
        "max_staleness_s=%s price_diff_threshold_bps=%s downsample_points_per_day=%s "
        "min_rows=%s max_memory_budget_gb=%s",
        len(input_paths), config.COMPARISON_ALIGNMENT_FREQ_S, config.COMPARISON_MAX_STALENESS_S,
        config.COMPARISON_PRICE_DIFF_THRESHOLD_BPS, config.COMPARISON_DOWNSAMPLE_POINTS_PER_DAY,
        config.MIN_ROWS, config.MAX_MEMORY_BUDGET_GB,
    )

    compare_output_dir = Path(report_dir) / export_name
    start = time.perf_counter()
    try:
        run_comparison(input_paths, compare_output_dir, Path(report_dir))
    except Exception as exc:
        logger.exception("FAILED comparing %d sources", len(input_paths))
        _write_export_error(export_name, report_dir, exc, kind="compare")
        raise SystemExit(1)
    logger.info(
        "OK comparison of %d sources -> %s (%.1fs)",
        len(input_paths), compare_output_dir / "comparison_dashboard.html",
        time.perf_counter() - start,
    )
