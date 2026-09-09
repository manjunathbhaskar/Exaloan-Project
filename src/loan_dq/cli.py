"""Command-line entry point.

loan-dq run data/loans.xlsx --out reports/                       # one tape, one process
loan-dq plan data/loans.csv --shards 4                           # row cut + as-of to pin
loan-dq run data/loans.csv --shard 2/4 --as-of 2025-01-23 --out shards/
loan-dq merge shards/*_report.json --out reports/                # tape-level checks once
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from loan_dq.config import Config, ConfigError
from loan_dq.engine import (
    AsOfError,
    ShardError,
    TapeRejectedError,
    plan_shards,
    resolve_as_of,
    run_pipeline,
)
from loan_dq.ingest.schema import ingest
from loan_dq.io.reader import TapeReadError, read_tape
from loan_dq.report.tables import TableContext
from loan_dq.report.writers import (
    write_console,
    write_csv,
    write_diary_coverage,
    write_json,
    write_payments_table,
)
from loan_dq.shard import load_report_dict, merge_reports


def _shard_spec(text: str) -> tuple[int, int]:
    try:
        index, count = (int(part) for part in text.split("/", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected i/n, e.g. 2/4, got {text!r}") from exc
    if count < 1 or not 1 <= index <= count:
        raise argparse.ArgumentTypeError(f"shard index must be within 1..n, got {text!r}")
    return index, count


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, default=None, help="YAML config (default: built-in)")
    p.add_argument("--out", type=Path, default=Path("reports"), help="output directory")
    p.add_argument(
        "--plugins",
        default=None,
        help="comma-separated opt-in annotators, e.g. 'triage' (overrides config; never a verdict)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="list every finding per loan")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress the console report")
    p.add_argument("--log-level", default="WARNING")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loan-dq", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="validate a loan tape (or one shard of it) and write reports"
    )
    p_run.add_argument("tape", type=Path, help="loan tape (.xlsx/.csv/.parquet/.jsonl)")
    p_run.add_argument(
        "--as-of", type=date.fromisoformat, default=None, help="as-of date YYYY-MM-DD"
    )
    p_run.add_argument(
        "--shard",
        type=_shard_spec,
        default=None,
        metavar="i/n",
        help="evaluate only row cut i of n (1-based); requires --as-of; merge the shard "
        "reports with 'loan-dq merge' to get the tape-level checks",
    )
    p_run.add_argument(
        "--tables",
        choices=["csv", "parquet", "none"],
        default="csv",
        help="format for the per-instalment payments table and per-loan diary coverage table",
    )
    _add_common(p_run)

    p_plan = sub.add_parser(
        "plan", help="print the row cut for n shards and the as-of date the whole tape implies"
    )
    p_plan.add_argument("tape", type=Path)
    p_plan.add_argument("--shards", type=int, required=True, metavar="n")
    p_plan.add_argument("--config", type=Path, default=None, help="YAML config (default: built-in)")
    p_plan.add_argument("--log-level", default="WARNING")
    p_plan.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_plan.set_defaults(plugins=None)

    p_merge = sub.add_parser(
        "merge",
        help="combine shard reports; run tape-level checks, tape health and plugins once "
        "(writes the JSON report, summary CSV and console; the payments and diary-coverage "
        "tables stay per shard)",
    )
    p_merge.add_argument("reports", type=Path, nargs="+", help="shard *_report.json files")
    _add_common(p_merge)
    p_merge.set_defaults(as_of=None)
    return parser


def _configure(args: argparse.Namespace) -> Config:
    level = logging.getLevelName(str(args.log_level).upper())
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = Config.load(args.config)
    if args.as_of is not None:
        config = config.model_copy(update={"as_of": args.as_of})
    if args.plugins is not None:
        names = [n.strip() for n in str(args.plugins).split(",") if n.strip()]
        config = config.model_copy(update={"plugins": names})
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _configure(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "plan":
        return _plan(args, config)
    if args.command == "merge":
        return _merge(args, config)
    return _run(args, config)


def _run(args: argparse.Namespace, config: Config) -> int:
    try:
        outcome = run_pipeline(args.tape, config, shard=args.shard)
    except TapeRejectedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (TapeReadError, ShardError, AsOfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = outcome.report
    stem = (
        args.tape.stem if report.shard is None else f"{args.tape.stem}.shard-{report.shard.label}"
    )
    write_json(report, args.out / f"{stem}_report.json")
    write_csv(report, args.out / f"{stem}_summary.csv")
    if args.tables != "none":
        loans = outcome.ingested.loans
        tables = TableContext.for_tape(report.as_of, config, report.input_sha256)
        write_payments_table(loans, tables, args.out / f"{stem}_payments.{args.tables}")
        write_diary_coverage(loans, tables, args.out / f"{stem}_diary_coverage.{args.tables}")
    if not args.quiet:
        write_console(report, sys.stdout, verbose=args.verbose)
    if not report.schema_ok or not report.loans:
        reason = "; ".join(outcome.ingested.issues) or "invalid schema or no usable rows"
        print(f"error: tape rejected: {reason}", file=sys.stderr)
        return 3
    return 1 if report.circuit_breaker_tripped else 0


def _plan(args: argparse.Namespace, config: Config) -> int:
    try:
        shards = plan_shards(args.tape, args.shards)
        # One linear pass over the tape, no rules: the as-of date every shard must be pinned to.
        ingested = ingest(read_tape(args.tape), config)
        if not ingested.schema.ok or not ingested.loans:
            raise ShardError("cannot plan a rejected tape: invalid schema or no usable rows")
        as_of, source = resolve_as_of(config, ingested)
    except (TapeReadError, ShardError, AsOfError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    plan = {
        "tape": args.tape.name,
        "rows": shards[-1].row_stop if shards else 0,
        "as_of": None if as_of is None else as_of.isoformat(),
        "as_of_source": source,
        "shards": [
            {"shard": f"{s.index}/{s.count}", "rows": [s.row_start, s.row_stop]} for s in shards
        ],
    }
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _merge(args: argparse.Namespace, config: Config) -> int:
    try:
        docs = [load_report_dict(p) for p in args.reports]
        report = merge_reports(docs, config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    stem = Path(report.input_file).stem
    write_json(report, args.out / f"{stem}_report.json")
    write_csv(report, args.out / f"{stem}_summary.csv")
    if not args.quiet:
        write_console(report, sys.stdout, verbose=args.verbose)
    if not report.schema_ok or not report.loans:
        return 3
    return 1 if report.circuit_breaker_tripped else 0


if __name__ == "__main__":
    raise SystemExit(main())
