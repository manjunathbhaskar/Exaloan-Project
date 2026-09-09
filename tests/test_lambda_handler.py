from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from loan_dq.config import Config
from loan_dq.report.schema import QuarantineRecord, TapeFinding
from tests.conftest import SyntheticLoan

ROOT = Path(__file__).resolve().parents[1]


class ConditionalFailureError(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Runs:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.fail_claim = False
        self.fail_release = False
        self.commit_failure = None
        self.before_commit = None

    def put_item(self, **request):
        if self.fail_claim:
            raise RuntimeError("claim unavailable")
        with self.lock:
            item = request["Item"]
            key = item["run_id"]["S"]
            current = self.items.get(key)
            assert "attribute_not_exists(run_id) OR" in request["ConditionExpression"]
            now = int(request["ExpressionAttributeValues"][":now"]["N"])
            if current and (
                current["state"]["S"] != "RUNNING" or int(current["lease_until"]["N"]) > now
            ):
                raise ConditionalFailureError()
            self.items[key] = copy.deepcopy(item)

    def get_item(self, **request):
        assert request["ConsistentRead"] is True
        with self.lock:
            return {"Item": copy.deepcopy(self.items.get(request["Key"]["run_id"]["S"], {}))}

    def update_item(self, **request):
        with self.lock:
            item = self.items[request["Key"]["run_id"]["S"]]
            values = request["ExpressionAttributeValues"]
            committing = ":manifest" in values
            if committing and self.before_commit:
                self.before_commit(item)
            if item["state"]["S"] != "RUNNING" or item["attempt"] != values[":attempt"]:
                raise ConditionalFailureError()
            if not committing:
                if self.fail_release:
                    raise RuntimeError("release unavailable")
                item["lease_until"] = values[":expired"]
                return
            assert "lease_until > :now" in request["ConditionExpression"]
            if int(item["lease_until"]["N"]) <= int(values[":now"]["N"]):
                raise ConditionalFailureError()
            if self.commit_failure == "before":
                raise RuntimeError("commit unavailable")
            manifest = json.loads(values[":manifest"]["S"])
            assert len(manifest["artifacts"]) == 4
            item.update(
                state=values[":committed"],
                publication_status=values[":status"],
                manifest=values[":manifest"],
            )
            if self.commit_failure == "after":
                raise RuntimeError("commit response lost")


class S3:
    def __init__(self, body):
        self.body = body
        self.as_of = "2025-02-01"
        self.uploads = []
        self.downloads = []
        self.heads = []
        self.fail_upload = None
        self.head_override = {}

    def head_object(self, **request):
        self.heads.append(request)
        return {
            "VersionId": request["VersionId"],
            "ContentLength": len(self.body),
            "Metadata": {"as_of": self.as_of},
            **self.head_override,
        }

    def download_file(self, bucket, key, path, **request):
        self.downloads.append((bucket, key, Path(path), request["ExtraArgs"]))
        Path(path).write_bytes(self.body)

    def put_object(self, **request):
        if self.fail_upload == len(self.uploads) + 1:
            raise RuntimeError("upload unavailable")
        version = f"output-version-{len(self.uploads)}"
        self.uploads.append({**request, "VersionId": version})
        return {"VersionId": version}


@pytest.fixture
def harness(monkeypatch):
    body = SyntheticLoan().frame().to_csv(index=False).encode()
    s3, runs = S3(body), Runs()
    metric_calls = []
    cloudwatch = SimpleNamespace(put_metric_data=lambda **request: metric_calls.append(request))
    clients = {"s3": s3, "dynamodb": runs, "cloudwatch": cloudwatch}

    def client(name, **kwargs):
        if name == "cloudwatch":
            settings = kwargs["config"]
            assert settings.connect_timeout == settings.read_timeout == 1
            assert settings.retries == {"total_max_attempts": 1}
        return clients[name]

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=SimpleNamespace))
    for key, value in {
        "RAW_BUCKET": "raw",
        "RESULTS_BUCKET": "results",
        "RUN_TABLE": "runs",
        "STAGE": "prod",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LOAN_DQ_CONFIG", raising=False)
    spec = importlib.util.spec_from_file_location(
        "offline_handler", ROOT / "infra/lambda/handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": "raw"},
            "object": {"key": "lender/tape.csv", "version-id": "v1", "size": len(body)},
        },
    }
    return SimpleNamespace(handler=module, s3=s3, runs=runs, event=event, metrics=metric_calls)


def test_warm_repeat_exact_outputs_version_and_provenance(harness, monkeypatch):
    h = harness
    real_run = h.handler.run_pipeline

    def run_with_unrelated_output(path, config):
        (path.parent / "out").mkdir()
        (path.parent / "out" / "unrelated.csv").write_text("do not publish")
        return real_run(path, config)

    monkeypatch.setattr(h.handler, "run_pipeline", run_with_unrelated_output)
    h.event["detail"]["object"]["key"] = "a/tape+%20.csv"
    first = h.handler.on_event(h.event, None)
    h.event["detail"]["object"]["version-id"] = "v2"
    second = h.handler.on_event(h.event, None)
    assert first["run_id"] != second["run_id"]
    assert first["publication_status"] == "accepted"
    assert len(h.s3.uploads) == 8
    assert {Path(item["Key"]).name for item in h.s3.uploads} == set(h.handler._OUTPUTS)
    assert h.s3.downloads[0][1] == "a/tape+%20.csv"
    assert [d[3] for d in h.s3.downloads] == [{"VersionId": "v1"}, {"VersionId": "v2"}]
    assert h.s3.downloads[0][2].parent != h.s3.downloads[1][2].parent
    assert all(not download[2].parent.exists() for download in h.s3.downloads)
    provenance = first["provenance"]
    assert provenance["source"] == {
        "bucket": "raw",
        "key": "a/tape+%20.csv",
        "version_id": "v1",
        "size": len(h.s3.body),
    }
    assert provenance["as_of"] == "2025-02-01"
    assert provenance["as_of_source"] == "s3_version_metadata:as_of"
    assert provenance["input_sha256"] == hashlib.sha256(h.s3.body).hexdigest()
    assert len(provenance["config_sha256"]) == 64
    assert provenance["ruleset_version"] == h.handler.RULESET_VERSION
    for artifact, upload in zip(first["artifacts"], h.s3.uploads[:4], strict=True):
        assert artifact["sha256"] == hashlib.sha256(upload["Body"]).hexdigest()
        assert artifact["version_id"] == upload["VersionId"]
    stored = h.runs.items[first["run_id"]]
    assert stored["state"] == {"S": "COMMITTED"}
    assert json.loads(stored["manifest"]["S"]) == first
    assert h.metrics[0]["MetricData"][0]["Dimensions"] == [{"Name": "Stage", "Value": "prod"}]


def test_changed_implementation_is_a_new_logical_run(harness, monkeypatch):
    h = harness
    first = h.handler.on_event(h.event, None)
    monkeypatch.setattr(h.handler, "IMPLEMENTATION_SHA256", "b" * 64, raising=False)
    second = h.handler.on_event(h.event, None)
    assert second["run_id"] != first["run_id"]
    assert second["provenance"]["implementation_sha256"] == "b" * 64
    assert len(h.s3.uploads) == 8


def test_duplicate_concurrency_has_one_logical_commit(harness, monkeypatch):
    h = harness
    entered, release = threading.Event(), threading.Event()
    real_run = h.handler.run_pipeline
    calls = []

    def blocking_run(path, config):
        calls.append(path)
        entered.set()
        assert release.wait(5)
        return real_run(path, config)

    monkeypatch.setattr(h.handler, "run_pipeline", blocking_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(h.handler.on_event, h.event, None)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="lease is held"):
                h.handler.on_event(h.event, None)
        finally:
            release.set()
        result = first.result(timeout=5)
    assert h.handler.on_event(h.event, None) == result
    assert len(calls) == 1 and len(h.s3.uploads) == 4


def test_partial_upload_never_commits_and_retry_uses_new_attempt(harness):
    h = harness
    h.s3.fail_upload = 2
    with pytest.raises(RuntimeError, match="upload unavailable"):
        h.handler.on_event(h.event, None)
    [item] = h.runs.items.values()
    assert item["state"] == {"S": "RUNNING"} and "manifest" not in item
    assert item["lease_until"] == {"N": "0"}
    old_prefix = h.s3.uploads[0]["Key"].rsplit("/", 1)[0]
    h.s3.fail_upload = None
    result = h.handler.on_event(h.event, None)
    assert len(h.s3.uploads) == 5
    assert all(not artifact["key"].startswith(old_prefix) for artifact in result["artifacts"])
    assert all(not download[2].parent.exists() for download in h.s3.downloads)


@pytest.mark.parametrize("failure", ["before", "after"])
def test_commit_outage_is_recoverable_and_lost_response_does_not_republish(harness, failure):
    h = harness
    h.runs.commit_failure = failure
    with pytest.raises(RuntimeError, match="commit"):
        h.handler.on_event(h.event, None)
    h.runs.commit_failure = None
    result = h.handler.on_event(h.event, None)
    assert result["state"] == "COMMITTED"
    assert len(h.s3.uploads) == (8 if failure == "before" else 4)


def test_claim_outage_prevents_execution_and_uploads(harness, monkeypatch):
    h = harness
    h.runs.fail_claim = True
    monkeypatch.setattr(h.handler, "run_pipeline", lambda *_: pytest.fail("must not execute"))
    with pytest.raises(RuntimeError, match="claim unavailable"):
        h.handler.on_event(h.event, None)
    assert not h.s3.uploads


def test_expired_lease_and_attempt_fencing(harness):
    h = harness
    h.runs.before_commit = lambda item: item.update(attempt={"S": "new-owner"})
    with pytest.raises(ConditionalFailureError):
        h.handler.on_event(h.event, None)
    [item] = h.runs.items.values()
    assert item["attempt"] == {"S": "new-owner"}
    assert item["state"] == {"S": "RUNNING"} and "manifest" not in item
    h.runs.before_commit = None
    item["lease_until"] = {"N": "0"}
    result = h.handler.on_event(h.event, None)
    assert result["attempt"] != "new-owner" and len(h.s3.uploads) == 8


def test_commit_after_lease_expiry_is_not_published(harness):
    h = harness
    h.runs.before_commit = lambda item: item.update(lease_until={"N": "0"})
    with pytest.raises(ConditionalFailureError):
        h.handler.on_event(h.event, None)
    assert all("manifest" not in item for item in h.runs.items.values())


def test_release_outage_preserves_original_error_and_stale_recovery(harness):
    h = harness
    h.s3.fail_upload = 1
    h.runs.fail_release = True
    with pytest.raises(RuntimeError, match="upload unavailable"):
        h.handler.on_event(h.event, None)
    [item] = h.runs.items.values()
    assert int(item["lease_until"]["N"]) > 0
    item["lease_until"] = {"N": "0"}
    h.s3.fail_upload = None
    assert h.handler.on_event(h.event, None)["state"] == "COMMITTED"


@pytest.mark.parametrize(
    "reason,status",
    [
        ("breaker", "review_required"),
        ("coverage", "review_required"),
        ("quarantine", "review_required"),
        ("tape_finding", "review_required"),
        ("schema", "rejected"),
        ("no_loans", "rejected"),
    ],
)
def test_held_tape_commits_diagnostics_without_retry(harness, monkeypatch, reason, status):
    h = harness
    real_run = h.handler.run_pipeline

    def held_run(path, config):
        outcome = real_run(path, config)
        if reason == "breaker":
            outcome.report.circuit_breaker_tripped = True
        elif reason == "coverage":
            outcome.report.loans[0].validation_coverage = "partial"
        elif reason == "quarantine":
            outcome.report.quarantined.append(QuarantineRecord(1, "bad", "unreadable"))
        elif reason == "tape_finding":
            outcome.report.tape_findings.append(TapeFinding("I2", "high", "hold"))
        elif reason == "no_loans":
            outcome.report.loans = []
        else:
            outcome.report.schema_ok = False
        return outcome

    monkeypatch.setattr(h.handler, "run_pipeline", held_run)
    result = h.handler.on_event(h.event, None)
    assert result["publication_status"] == status and result["state"] == "COMMITTED"
    assert h.handler.on_event(h.event, None) == result
    assert len(h.s3.uploads) == 4
    assert all(artifact["key"].startswith("diagnostics/") for artifact in result["artifacts"])


def test_metric_outage_cannot_fail_a_committed_run(harness, monkeypatch):
    h = harness

    def unavailable(**_):
        raise RuntimeError("metric outage")

    monkeypatch.setattr(h.handler._cloudwatch, "put_metric_data", unavailable)
    result = h.handler.on_event(h.event, None)
    assert result["state"] == "COMMITTED"
    assert h.handler.on_event(h.event, None) == result and len(h.s3.uploads) == 4


def test_same_stem_different_source_and_config_get_distinct_namespaces(harness, monkeypatch):
    h = harness
    first = h.handler.on_event(h.event, None)
    h.event["detail"]["object"]["key"] = "other/tape.csv"
    second = h.handler.on_event(h.event, None)
    monkeypatch.setattr(h.handler.Config, "load", lambda _: Config(lender="different"))
    third = h.handler.on_event(h.event, None)
    assert len({r["run_id"] for r in (first, second, third)}) == 3
    assert first["provenance"]["input_sha256"] == third["provenance"]["input_sha256"]
    assert second["provenance"]["config_sha256"] != third["provenance"]["config_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("version-id", None),
        ("version-id", "null"),
        ("size", True),
        ("size", 0),
        ("size", 100_000_000),
        ("key", "tape.exe"),
    ],
)
def test_invalid_event_object_is_rejected_before_download(harness, field, value):
    h = harness
    h.event["detail"]["object"][field] = value
    with pytest.raises(ValueError):
        h.handler.on_event(h.event, None)
    assert not h.s3.heads and not h.s3.downloads


@pytest.mark.parametrize(
    "event",
    [{}, {"source": "aws.s3", "detail-type": "Object Created"}, {"source": "other", "detail": {}}],
)
def test_invalid_event_structure(harness, event):
    with pytest.raises(ValueError):
        harness.handler.on_event(event, None)
    assert not harness.s3.heads


def test_untrusted_bucket_is_rejected_before_s3_access(harness):
    harness.event["detail"]["bucket"]["name"] = "untrusted"
    with pytest.raises(ValueError, match="configured raw bucket"):
        harness.handler.on_event(harness.event, None)
    assert not harness.s3.heads


@pytest.mark.parametrize("as_of", ["", "2025-02-30", "20250201", "2025-02-01T00:00:00Z"])
def test_snapshot_metadata_is_required_not_upload_timestamp(harness, as_of):
    harness.s3.as_of = as_of
    with pytest.raises(ValueError, match="snapshot date"):
        harness.handler.on_event(harness.event, None)
    assert not harness.s3.downloads


def test_version_and_size_head_mismatch_fail_admission(harness):
    h = harness
    for override in ({"ContentLength": 100_000_000}, {"VersionId": "latest"}):
        h.s3.head_override = override
        with pytest.raises(ValueError, match="does not match"):
            h.handler.on_event(h.event, None)
    assert not h.s3.downloads


def test_lease_must_exceed_lambda_runtime(harness, monkeypatch):
    monkeypatch.setattr(harness.handler, "LEASE_SECONDS", 600)
    with pytest.raises(ValueError, match="lease must exceed"):
        harness.handler.on_event(harness.event, None)
    assert not harness.s3.heads


def test_report_provenance_mismatch_never_uploads(harness, monkeypatch):
    h = harness
    real_run = h.handler.run_pipeline

    def corrupted_report(path, config):
        outcome = real_run(path, config)
        outcome.report.input_sha256 = "not-the-source-hash"
        return outcome

    monkeypatch.setattr(h.handler, "run_pipeline", corrupted_report)
    with pytest.raises(RuntimeError, match="provenance differs"):
        h.handler.on_event(h.event, None)
    assert not h.s3.uploads


def test_source_snapshot_must_not_conflict_with_configuration(harness, monkeypatch):
    h = harness
    config = Config.model_validate({"as_of": "2024-01-01"})
    monkeypatch.setattr(h.handler.Config, "load", lambda _: config)
    with pytest.raises(ValueError, match="conflicts with source"):
        h.handler.on_event(h.event, None)
    assert not h.s3.downloads


def test_missing_result_version_cannot_commit(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.s3, "put_object", lambda **_: {})
    with pytest.raises(RuntimeError, match="immutable object version"):
        h.handler.on_event(h.event, None)
    assert all("manifest" not in item for item in h.runs.items.values())


def test_ruleset_and_snapshot_date_are_part_of_identity(harness, monkeypatch):
    h = harness
    first = h.handler.on_event(h.event, None)
    h.s3.as_of = "2025-02-02"
    second = h.handler.on_event(h.event, None)
    real_run = h.handler.run_pipeline

    def revised_run(path, config):
        outcome = real_run(path, config)
        outcome.report.ruleset_version = "revised-ruleset"
        return outcome

    monkeypatch.setattr(h.handler, "RULESET_VERSION", "revised-ruleset")
    monkeypatch.setattr(h.handler, "run_pipeline", revised_run)
    third = h.handler.on_event(h.event, None)
    assert len({result["run_id"] for result in (first, second, third)}) == 3
    assert first["provenance"]["as_of"] != second["provenance"]["as_of"]
    assert third["provenance"]["ruleset_version"] == "revised-ruleset"
