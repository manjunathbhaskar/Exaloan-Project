# Part 2: repeatable loan-tape onboarding on AWS

The [README](../README.md) is the concise presentation guide. This document describes
the implemented sample, its consumer contract and the production extensions. Nothing
has been deployed. Synthesis, local tests and a deployment rehearsal are different evidence.

## 1. One-page architecture

![Loan-tape ingestion, publication and serving](architecture.png)

Blue nodes and solid paths represent the implemented sample, not deployed resources.
Grey dashed nodes/paths are design-only; purple identifies an optional, unselected proposal.
Amber boxes explain contracts and limitations, not additional AWS services. Observability
is a shared capability, not a processing step that waits for the serving layer.

Edit [architecture.mmd](architecture.mmd); [architecture.svg](architecture.svg) and the PNG
are rendered outputs. The five sections preserve the ingestion, processing, storage,
serving and operations view while separating executable paths from future work.

Regenerate both images from the repository root (Mermaid CLI requires Chromium):

```bash
npx --yes @mermaid-js/mermaid-cli@11.17.0 -i docs/architecture.mmd -o docs/architecture.svg -b white -w 2000
npx --yes @mermaid-js/mermaid-cli@11.17.0 -i docs/architecture.mmd -o docs/architecture.png -b white -w 2000 -s 2
```

```text
IMPLEMENTED
Lender upload → versioned S3 raw → EventBridge → container Lambda
                                                ├→ DynamoDB run claim/status
                                                ├→ S3 per-attempt artifacts
                                                └→ CloudWatch metrics/logs
Event delivery / execution failures → SQS DLQ → CloudWatch alarm → encrypted SNS

DESIGNED EXTENSIONS
Accepted run → transactional loader → Aurora PostgreSQL → authorised API → scoring
Large tape → Standard Step Functions → physical shards / Fargate → validated merge
Review-required run → human review → separately authorised release or corrected replay
```

The uploader onboarding/authentication flow, database loader, API, review interface,
large-tape orchestration and organisation-wide controls are design-only. The sample
implements safe evaluation and a durable publication decision, not a complete lending platform.
Macie, the separate PII vault, CloudTrail/X-Ray configuration and a pager/Lambda responder
are also unbuilt designs. CloudTrail data events require explicit selection; X-Ray needs
instrumentation and sampling. A future responder needs sanitised context, bounded retry
classification and authorised replay to the handler, not a loop back into the DLQ.
Neptune/ArangoDB graph investigation is an optional, unselected idea requiring suitable
linking data and a validated use case; it is not a dependency of detection or publication.

## 2. Input and run identity

The source contract includes an authorised raw bucket/key, an immutable S3 **version ID**
and an explicit **as-of date**. The detector reads the event's version, not whatever version
is current when a delayed event arrives. Object size is checked before downloading;
oversized or unsupported input must not exhaust the small-tape worker.

A logical run binds the source version and content hash to the detector/configuration,
ruleset and snapshot. The filename alone is never a tenant identity or an idempotency key.
An exact retry may reuse a completed run; a new source version or detection policy is a
new logical evaluation. The lender identity must originate from trusted onboarding and
prefix authorisation, not a field a borrower can edit in the uploaded file.

The upload service and IAM prefix restrictions are not implemented by this sample.
The handler's bucket/source validation is defence in depth, not a complete tenant boundary.

## 3. Publication is a protocol, not four S3 writes

1. **Claim:** conditionally acquire the logical run in DynamoDB with an attempt owner and
   bounded lease. Duplicate events cannot publish competing successful decisions.
2. **Evaluate:** use an invocation-specific temporary directory; run the same Part 1 code
   as the CLI. Persist the source/configuration provenance alongside the output.
3. **Stage:** write the explicit output set under the attempt's own prefix. Individual S3
   writes are atomic; the collection is not. A partially written attempt is not published.
4. **Decide:** atomically store the complete manifest and final status in one conditional
   DynamoDB update while still holding the ownership token. The run record contains the
   authoritative manifest; there is no separately published S3 ready marker.
5. **Consume:** read only a finalised run whose technical status is `accepted`, then use
   its manifest to locate the output objects. Never discover accepted data by listing S3
   prefixes or taking the newest filename.

`review_required` and `rejected` runs retain diagnostic evidence but are not accepted
for automatic serving. Incomplete coverage, quarantined rows and the integrity circuit
breaker are acceptance inputs. Per-loan flags remain part of accepted reports; technical
acceptance is not credit approval and must not bypass downstream eligibility rules.

An expired worker must not overwrite a newer owner's decision. Orphaned attempt objects
can be expired by a lifecycle policy after a retention window. Cleanup is not a transaction
rollback. The guarantee is a single durable publication decision, **not exactly-once
execution** and not an atomic transaction spanning S3 and DynamoDB.

## 4. Failure handling and self-healing

| Failure | Behaviour / production response |
|---|---|
| EventBridge cannot deliver to Lambda | Bounded target-delivery retries; encrypted delivery DLQ; monitor failed DLQ delivery too. |
| Accepted asynchronous Lambda invocation fails | Lambda's execution retry/DLQ path, separate from EventBridge delivery retries. |
| Temporary storage/S3/DynamoDB failure | Retry the same logical run; stale ownership is recoverable after the lease; partial attempts stay unpublished. |
| Schema rejection or insufficient coverage | Persist rejected/review-required diagnostics; do not repeatedly retry deterministic data defects as infrastructure failures. |
| Metrics unavailable | Do not convert an already completed business operation into failed processing. |
| Missing or corrupt shard | Retry that shard and validate population accounting before publishing; never silently omit its loans. |
| Repeated poison input | Alert and investigate with the exact source version; an operator must authorise replay or replacement. |

The DLQ is not a self-executing recovery workflow. An operator replay tool, authorisation,
audit trail and review/release process still need implementation. An EventBridge/Lambda
DLQ is not the same as an SQS source queue with an automatic source-queue redrive policy.

Use quarantine **status** rather than moving or copying raw objects into a watched prefix.
That preserves source identity and avoids accidental ingestion loops. Recovery never
changes a financial value; proposed corrections require a new version and approval.

## 5. Scaling and serving design

For the supplied tape, one Lambda invocation is sufficient. For large deliveries:

- Normalise once into physical loan/payment partitions and a manifest. Repeatedly reading
  the entire Excel file in every worker is not a scalable partitioning strategy.
- Use a **Standard Step Functions parent** for long-running orchestration and Distributed
  Map. Express children are appropriate only when their work fits the five-minute limit.
- Use Standard `.sync` integration for Fargate jobs. The packaged Python code is reusable,
  but a Fargate task must override the Lambda-specific container entrypoint/command.
- Bound concurrency against Lambda, DynamoDB, KMS and database capacity, with per-lender
  admission limits. The service's maximum concurrency is not a safe operating target.
- Validate every expected input row as an evaluated or explicitly quarantined record.
  A tolerated task-failure percentage must never certify an incomplete population.
- Load accepted output into Aurora PostgreSQL staging tables; reconcile counts and promote
  a run pointer transactionally. Use `(tenant, run, source_row_index)` identity and explicit
  loan-ID uniqueness constraints rather than assuming every lender ID is unique.
- Serve through authenticated API Gateway/Lambda with tenant-scoped authorisation and
  PostgreSQL row-level security. API/WAF controls do not replace database authorisation.

Parquet/Athena can provide historical investigation separately from the serving database.
Object/partition compaction limits small-file costs. Aurora Serverless v2 auto-pause needs
supported engine versions and introduces resume latency; keep capacity warm if the API's
latency objective requires it. Cross-AZ storage alone is not a tested writer-failover plan.

## 6. Security, privacy and compliance

### Implemented controls

The sample uses versioned, private, TLS-only S3 buckets; KMS encryption; scoped worker
permissions; encrypted failure/alert channels with service-principal grants; bounded
execution and retained production operational evidence. See [infra/README.md](../infra/README.md)
for the exact resources and input contract. Data and operational resources are not
publicly exposed merely because the Lambda is outside a customer VPC.

Versioning and stack `RETAIN` do **not** provide S3 Object Lock/WORM. Object Lock is not
claimed as implemented. KMS protects wrapping keys; envelope encryption also uses data
keys outside KMS. S3 Bucket Keys reduce KMS requests, not eliminate or make them universally
constant. Financial outputs remain sensitive even after demographic minimisation.

### Production controls to establish

- Separate production, non-production and security/log-archive responsibilities, with
  least-privilege deployment roles, short-lived OIDC credentials and constrained issuer,
  audience, repository and environment claims.
- Tenant-aware upload policies and authorisation at every serving boundary; protect
  against one tenant exhausting shared capacity or reading another tenant's diagnostics.
- Private database subnets and deliberately restricted workload egress. Enumerate all
  necessary endpoints, including logs, secrets and run-state services, not just S3/ECR.
- CloudTrail data events, access reviews, alert-delivery monitoring, incident ownership,
  vulnerability/image scanning and dependency-update review. Macie can assist discovery,
  but it is not a complete privacy enforcement layer.
- Validate file types, size and decompression bounds; treat spreadsheet output as untrusted
  text and avoid executable formulas in exported identifiers or error messages.

### GDPR

Loan identifiers, repayment histories and credit events can remain personal data. A hash
or stable token is **pseudonymisation, not anonymisation**. Sanitising direct demographics
reduces exposure but does not remove GDPR obligations from results, logs or backups.

A production data inventory should define controller/processor roles, lawful purpose,
recipients, retention and access for raw versions, diagnostic artifacts, accepted results,
serving tables, exports, logs and backups. A separate demographic vault may reduce access,
but deleting its entry does not erase the original identifiable raw tape.

Subject-request handling needs a lineage index across those stores, auditable deletion
or justified retention exceptions, and deletion records reapplied after a backup restore.
Any Object Lock mode, legal hold or statutory retention must be reconciled with that
policy before being enabled. A lender contract alone is not proof that indefinite
retention is lawful. Cross-region backup choices must respect data-residency requirements.

The detector does not itself approve or deny credit. Whether downstream scoring invokes
Article 22 obligations depends on the actual decision process and human involvement;
calling the output a data-quality result does not settle that question.

### ISO 27001 readiness

This repository is not a certification claim. Relevant evidence includes a maintained
risk register, control ownership/Statement of Applicability, access reviews, supplier
assessment, secure change approvals, vulnerability remediation, incident exercises and
restore-test records. IaC and tests support these processes; they do not replace them.

## 7. Rollback and disaster recovery

**Release:** lock dependencies, build and test one target-platform artifact, retain its
image digest and configuration, then promote those bytes. Shadow-replay a new ruleset
against adjudicated historical tapes; inspect changed findings before gradual rollout.
A canary should monitor output coverage and unexpected verdict changes as well as errors.
Alias/CodeDeploy promotion is a production extension, not an implemented deployment here.

**Data rollback:** reverting the Lambda does not retract already served results. Retain
versioned output and an approved-run pointer. A rollback withdraws the incorrect version,
restores the previous approved pointer and notifies downstream consumers of supersession.
Replays preserve original source version, snapshot and compatible configuration. Schema
migrations need expand/contract compatibility with both old and new consumers.

**Recovery:** agree RPO/RTO from business needs before choosing multi-region complexity.
Enable and test database point-in-time recovery, run-state backups and recovery of required
S3 versions. Ensure retained encrypted data remains decryptable after restore. Replaying
Glacier objects requires restore/wait handling; lifecycle savings increase recovery latency.
Exercise lost run-state, expired leases, key unavailability and downstream database loss.

## 8. Monitoring and cost

Monitor input-to-decision latency, duplicate events, lease contention, oldest unresolved
run, accepted/review/rejected counts, quarantine/coverage rates, DLQ age and delivery
failures. Track technical error rates separately from borrower credit events. An absence
of metrics is not proof of health; use a scheduled end-to-end synthetic check in production.

Budget from workload and region rather than quoting a universal monthly total. Include
compute duration, DynamoDB requests, versioned storage, KMS, image storage, logs, network
endpoint/NAT hours, backups, security services and database availability. The small sample
avoids provisioning a database or permanent orchestration fleet. Fargate Spot is suitable
only for restartable work whose interruption cost is understood.

## 9. Verification boundary

Local tests cover source versions, duplicate events, warm invocation isolation, partial
publication, review states and permission/resource assertions. CI also builds and smoke-tests
the target image. `cdk synth` validates generation of the assembly, not Docker execution
or live IAM behaviour. Before production, rehearse failure delivery, authorised replay,
tenant isolation, rollback and restore in a non-production AWS account.

AWS references: [workflow types](https://docs.aws.amazon.com/step-functions/latest/dg/choosing-workflow-type.html),
[Distributed Map](https://docs.aws.amazon.com/step-functions/latest/dg/state-map-distributed.html),
[EventBridge failure queues](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-dlq.html),
[Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html).
