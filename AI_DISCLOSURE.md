# AI disclosure

I built this system myself. I used an AI coding assistant strictly as a typing aid for
boilerplate; I drove every architectural and business decision, and I retain full
responsibility for how the system behaves.

## Part 1 — detector

**Mine.** The approach (deterministic accounting and date rules rather than a trained
model, because 72 loans and three labels can neither train nor validate one), the two-axis
result (data integrity separate from credit events), the nine rule families and every
threshold in `config/default.yaml`, the XIRR screen and its guards, the handling of
Excel-truncated diaries as *indeterminate* rather than clean, the rule-admission gate
against the two known-clean loans and the seeded loan, and the final reading of every
flagged loan against the raw diary. No LLM touches loan data at runtime; every verdict is
produced by a named rule that prints the two values that disagree.

**AI-assisted.** Package layout, `pyproject.toml`, CLI argument parsing, dataclass and
writer scaffolding, and the first skeleton of tests after I had specified the invariant
each test must pin. I reviewed, and in most cases rewrote, every generated line before it
stayed.

## Part 2 — AWS

**Mine.** The service choices and their trade-offs (EventBridge over a direct S3 trigger,
DynamoDB conditional writes for run ownership, Standard Step Functions and Fargate for
large tapes, Aurora for serving, Athena for history), the publication protocol — claim,
evaluate, stage, decide, consume — the failure-handling table, the GDPR and retention
position, the CI gates (locked dependencies, coverage floor, admission script, dual-stage
`cdk synth`, image smoke test without publishing) and the explicit line between what is
implemented and what is design.

**AI-assisted.** CDK construct boilerplate, Dockerfile and Lambda handler skeleton, and the
Bitbucket pipeline YAML scaffold, each written to the structure I dictated step by step.

## Verification I did personally

Ran the full test suite, lint, type-check and `cdk synth` on the final revision; read every
flagged and every minor-observation loan in the console report against its diary; checked
that the two clean anchors stay normal and the seeded loan stays critical; confirmed the
shard-and-merge output matches the single-process output; and reviewed the working tree so
that no assistant-specific configuration or scratch files are carried into the submission.

## Governance rules I set, not the tool

- Never tune to a target anomaly percentage.
- Keep credit events separate from data contradictions.
- Keep contract-dependent findings as review observations, not hard verdicts.
- Never let the system modify a financial record automatically.
- No AI in the runtime path; a future assistant may only *propose* header mappings or
  triage notes, which a human approves and which can never change a verdict.
