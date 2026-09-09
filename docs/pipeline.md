# Part 1: detection and evidence

The [README](../README.md) is the presentation guide. This reference explains the
implementation boundaries and the assumptions to challenge when onboarding another lender.

## 1. Follow one loan through the code

![Part A detector: six stages and three check groups](pipeline.png)

[Editable Mermaid source](pipeline.mmd) · [Scalable SVG](pipeline.svg)

The diagram shows the normal run and its input/coverage guardrails. Inspect and the
per-loan decision repeat for each loan; the whole-tape group runs after those results
are collected. Admission and development tests are separate from the runtime stages.
Colours distinguish responsibilities, not risk severity. Fatal input errors can stop
before report generation; console output is captured separately from the four default files.

<details>
<summary>Regenerate the Part A diagram</summary>

Run from the repository root; Mermaid CLI requires Chromium:

```bash
npx --yes @mermaid-js/mermaid-cli@11.17.0 -i docs/pipeline.mmd -o docs/pipeline.svg -b white -w 2000
npx --yes @mermaid-js/mermaid-cli@11.17.0 -i docs/pipeline.mmd -o docs/pipeline.png -b white -w 2000 -s 2
```

</details>

```text
File → raw table → schema validation → typed Loan + Payment objects
     → snapshot date → rule outcomes → evidence and loan classification
     → whole-tape checks → publication eligibility → reports
```

| Layer | Code | Responsibility |
|---|---|---|
| Transport | `src/loan_dq/io/reader.py` | Read supported file formats; reject unusable files and ambiguous headers. |
| Normalisation | `src/loan_dq/ingest/normalize.py` | Convert dates, numbers and identifiers without silently accepting unsupported encodings. |
| Ingestion | `src/loan_dq/ingest/schema.py`, `diary.py`, `model.py` | Check columns and required values, parse the nested diary, preserve row identity and quarantine unusable rows. |
| Rules | `src/loan_dq/rules/` | Evaluate explicit invariants and policy thresholds over typed data. |
| Application | `src/loan_dq/engine.py` | Coordinate rules, classify findings and record evidence/coverage. |
| Reporting | `src/loan_dq/report/` | Emit the loan report, payment child table and coverage table; minimise sensitive output. |
| Optional processing | `plugins.py`, `shard.py` | Isolate annotation plugins; validate and combine shard results. |
| Entry point | `cli.py` | Load configuration, run commands and return meaningful exit statuses. |

These are practical module boundaries, not a claim that every module is a perfectly
independent clean-architecture layer. The financial logic does not call AWS services.

## 2. Readability is not completeness

The workbook holds each loan's payment history as a Python-literal list in one cell.
Thirteen cells were cut at Excel's 32,767-character limit. The parser keeps complete
records before the cut; the missing tail cannot be recovered from this workbook.

Keep three questions separate:

1. **Can the file and row be parsed?** Invalid identifiers can quarantine a row without
   preventing other rows from being evaluated.
2. **Is the evidence sufficient for this check?** A missing rate or truncated ledger is
   not equivalent to a successful financial check.
3. **Can the resulting tape be published?** A technically successful evaluation can still
   require review because coverage is incomplete or rows were quarantined.

A rule returns `pass`, `fail`, or `not_evaluable`. A non-applicable rule is different from
missing required evidence. Coverage and required-value validation prevent skipped checks
from automatically establishing a clean record. Missing optional demographic context stays
`not_evaluable` without invalidating an otherwise evaluable financial ledger. An empty
usable population is not success.

## 3. Establish a reproducible time reference

Local runs can infer the snapshot from actual-payment and loan event dates, excluding
future scheduled instalments. The supplied tape infers **2025-01-23**. This is an
assumption, not a lender-provided export timestamp. Use `--as-of` to pin it; if no usable
reference can be inferred, an explicit date is required rather than today's clock.

Production ingestion requires an explicit snapshot date. Upload time is not a substitute:
an old tape delivered today still describes its original reporting period. A future-dated
actual payment can contaminate inference, so production should use the lender's data contract.

## 4. Three inspection groups

The groups describe what a check asks, not when processing happens. Read, ingest, anchor,
evaluate, judge and report remain separate responsibilities. Rules still live in small,
focused files; the original categories remain useful implementation detail, not additional
presentation groups. There are **50 loan rules (43 + 7)** and **six whole-file checks**.

### Record consistency (`record_consistency`)

**Question:** do the loan's fields, amounts, dates and status tell a consistent, plausible
story? This group combines readability (A), balance identity/reconciliation (B), arithmetic
(C except C8), temporal (D), lifecycle (E) and plausibility (H): **43 loan rules**.

Each loan describes itself twice: summary balances and component-level payment rows.
Compare those accounts together, rather than separating money from dates and status:

- **Usable evidence:** required fields, unreadable records and a truncated diary remain
  visible through consistency rules. Ingestion parses once and preserves issues; rules
  consume that evidence, not a fresh interpretation of the raw payment cell.
- **Money and status:** a repaid loan with pending components contradicts its own ledger.
  For example, 94863476 says repaid but retains EUR 806.68 pending; 65318525 has a
  EUR 4,664.53 principal gap. Check arithmetic and accounting identities alongside the
  lifecycle claim, using configured tolerances and settlement conventions.
- **Dates and status:** ask whether dates are possible and agree with the recorded claims.
  Future scheduled instalments are legitimate; an actual payment after a pinned snapshot
  is not. A row labelled "paid on time" but paid beyond the grace window is a status/date
  contradiction here. A genuinely recorded late payment belongs to repayment behaviour;
  the same row can supply evidence for both questions without conflating them.
- **Context:** H checks can surface low-severity borrower/product observations. Unemployment
  with income or a high payment/income ratio is not proof of a hard data-integrity defect.

#### Cash and settlement assumptions

For a valid amount/pending pair, the implied paid portion is `amount - pending` and the
remaining exposure is `pending`. A label saying paid does not erase a nonzero pending
balance. Retain the contradiction; do not invent a receipt date for an undated part-payment.

The export sometimes represents an early settlement through both an aggregate event and
future schedule rows stamped with the same settlement date. Those are alternative
representations, not two independent cash receipts. Convention tags prevent double counting,
but remain lender-specific assumptions that should be confirmed against the servicing ledger.

Amounts are compared using configured tolerances. For this tape, the loan-amount header is
integer-rounded, so a EUR 0.50 tolerance avoids interpreting normal rounding as a defect.
This does not justify exempting an arbitrary principal shortfall.

#### XIRR is a screen, not a contractual verdict

XIRR solves for the annual rate that makes dated cash flows net to zero. The input cash
flows matter at least as much as the numerical solver. Fees, nominal versus effective
rates, day-count conventions, late receipts, incomplete principal and early-settlement
waivers can all create differences from the advertised rate.

The detector therefore guards evaluability, exposes confidence and links explainable
symptoms. Low-confidence early-settled deferred-annuity differences are review observations,
not confirmed major defects. A terminal outstanding balance on a live loan is a valuation
assumption, not cash actually received. Missing dated receipts must not be silently imputed.

### Repayment behaviour (`repayment_behaviour`)

**Question:** is the borrower paying late, remaining overdue or changing payment habits?
Lateness (F), behaviour (G) and the stopped-payments check (C8) form **seven loan rules**,
covering paid and unpaid delays, missed-payment streaks, worsening severity and drift from
an earlier within-grace baseline. C8 retains its legacy ID; its purpose determines its group.

A payment can be genuinely 90+ days late while every amount, date and label is internally
consistent. That is repayment evidence, not necessarily bad data. Historical late payment
and current exposure are also distinct: 37216892 has EUR 755.66 of components still
90+ days overdue, measured from pending remainders rather than gross scheduled amounts.
G8 groups components into due-date episodes so one late month is not counted three times.
Some general profile measures still use component rows; this regrouping does not change
that granularity. Repeated small delays can remain low-severity observations, and grace
periods and severity thresholds are unchanged.

### Whole-tape quality (`whole_tape`)

**Question:** do records agree across the file, and are there population-wide quality
concerns? **I1-I6 are six whole-file checks**, run on the whole population after loan
results, once after merge for a sharded run—not independently inside each shard.

Examples include duplicate loan identifiers, conflicting borrower details across loans and
a paid diary row repeated under different loans. Aggregate integrity rates and digit-pattern
screens ask questions that a single loan cannot answer. Their existing sample/evaluability
guards still apply: a small population does not establish a reliable Benford verdict, and
a high finding rate may indicate a widespread defect rather than a rule to disable.
These checks inform tape-level review; they do not replace individual loan evidence.

## 5. Interpret the report

- **Major:** evidence crosses the configured flag threshold.
- **Minor:** an observation remains visible without becoming a major flag, including
  contract-dependent review items.
- **Indeterminate:** insufficient validation coverage; absence of a finding is not a clean bill of health.
- **Clean:** no material finding within the applicable checks and available evidence; not a guarantee that every possible anomaly is absent.

The inspection contract exposes three groups: the registry groups the per-loan checks,
and the tape module supplies the whole-file checks. JSON findings and rule outcomes carry
`group` metadata; the report summary includes `check_groups` descriptors,
and the summary CSV includes `finding_groups`. Rule IDs stay unchanged so existing
rule-level evidence remains traceable. A legacy `category` detail can remain internal or
backward-compatible; it is not a fourth public group.

Grouping does not determine severity or outcome: `pass`, `fail`, `not_evaluable`, tier
assignment and coverage semantics are unchanged. The report separately records
`data_integrity`, `credit_event` and `validation_coverage`; these axes answer different
questions from inspection groups. In particular, record consistency includes contextual
H observations, not just hard integrity defects.
Technical publication status is a separate tape-level decision: `accepted`,
`review_required` or `rejected`. Acceptance does not approve a borrower or override the
per-loan findings. The local CLI retains diagnostic reports even when publication is held.

Payment rows retain their source row and sequence so duplicate loan identifiers cannot
silently become unique database keys. Summary and child tables are traceable evidence,
not independent ground-truth datasets.

## 6. Validate without chasing a flag count

The brief supplies two clean examples and one irregular example, not the complete label
set. Use these as regression anchors. Synthetic tests exercise specific failure modes,
including missing required values, contradictory pending balances and isolated corrupted
interest. Negative controls exercise legitimate rounding, early settlement and repeated
small delays. Admission reports show rule coverage and firing rates, not estimated recall.

No test should demand a percentage merely because the assignment gives an approximate
anomaly prevalence. A new rule needs a defensible invariant, positive and negative cases,
and an explanation of product assumptions. A held-out, adjudicated set is needed to measure
precision/recall. Multiple simultaneous defects and clean contractual edge cases matter as
much as single-field fault injection.

## 7. Scale and privacy boundaries

Per-loan work can be sharded; whole-tape checks run once after merge. Merge must account
for each declared input row exactly once as a result or explicit quarantine and match
provenance, configuration and the executing ruleset. Missing shards are not silently
accepted as a complete population.

This is not an end-to-end streaming implementation. Whole-file ingestion, materialised
reports, repeated source scans and central aggregation still limit very large tapes.
Production should normalise once into partitioned child tables, process physical partitions
and aggregate bounded statistics. Financial truth should not change with shard count;
approximate descriptive statistics must be identified as such.

Outputs minimise demographic and free-text content through allowlists and sanitised
errors. Internal consistency fingerprints are pseudonymous, not anonymous. Loan IDs and
payment histories remain potentially personal financial data; apply access control,
retention and erasure policies to results as well as raw inputs. An annotation plugin is
an isolated extension contract, not a sandbox for untrusted executable code.
