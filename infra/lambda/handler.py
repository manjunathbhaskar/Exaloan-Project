"""Version-pinned ingestion with a fenced, atomic DynamoDB run/manifest commit.

Artifacts are private diagnostics until the run table commits an accepted manifest.
Acceptance is a technical publication decision, not borrower credit approval.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import loan_dq
from loan_dq.config import Config
from loan_dq.engine import run_pipeline
from loan_dq.io.reader import sha256_of
from loan_dq.report.tables import TableContext
from loan_dq.report.writers import (
    write_csv,
    write_diary_coverage,
    write_json,
    write_payments_table,
)
from loan_dq.rules.registry import RULESET_VERSION

_s3 = boto3.client("s3")
_cloudwatch = boto3.client(
    "cloudwatch",
    config=BotoConfig(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}),
)
_runs = boto3.client("dynamodb")

RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
RAW_BUCKET = os.environ["RAW_BUCKET"]
RUN_TABLE = os.environ["RUN_TABLE"]
STAGE = os.environ.get("STAGE", "dev")
CONFIG_PATH = os.environ.get("LOAN_DQ_CONFIG")
MAX_INPUT_BYTES = int(os.environ.get("MAX_INPUT_BYTES", str(50 * 1024 * 1024)))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "660"))
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "600"))

_CONTENT_TYPE = {".json": "application/json", ".csv": "text/csv"}
_OUTPUTS = ("report.json", "summary.csv", "payments.csv", "diary_coverage.csv")
_SUFFIXES = {".csv", ".parquet", ".jsonl", ".xlsx"}


def _implementation_digest() -> str:
    package = Path(loan_dq.__file__).resolve().parent
    handler = Path(__file__).resolve()
    sources = [(f"loan_dq/{p.relative_to(package)}", p) for p in sorted(package.rglob("*.py"))]
    sources.append(("handler.py", handler))
    for name in ("requirements-runtime.lock", "Dockerfile"):
        path = handler.parent / name
        if not path.is_file():
            path = handler.parents[2] / name
        sources.append((name, path))
    digest = hashlib.sha256()
    for name, path in sources:
        digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


IMPLEMENTATION_SHA256 = _implementation_digest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _conditional_failure(exc: Exception) -> bool:
    return (
        getattr(exc, "response", {}).get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _emit_circuit_breaker(*, tripped: bool) -> None:
    try:
        _cloudwatch.put_metric_data(
            Namespace="LoanTapeDQ",
            MetricData=[
                {
                    "MetricName": "CircuitBreakerTripped",
                    "Dimensions": [{"Name": "Stage", "Value": STAGE}],
                    "Value": 1.0 if tripped else 0.0,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "circuit_breaker_metric_delivery_failed stage=%s", STAGE
        )


def _source(event: dict[str, Any]) -> tuple[str, str, int]:
    if not isinstance(event, dict) or (
        event.get("source") != "aws.s3" or event.get("detail-type") != "Object Created"
    ):
        raise ValueError("expected an EventBridge S3 Object Created event")
    try:
        detail = event["detail"]
        bucket = detail["bucket"]["name"]
        obj = detail["object"]
        key, version, size = obj["key"], obj["version-id"], obj["size"]
    except (KeyError, TypeError) as exc:
        raise ValueError("event must include bucket, key, version-id and size") from exc
    if bucket != RAW_BUCKET:
        raise ValueError("event bucket is not the configured raw bucket")
    if not isinstance(key, str) or not key or Path(key).suffix not in _SUFFIXES:
        raise ValueError("unsupported object key")
    if not isinstance(version, str) or not version.strip() or version == "null":
        raise ValueError("an immutable source version-id is required")
    if type(size) is not int or not 0 < size <= MAX_INPUT_BYTES:
        raise ValueError("object size is outside the admitted range")
    return key, version, size


def _claim(run_id: str, attempt: str) -> dict[str, Any] | None:
    now = int(time.time())
    try:
        _runs.put_item(
            TableName=RUN_TABLE,
            Item={
                "run_id": {"S": run_id},
                "state": {"S": "RUNNING"},
                "attempt": {"S": attempt},
                "lease_until": {"N": str(now + LEASE_SECONDS)},
            },
            ConditionExpression=(
                "attribute_not_exists(run_id) OR (#state = :running AND lease_until <= :now)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":running": {"S": "RUNNING"}, ":now": {"N": str(now)}},
        )
    except Exception as exc:
        if not _conditional_failure(exc):
            raise
        current = _runs.get_item(
            TableName=RUN_TABLE, Key={"run_id": {"S": run_id}}, ConsistentRead=True
        ).get("Item", {})
        if current.get("state", {}).get("S") == "COMMITTED":
            return json.loads(current["manifest"]["S"])
        raise RuntimeError("run lease is held; retry after the current attempt completes") from exc
    return None


def _release(run_id: str, attempt: str) -> None:
    try:
        _runs.update_item(
            TableName=RUN_TABLE,
            Key={"run_id": {"S": run_id}},
            UpdateExpression="SET lease_until = :expired",
            ConditionExpression="#state = :running AND attempt = :attempt",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":expired": {"N": "0"},
                ":running": {"S": "RUNNING"},
                ":attempt": {"S": attempt},
            },
        )
    except Exception:
        logging.getLogger(__name__).warning("run_lease_release_failed run_id=%s", run_id)


def _commit(run_id: str, attempt: str, manifest: dict[str, Any]) -> None:
    _runs.update_item(
        TableName=RUN_TABLE,
        Key={"run_id": {"S": run_id}},
        UpdateExpression=(
            "SET #state = :committed, publication_status = :status, manifest = :manifest"
        ),
        ConditionExpression="#state = :running AND attempt = :attempt AND lease_until > :now",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={
            ":running": {"S": "RUNNING"},
            ":committed": {"S": "COMMITTED"},
            ":attempt": {"S": attempt},
            ":status": {"S": manifest["publication_status"]},
            ":manifest": {"S": _json(manifest)},
            ":now": {"N": str(int(time.time()))},
        },
    )


def on_event(event: dict[str, Any], _context: object) -> dict[str, Any]:
    if LEASE_SECONDS <= MAX_RUNTIME_SECONDS or MAX_INPUT_BYTES <= 0:
        raise ValueError("lease must exceed the Lambda timeout; input limit must be positive")
    key, version, size = _source(event)
    head = _s3.head_object(Bucket=RAW_BUCKET, Key=key, VersionId=version)
    if head.get("VersionId") != version or head.get("ContentLength") != size:
        raise ValueError("source version or size does not match the event")
    raw_as_of = head.get("Metadata", {}).get("as_of", "")
    try:
        as_of = dt.date.fromisoformat(raw_as_of)
        if as_of.isoformat() != raw_as_of:
            raise ValueError("not a canonical date")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "source metadata as_of must be a snapshot date in YYYY-MM-DD format"
        ) from exc
    config = Config.load(Path(CONFIG_PATH) if CONFIG_PATH else None)
    if config.as_of is not None and config.as_of != as_of:
        raise ValueError("configured as_of conflicts with source snapshot metadata")
    config = config.model_copy(update={"as_of": as_of})
    with TemporaryDirectory(prefix="loan-dq-") as temporary:
        workdir = Path(temporary)  # Lambda's only writable path
        local_tape = workdir / f"input{Path(key).suffix}"
        _s3.download_file(RAW_BUCKET, key, str(local_tape), ExtraArgs={"VersionId": version})
        if local_tape.stat().st_size != size:
            raise ValueError("downloaded size does not match the admitted source version")
        provenance = {
            "source": {"bucket": RAW_BUCKET, "key": key, "version_id": version, "size": size},
            "input_sha256": sha256_of(local_tape),
            "implementation_sha256": IMPLEMENTATION_SHA256,
            "ruleset_version": RULESET_VERSION,
            "config_digest": config.digest(),
            "config_sha256": hashlib.sha256(
                _json(config.model_dump(mode="json", by_alias=True)).encode()
            ).hexdigest(),
            "as_of": as_of.isoformat(),
            "as_of_source": "s3_version_metadata:as_of",
        }
        run_id = hashlib.sha256(_json(provenance).encode()).hexdigest()
        attempt = uuid.uuid4().hex
        existing = _claim(run_id, attempt)
        if existing is not None:
            return existing
        try:
            outcome = run_pipeline(local_tape, config)
            report = outcome.report
            if (
                report.input_sha256 != provenance["input_sha256"]
                or report.config_digest != provenance["config_digest"]
                or report.ruleset_version != RULESET_VERSION
                or report.as_of != as_of
            ):
                raise RuntimeError("report provenance differs from claimed run")
            publication_status = report.publication_status
            if publication_status not in {"accepted", "review_required", "rejected"}:
                raise RuntimeError("unknown report publication status")
            out_dir = workdir / "out"
            write_json(report, out_dir / _OUTPUTS[0])
            write_csv(report, out_dir / _OUTPUTS[1])
            tables = TableContext.for_tape(report.as_of, config, report.input_sha256)
            write_payments_table(outcome.ingested.loans, tables, out_dir / _OUTPUTS[2])
            write_diary_coverage(outcome.ingested.loans, tables, out_dir / _OUTPUTS[3])
            run_prefix = f"diagnostics/runs/{run_id}/attempts/{attempt}"
            artifacts = []
            for name in _OUTPUTS:
                produced = out_dir / name
                body = produced.read_bytes()
                object_key = f"{run_prefix}/{name}"
                stored = _s3.put_object(
                    Bucket=RESULTS_BUCKET,
                    Key=object_key,
                    Body=body,
                    ContentType=_CONTENT_TYPE[produced.suffix],
                )
                output_version = stored.get("VersionId")
                if not output_version or output_version == "null":
                    raise RuntimeError("results bucket must return an immutable object version")
                artifacts.append(
                    {
                        "bucket": RESULTS_BUCKET,
                        "key": object_key,
                        "version_id": output_version,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
                    }
                )
            manifest = {
                "run_id": run_id,
                "state": "COMMITTED",
                "publication_status": publication_status,
                "attempt": attempt,
                "provenance": provenance,
                "artifacts": artifacts,
                "schema_ok": report.schema_ok,
                "circuit_breaker_tripped": report.circuit_breaker_tripped,
            }
            _commit(run_id, attempt, manifest)
        except Exception:
            # After async acceptance, execution failures use Lambda's retry / DLQ path.
            _release(run_id, attempt)
            raise
        _emit_circuit_breaker(tripped=report.circuit_breaker_tripped)
        return manifest
