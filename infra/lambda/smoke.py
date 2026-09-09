"""Offline image smoke: the real handler and writers for every admitted tape format."""

from __future__ import annotations

import importlib
import json
import os
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import boto3
import pandas as pd
import pyarrow


class OfflineAWS:
    def __init__(self) -> None:
        self.body = b""
        self.uploads: list[dict[str, object]] = []
        self.commits: list[dict[str, object]] = []

    def head_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["VersionId"] == "snapshot-version"
        return {
            "VersionId": "snapshot-version",
            "ContentLength": len(self.body),
            "Metadata": {"as_of": "2025-02-01"},
        }

    def download_file(self, bucket: str, key: str, path: str, **request: object) -> None:
        assert bucket == "raw" and request["ExtraArgs"] == {"VersionId": "snapshot-version"}
        Path(path).write_bytes(self.body)

    def put_object(self, **kwargs: object) -> dict[str, str]:
        self.uploads.append(kwargs)
        return {"VersionId": f"output-{len(self.uploads)}"}

    def put_item(self, **kwargs: object) -> None:
        assert "ConditionExpression" in kwargs

    def update_item(self, **kwargs: object) -> None:
        assert len(self.uploads) % 4 == 0
        self.commits.append(kwargs)

    def put_metric_data(self, **kwargs: object) -> None:
        assert kwargs["Namespace"] == "LoanTapeDQ"


def main() -> None:
    assert platform.machine() in {"aarch64", "arm64"}
    assert boto3.__version__ and pyarrow.__version__
    aws = OfflineAWS()
    with (
        patch.dict(
            os.environ, {"RAW_BUCKET": "raw", "RESULTS_BUCKET": "results", "RUN_TABLE": "runs"}
        ),
        patch("boto3.client", return_value=aws),
    ):
        handler = importlib.import_module("handler")
    frame = pd.DataFrame(
        [
            {
                "Loan ID": 1,
                "Borrower ID": 2,
                "Loan amount": 1000,
                "Disbursal date": "2024-01-01",
                "Interest rate": 10,
                "Loan term": 12,
                "Loan status": "granted",
                "payments": "[]",
            }
        ]
    )
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        frame.to_csv(directory / "tape.csv", index=False)
        frame.to_parquet(directory / "tape.parquet", index=False)
        frame.to_json(directory / "tape.jsonl", orient="records", lines=True)
        frame.to_excel(directory / "tape.xlsx", index=False)
        for suffix in ("csv", "parquet", "jsonl", "xlsx"):
            aws.body = (directory / f"tape.{suffix}").read_bytes()
            result = handler.on_event(
                {
                    "source": "aws.s3",
                    "detail-type": "Object Created",
                    "detail": {
                        "bucket": {"name": "raw"},
                        "object": {
                            "key": f"snapshot/tape.{suffix}",
                            "version-id": "snapshot-version",
                            "size": len(aws.body),
                        },
                    },
                },
                None,
            )
            assert result["state"] == "COMMITTED" and result["schema_ok"]
            assert result["provenance"]["as_of"] == "2025-02-01"
            assert len(result["artifacts"]) == 4
            report = json.loads(aws.uploads[-4]["Body"])
            assert report["meta"]["rows_read"] == 1
    assert len(aws.uploads) == 16 and len(aws.commits) == 4
    print("ARM64 offline handler smoke passed: csv, parquet, jsonl, xlsx; four outputs per run")


if __name__ == "__main__":
    main()
