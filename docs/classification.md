# Classification scales — one-page reference

Five scales. Severity drives the verdict; the two axes stay separate; the tier is the
single label a reviewer reads; `publication_status` gates the whole run.

## Per finding

| Scale | Values |
|---|---|
| **Severity** | `info` · `low` · `medium` · `high` · `critical` |
| **root_cause** | `true` / `false` — `false` means it is a symptom of another failed rule; severity counts roots only |

Each rule also carries an **axis** (`integrity` · `credit` · `coverage`) and a
**check group** (`record_consistency` · `repayment_behaviour` · `whole_tape`).
A rule returns one **status**: `pass` · `fail` · `not_evaluable` — `not_evaluable`
(missing inputs, truncated diary) is never a flag.

## Per loan

| Scale | Values | Set by |
|---|---|---|
| **verdict** | `flagged` · `normal` | `flagged` when a root finding's severity ≥ the floor |
| **severity** | the 5 levels, or none | max severity of the **root** findings |
| **data_integrity** (axis 1) | `clean` · `defect` · `unknown` | `defect` = root integrity finding ≥ medium, or diary unreadable · `unknown` = coverage incomplete, nothing proven · else `clean` |
| **credit_event** (axis 2) | `none` · `watch` · `default` | `default` = F1 or F2 · `watch` = F3, C8 or G7 · else `none` |
| **validation_coverage** | `full` · `partial` · `none` | `none` = diary unreadable · `partial` = truncated or a rule errored · else `full` |
| **tier** | `major` · `minor` · `indeterminate` · `clean` | `major` if flagged → else `indeterminate` if coverage ≠ full → else `minor` if any non-info finding → else `clean` |

The floor is `verdict.flag_min_severity = medium`: `low` and `info` findings print but do
not flag. The two axes are kept apart because a loan can have clean data and a defaulting
borrower, or a bookkeeping defect and a healthy borrower.

## Per tape

| Scale | Values | Set by |
|---|---|---|
| **publication_status** | `accepted` · `review_required` · `rejected` | `rejected` = schema failed / no usable loans · `review_required` = any quarantine, incomplete coverage, circuit breaker, or high/critical tape finding · else `accepted` |
| **circuit_breaker_tripped** | `true` / `false` | > 40% of loans with a high/critical integrity defect |

## This tape

72 loans → **15 flagged, 57 normal**.
Tiers: **15 major · 10 minor · 13 indeterminate · 34 clean**.
`data_integrity`: 49 clean · 9 defect · 14 unknown.
`credit_event`: 60 none · 6 watch · 6 default.
`publication_status = review_required`.
