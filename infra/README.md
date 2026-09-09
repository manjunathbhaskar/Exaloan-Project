# infra — ingestion and publication sample

**Implemented, not deployed:** versioned private S3 raw/results buckets, an encrypted
DynamoDB run table, ARM64 detector Lambda, EventBridge trigger, encrypted DLQ/logs/SNS,
and error/DLQ/circuit-breaker alarms. No AWS resources or images are published by CI.

## Input and publication contract

- Only EventBridge `aws.s3` / `Object Created` events from the configured `RAW_BUCKET`
  are admitted. The event must include a non-null `version-id`, key and positive size
  at most 50 MiB; supported suffixes are `.csv`, `.parquet`, `.jsonl`, `.xlsx`.
  EventBridge keys are used verbatim, not decoded as form-encoded S3 notifications.
- Both HEAD and download request that exact version, never latest. Its metadata must
  contain `as_of=YYYY-MM-DD`: the lender's **snapshot date**, not upload time. This is
  required in every stage, propagated into `Config`, and must not conflict with a
  configured date. Size/version mismatch fails admission before processing.
- A SHA-256 run ID covers source bucket/key/version/size, content SHA-256, ruleset,
  implementation digest, effective configuration hashes and explicit as-of provenance.
  The implementation digest includes packaged Python sources, handler, runtime lock and
  Dockerfile (including its pinned base). It is not a registry image digest. Basenames and clocks
  do not identify logical runs. Each invocation has its own `TemporaryDirectory`.
- DynamoDB conditionally claims the run with a 660-second lease, longer than the
  600-second Lambda timeout. Expired claims can be reclaimed; attempt tokens fence
  stale writers. Caught infrastructure failures release only their own claim; a lost
  release or hard timeout recovers through lease expiry and event retry/replay.
- Each attempt writes exactly `report.json`, `summary.csv`, `payments.csv`, and
  `diary_coverage.csv` under `diagnostics/runs/<run-id>/attempts/<attempt>/`.
  Only after all four writes succeed does one conditional DynamoDB update store
  `state=COMMITTED`, `publication_status`, and the **durable ready manifest** together.
  The manifest includes provenance and each artifact's key, S3 version, size and hash.
  There is no S3 ready marker that can get ahead of the database commit.
- **Consumer barrier:** a serving loader must consistently read the run item, require
  both `COMMITTED` and `accepted`, and fetch only the manifest's exact object versions.
  It must never discover work by listing S3 or trust an attempt prefix. No consumer
  read permissions or serving loader are provisioned here; diagnostics stay private
  to explicitly authorized operational access. A future broker/loader must enforce
  this contract before granting any end-user access.
- The report's `publication_status` is `rejected` for schema failure/no usable loans,
  `review_required` for quarantine, incomplete validation, circuit breaker or high/
  critical tape findings, otherwise `accepted` (with additional report safety gates).
  Held/rejected reports commit diagnostics and **do not raise retryable failures**.
  Acceptance is technical publication eligibility, **not borrower credit approval**.
  Duplicate delivery returns the already committed manifest. Partial/uncommitted
  attempts are not publications. This is logical idempotency, not exactly-once execution.

## Failure handling and retention

EventBridge retries **delivery to Lambda** twice over at most two hours; Lambda's
asynchronous queue independently retries **handler execution** twice with a six-hour
maximum event age. Both failure paths use the encrypted DLQ. It is not a source queue,
so no SQS redrive policy is appropriate. DLQ replay and abandoned-artifact cleanup are
manual operational responsibilities, not automated destructive jobs.

KMS grants cover EventBridge DLQ sends and CloudWatch encrypted SNS alerts, scoped by
source account and rule/alarm ARN; log encryption uses the log-group encryption context.
Metrics are best effort and stage-dimensioned, with one-second connect/read timeouts
and no SDK retry, never part of the publication transaction.
Stages are strictly `dev|staging|prod`: staging/prod retain buckets, run table, DLQ, logs
and key on deletion/replacement, with one-year logs; dev uses deletion policies and
one-month logs. S3 versioning is not Object Lock/WORM. DynamoDB PITR is enabled; run
records have no TTL, so idempotency is not silently lost through automatic expiry.

## Verify without deploying

From the repository root (Python 3.12 and Node required):

```bash
uv sync --frozen --all-extras
uv run --frozen --all-extras pytest tests/test_lambda_handler.py tests/test_infra.py
(cd infra && PATH="../.venv/bin:$PATH" npx --yes aws-cdk@2.150.0 synth -c stage=dev)
(cd infra && PATH="../.venv/bin:$PATH" npx --yes aws-cdk@2.150.0 synth -c stage=prod)
```

CDK assertions require the optional `infra` extra; without it those tests explicitly
skip. CI installs all extras and verifies the CDK import before testing. **Synth stages
image assets and emits templates; it does not build Docker images.** Dev/prod synth
and offline handler/CDK tests were run locally. The four-format smoke also passed on
native ARM64 with the locked runtime, but the local Docker daemon was unavailable:
container build/execution and cloud delivery have not been verified locally.

The Dockerfile pins the public ECR ARM64 Python 3.12 base by registry digest, installs
`requirements-runtime.lock` with hashes (including boto3, PyArrow and the build backend),
and installs project code without dependency resolution/build isolation. CI uses
`uv.lock` for test/infra dependencies. The PR and main pipelines synthesize both stages,
then on a native ARM cloud runner (requiring an ARM-enabled Bitbucket plan) build
**the staged CDK asset** with its declared
platform/Dockerfile, check its template tag, and execute `lambda/smoke.py` by immutable
local image ID with networking disabled. `verified-image.json` records the CDK asset
hash and tested local image ID; it is not a published ECR digest. The automatic steps use
no AWS credentials. A separate **manual** `publish` step on `main` authenticates through
Bitbucket OIDC identity federation — STS exchanges the short-lived token for temporary
credentials scoped to a push-only ECR role, gated by a trust policy on this repo UUID and
branch — then rebuilds the verified asset and pushes it by asset id. No long-lived key
exists in the pipeline, and no `latest` tag or `cdk deploy` is run. Future deployment must
promote that published asset through an approved process, not rebuild an unrelated image.

Not implemented: serving/IAM consumer broker, human review workflow, automated replay,
large-tape Fargate/Step Functions routing, VPC endpoints, integrations or load testing.
The 50 MiB admission limit is not a guarantee against expanded XLSX/Parquet memory
usage; larger or resource-heavy tapes require a separately validated execution tier.
