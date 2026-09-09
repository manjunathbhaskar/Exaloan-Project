"""Tape health: what is wrong with the *file and the export*, independent of any one loan.

Per-loan rules answer "is this loan anomalous?". This module answers "can this tape be
trusted as a data contract?" - column fill, dead fields, header typos, mixed date formats,
the nested diary and how much of it Excel cut off, vocabulary that escaped translation.
Every number is measured from the frame that was read; nothing is asserted from memory.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field

import pandas as pd

from loan_dq.config import Config
from loan_dq.ingest.schema import IngestResult
from loan_dq.report.schema import PUBLIC_CATEGORIES, LoanResult, verdict_tiers

MOSTLY_EMPTY_SHARE = 0.80
LOW_INFORMATION_SHARE = 0.95
LOW_INFORMATION_MIN_ROWS = 10
VALUE_COUNTS_TOP = 50
UNTRANSLATED_CODE = re.compile(r"^[a-z_]+\.\d+$")
DIARY_DATE = re.compile(r"[\"'](?:Payment|Repayment) date[\"']\s*:\s*[\"']([^\"']*)[\"']")
PUBLIC_NUMERIC_COUNTS = frozenset({"Days late"})
PUBLIC_COUNT_FIELDS = frozenset(PUBLIC_CATEGORIES) | PUBLIC_NUMERIC_COUNTS
LOAN_DATE_COLUMNS = ("Disbursal date", "Expected repayment date", "Repayment date")
DECIMAL_EXPECTED_COLUMNS = (
    "Loan amount",
    "Interest rate",
    "Monthly payment",
    "Outstanding principal",
    "Repaid principal",
    "Outstanding interest",
    "Repaid interest",
)


@dataclass
class SchemaHealth:
    rows: int
    columns: int
    required_columns_missing: list[str]
    optional_columns_missing: list[str]
    columns_not_in_data_dictionary: list[str]
    quarantined_rows: int
    export_timestamp_supplied: bool
    as_of_source: str
    header_words_with_transposed_letters: list[list[str]]


@dataclass
class LowInformationColumn:
    dominant_value: str
    rows_with_it: int
    rows_filled: int
    share: float


@dataclass
class ColumnHealth:
    columns_100pct_empty: list[str]
    columns_mostly_empty: dict[str, int]
    columns_mostly_empty_threshold: float
    low_information_columns: dict[str, LowInformationColumn]
    decimal_columns_stored_as_integers: list[str]
    value_counts_omitted: list[str] = field(default_factory=list)
    value_counts_scope: str = "exact for retained allowlisted fields; other values not profiled"


@dataclass
class DateFormats:
    loan_level: dict[str, int]
    payments_diary: dict[str, int]
    distinct_formats_in_file: int
    single_format: bool


@dataclass
class DiaryHealth:
    stored_as: str
    records_parsed: int
    max_cell_chars: int
    excel_cell_limit: int
    cells_at_excel_limit: int
    diaries_truncated: int
    diaries_truncated_share: float
    truncated_loan_ids: list[int]
    dropped_tail_chars_total: int  # chars of the half-written last record; the cut tail is unknown
    diaries_unreadable: int
    loans_with_record_of_another_loan: int


@dataclass
class VocabularyHealth:
    untranslated_codes: dict[str, dict[str, int]]
    categorical_values_outside_dictionary: dict[str, dict[str, int]]
    free_text_categories: dict[str, dict[str, int]]


@dataclass
class TapeHealth:
    verdict_tiers: dict[str, object]
    schema: SchemaHealth
    columns: ColumnHealth
    dates: DateFormats
    payments_diary: DiaryHealth
    vocabulary: VocabularyHealth

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def lines(self) -> list[str]:
        """One console line per export-level fact worth a reviewer's attention."""
        s, c, d, p, v = self.schema, self.columns, self.dates, self.payments_diary, self.vocabulary
        out = [
            f"schema: {s.rows} rows x {s.columns} columns; "
            f"{len(c.columns_100pct_empty)} columns 100% empty, "
            f"{len(c.columns_mostly_empty)} columns >= {c.columns_mostly_empty_threshold:.0%} "
            f"empty; {len(s.columns_not_in_data_dictionary)} outside the pipeline's dictionary "
            "(carried through, not validated)"
        ]
        if not s.export_timestamp_supplied:
            out.append(
                f"as-of: no export timestamp in the file; as-of date {s.as_of_source} - every "
                "'overdue today' check depends on this inference"
            )
        if s.header_words_with_transposed_letters:
            out.append(
                "headers: words spelled with the same letters in different order: "
                + ", ".join("/".join(g) for g in s.header_words_with_transposed_letters)
            )
        for name, info in c.low_information_columns.items():
            out.append(
                f"low-information column: {name!r} = {info.dominant_value!r} on "
                f"{info.rows_with_it} of {info.rows_filled} rows ({info.share:.0%})"
            )
        if c.decimal_columns_stored_as_integers:
            out.append(
                "decimal fields stored as whole numbers: "
                + ", ".join(c.decimal_columns_stored_as_integers)
            )
        if not d.single_format:
            out.append(
                f"dates: {d.distinct_formats_in_file} formats in one file - loan level "
                f"{d.loan_level}, diary {d.payments_diary}"
            )
        out.append(
            f"payments diary: {p.stored_as}; {p.records_parsed} records parsed; "
            f"{p.diaries_truncated} diaries ({p.diaries_truncated_share:.0%}) cut at the "
            f"{p.excel_cell_limit}-char Excel cell limit ({p.dropped_tail_chars_total} chars of "
            f"half-written last records discarded; the rest never reached the file); "
            f"{p.diaries_unreadable} unreadable"
        )
        if v.untranslated_codes:
            out.append(
                "vocabulary: untranslated codes "
                + "; ".join(f"{col} {codes}" for col, codes in v.untranslated_codes.items())
            )
        if v.categorical_values_outside_dictionary:
            out.append(
                "vocabulary: values outside the dictionary "
                + "; ".join(
                    f"{col} {vals}" for col, vals in v.categorical_values_outside_dictionary.items()
                )
            )
        return out


@dataclass
class HealthPartial:
    """Everything ``TapeHealth`` needs, measured on one row range of the tape.

    Adding the partials of every shard (``combine``) and finishing once (``finish``) yields the
    same ``TapeHealth`` a single pass over the whole tape produces, with one bounded
    approximation: ``value_counts`` keeps only the ``VALUE_COUNTS_TOP`` most frequent values of
    a column per shard. Only the dominant value matters downstream (``column_health``), and a
    value can fall out of a shard's top list only while it fills under 1/(TOP+1) ~ 2% of that
    shard's rows, so a tape-wide share of >= 95% is understated by at most that much.
    """

    rows: int
    columns: list[str]
    required_columns_missing: list[str]
    optional_columns_missing: list[str]
    columns_not_in_data_dictionary: list[str]
    quarantined_rows: int
    loans: int
    filled: dict[str, int]
    value_counts: dict[str, dict[str, int]]
    numeric_seen: dict[str, bool]
    integer_valued: dict[str, bool]
    loan_dates: dict[str, int]
    diary_dates: dict[str, int]
    diary_records: int
    diary_max_cell_chars: int
    diary_cells_at_limit: int
    diary_truncated_loan_ids: list[int]
    diary_dropped_tail_chars: int
    diary_unreadable: int
    diary_record_of_another_loan: int
    untranslated: dict[str, dict[str, int]]
    outside_dictionary: dict[str, dict[str, int]]
    free_text: dict[str, dict[str, int]]
    value_counts_omitted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["value_counts"] = {
            c: counts for c, counts in self.value_counts.items() if c in PUBLIC_COUNT_FIELDS
        }
        d["free_text"] = {}
        d["value_counts_accuracy"] = "exact for retained allowlisted fields; others omitted"
        return d

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> HealthPartial:
        if set(d) != set(cls.__dataclass_fields__) | {"value_counts_accuracy"}:
            raise ValueError("unsupported health partial schema")

        def integer(v: object) -> int:
            if type(v) is not int or v < 0:
                raise ValueError("invalid health counter")
            return v

        def counts(v: object) -> dict[str, int]:
            if not isinstance(v, dict) or any(not isinstance(k, str) for k in v):
                raise ValueError("invalid health counter mapping")
            return {k: integer(n) for k, n in v.items()}

        def nested(v: object) -> dict[str, dict[str, int]]:
            assert isinstance(v, dict)
            return {str(k): counts(inner) for k, inner in v.items()}

        def strings(v: object) -> list[str]:
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                raise ValueError("invalid health field names")
            return list(v)

        def flags(v: object) -> dict[str, bool]:
            if not isinstance(v, dict) or any(type(x) is not bool for x in v.values()):
                raise ValueError("invalid health flags")
            return dict(v)

        truncated = d["diary_truncated_loan_ids"]
        if not isinstance(truncated, list) or any(type(x) is not int for x in truncated):
            raise ValueError("invalid truncated loan identifiers")
        for name in (
            "rows",
            "quarantined_rows",
            "loans",
            "diary_records",
            "diary_max_cell_chars",
            "diary_cells_at_limit",
            "diary_dropped_tail_chars",
            "diary_unreadable",
            "diary_record_of_another_loan",
        ):
            integer(d[name])
        part = cls(
            rows=integer(d["rows"]),
            columns=strings(d["columns"]),
            required_columns_missing=strings(d["required_columns_missing"]),
            optional_columns_missing=strings(d["optional_columns_missing"]),
            columns_not_in_data_dictionary=strings(d["columns_not_in_data_dictionary"]),
            quarantined_rows=int(str(d["quarantined_rows"])),
            loans=int(str(d["loans"])),
            filled=counts(d["filled"]),
            value_counts=nested(d["value_counts"]),
            numeric_seen=flags(d["numeric_seen"]),
            integer_valued=flags(d["integer_valued"]),
            loan_dates=counts(d["loan_dates"]),
            diary_dates=counts(d["diary_dates"]),
            diary_records=int(str(d["diary_records"])),
            diary_max_cell_chars=int(str(d["diary_max_cell_chars"])),
            diary_cells_at_limit=int(str(d["diary_cells_at_limit"])),
            diary_truncated_loan_ids=list(truncated),
            diary_dropped_tail_chars=int(str(d["diary_dropped_tail_chars"])),
            diary_unreadable=int(str(d["diary_unreadable"])),
            diary_record_of_another_loan=int(str(d["diary_record_of_another_loan"])),
            untranslated=nested(d["untranslated"]),
            outside_dictionary=nested(d["outside_dictionary"]),
            free_text=nested(d["free_text"]),
            value_counts_omitted=strings(d["value_counts_omitted"]),
        )
        part.validate()
        return part

    def validate(self) -> None:
        if (
            self.rows != self.loans + self.quarantined_rows
            or len(set(self.columns)) != len(self.columns)
            or set(self.filled) != set(self.columns)
            or any(n > self.rows for n in self.filled.values())
            or self.diary_unreadable > self.loans
            or self.diary_record_of_another_loan > self.loans
            or len(self.diary_truncated_loan_ids) > self.loans
            or self.diary_cells_at_limit > self.rows
            or self.free_text
            or set(self.value_counts) - PUBLIC_COUNT_FIELDS
            or set(self.value_counts_omitted) - PUBLIC_COUNT_FIELDS
            or set(self.value_counts_omitted) & set(self.value_counts)
            or set(self.numeric_seen) - set(DECIMAL_EXPECTED_COLUMNS)
            or set(self.integer_valued) != set(self.numeric_seen)
        ):
            raise ValueError("inconsistent or unsafe health partial")
        for column, counts in self.value_counts.items():
            if len(counts) > VALUE_COUNTS_TOP or sum(counts.values()) > self.filled.get(column, 0):
                raise ValueError("invalid health value counts")
            for value in counts:
                if column in PUBLIC_NUMERIC_COUNTS:
                    if re.fullmatch(r"-?[0-9]{1,7}", value) is None or abs(int(value)) > 1_000_000:
                        raise ValueError("unsafe numeric health value")
                elif value not in PUBLIC_CATEGORIES[column]:
                    raise ValueError("unsafe categorical health value")
        for counts_by_field in (self.untranslated, self.outside_dictionary):
            for column, counts in counts_by_field.items():
                if set(counts) != {"rows"} or counts["rows"] > self.filled.get(column, 0):
                    raise ValueError("unsafe vocabulary counters")
        shapes = {
            "YYYY-MM-DD",
            "YYYY-MM-DDThh:mm:ss",
            "YYYY-MM-DDThh:mm:ss.sss",
            "DD/MM/YYYY",
            "DD.MM.YYYY",
            "unrecognized",
        }
        if (set(self.loan_dates) | set(self.diary_dates)) - shapes:
            raise ValueError("unsafe date format labels")


def health_partial(frame: pd.DataFrame, ingested: IngestResult, config: Config) -> HealthPartial:
    """Measure one row range. Column lists come from the header, counters from the rows."""
    loans = ingested.loans
    frame = frame.mask(frame.map(lambda v: isinstance(v, str) and not v.strip()))
    filled = frame.notna().sum()
    value_counts: dict[str, dict[str, int]] = {}
    omitted: list[str] = []
    numeric_seen: dict[str, bool] = {}
    integer_valued: dict[str, bool] = {}
    untranslated: dict[str, dict[str, int]] = {}
    for c in frame.columns:
        name = str(c)
        series = frame[c].dropna()
        if name == "payments":
            continue
        if name in PUBLIC_COUNT_FIELDS:
            if name in PUBLIC_NUMERIC_COUNTS:
                numeric = pd.to_numeric(series, errors="coerce")
                approved = numeric[numeric.notna() & numeric.between(-1_000_000, 1_000_000)]
                approved = approved[approved == approved.round()].map(lambda n: str(int(n)))
            else:
                approved = series.astype(str).str.strip().str.lower()
                approved = approved[approved.isin(PUBLIC_CATEGORIES[name])]
            counts = Counter(approved)
            if len(counts) <= VALUE_COUNTS_TOP:
                value_counts[name] = dict(_top(counts))
            else:
                omitted.append(name)
        hits = sum(bool(UNTRANSLATED_CODE.match(str(v).strip().lower())) for v in series)
        if hits:
            untranslated[name] = {"rows": hits}
        if name in DECIMAL_EXPECTED_COLUMNS:
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            numeric_seen[name] = not numeric.empty
            integer_valued[name] = bool((numeric == numeric.round()).all())

    loan_dates: Counter[str] = Counter()
    for c in LOAN_DATE_COLUMNS:
        if c in frame.columns:
            for v in frame[c].dropna():
                loan_dates[_shape(str(v))] += 1
    diary_dates: Counter[str] = Counter()
    lengths = pd.Series(dtype=int)
    if "payments" in frame.columns:
        cells = frame["payments"].dropna().astype(str)
        lengths = cells.str.len()
        for cell in cells:
            for raw in DIARY_DATE.findall(cell):
                if raw.strip():
                    diary_dates[_shape(raw)] += 1

    outside: dict[str, dict[str, int]] = {}
    for column, allowed_values in _categorical_dictionary(config).items():
        if column not in frame.columns:
            continue
        allowed = {v.lower() for v in allowed_values}
        bad = sum(str(v).strip().lower() not in allowed for v in frame[column].dropna())
        if bad:
            outside[column] = {"rows": bad}
    free_text: dict[str, dict[str, int]] = {}

    truncated = [loan for loan in loans if loan.diary.truncated]
    return HealthPartial(
        rows=len(frame),
        columns=[str(c) for c in frame.columns],
        required_columns_missing=list(ingested.schema.required_missing),
        optional_columns_missing=list(ingested.schema.optional_missing),
        columns_not_in_data_dictionary=list(ingested.schema.unexpected),
        quarantined_rows=len(ingested.quarantined),
        loans=len(loans),
        filled={str(c): int(filled[c]) for c in frame.columns},
        value_counts=value_counts,
        numeric_seen=numeric_seen,
        integer_valued=integer_valued,
        loan_dates=dict(loan_dates),
        diary_dates=dict(diary_dates),
        diary_records=sum(len(loan.diary.payments) for loan in loans),
        diary_max_cell_chars=int(lengths.max()) if not lengths.empty else 0,
        diary_cells_at_limit=int((lengths >= config.tape.excel_cell_limit).sum()),
        diary_truncated_loan_ids=sorted(
            loan.loan_id for loan in truncated if loan.loan_id is not None
        ),
        diary_dropped_tail_chars=sum(loan.diary.dropped_tail_chars for loan in truncated),
        diary_unreadable=sum(1 for loan in loans if not loan.diary.readable),
        diary_record_of_another_loan=sum(
            1
            for loan in loans
            if any(p.loan_id_raw not in (None, str(loan.loan_id)) for p in loan.diary.payments)
        ),
        untranslated=untranslated,
        outside_dictionary=outside,
        free_text=free_text,
        value_counts_omitted=omitted,
    )


def combine(parts: list[HealthPartial]) -> HealthPartial:
    """Sum shard partials. Header-derived lists are taken from the first shard (all shards read
    the same header); every counter is added."""
    if not parts:
        raise ValueError("nothing to combine")
    head = parts[0]
    omitted = {c for p in parts for c in p.value_counts_omitted}
    value_counts = {}
    for c in sorted(PUBLIC_COUNT_FIELDS & set(head.columns)):
        counts = _sum_counts([p.value_counts.get(c, {}) for p in parts])
        if len(counts) > VALUE_COUNTS_TOP:
            omitted.add(c)
        if c not in omitted:
            value_counts[c] = dict(_top(Counter(counts)))
    numeric_seen = {c: any(p.numeric_seen.get(c, False) for p in parts) for c in head.numeric_seen}
    integer_valued = {
        c: all(p.integer_valued.get(c, True) for p in parts if p.numeric_seen.get(c, False))
        for c in head.numeric_seen
    }
    return HealthPartial(
        rows=sum(p.rows for p in parts),
        columns=list(head.columns),
        required_columns_missing=list(head.required_columns_missing),
        optional_columns_missing=list(head.optional_columns_missing),
        columns_not_in_data_dictionary=list(head.columns_not_in_data_dictionary),
        quarantined_rows=sum(p.quarantined_rows for p in parts),
        loans=sum(p.loans for p in parts),
        filled={c: sum(p.filled.get(c, 0) for p in parts) for c in head.columns},
        value_counts=value_counts,
        numeric_seen=numeric_seen,
        integer_valued=integer_valued,
        loan_dates=_sum_counts([p.loan_dates for p in parts]),
        diary_dates=_sum_counts([p.diary_dates for p in parts]),
        diary_records=sum(p.diary_records for p in parts),
        diary_max_cell_chars=max(p.diary_max_cell_chars for p in parts),
        diary_cells_at_limit=sum(p.diary_cells_at_limit for p in parts),
        diary_truncated_loan_ids=sorted(
            loan_id for p in parts for loan_id in p.diary_truncated_loan_ids
        ),
        diary_dropped_tail_chars=sum(p.diary_dropped_tail_chars for p in parts),
        diary_unreadable=sum(p.diary_unreadable for p in parts),
        diary_record_of_another_loan=sum(p.diary_record_of_another_loan for p in parts),
        untranslated=_sum_nested([p.untranslated for p in parts]),
        outside_dictionary=_sum_nested([p.outside_dictionary for p in parts]),
        free_text={},
        value_counts_omitted=sorted(omitted),
    )


def _top(counts: Counter[str]) -> list[tuple[str, int]]:
    """The ``VALUE_COUNTS_TOP`` most frequent values, ties broken by value for determinism."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:VALUE_COUNTS_TOP]


def _sum_counts(items: list[dict[str, int]]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for item in items:
        total.update(item)
    return dict(total)


def _sum_nested(items: list[dict[str, dict[str, int]]]) -> dict[str, dict[str, int]]:
    keys = sorted({k for item in items for k in item})
    return {k: _sum_counts([item[k] for item in items if k in item]) for k in keys}


def tape_health(
    frame: pd.DataFrame,
    ingested: IngestResult,
    results: list[LoanResult],
    as_of_source: str,
    config: Config,
) -> TapeHealth:
    return finish(health_partial(frame, ingested, config), results, as_of_source, config)


def finish(
    part: HealthPartial, results: list[LoanResult], as_of_source: str, config: Config
) -> TapeHealth:
    return TapeHealth(
        verdict_tiers={"loans": len(results), **verdict_tiers(results)},
        schema=schema_health(part, as_of_source),
        columns=column_health(part),
        dates=date_formats(part),
        payments_diary=diary_health(part, config),
        vocabulary=VocabularyHealth(
            untranslated_codes=part.untranslated,
            categorical_values_outside_dictionary=part.outside_dictionary,
            free_text_categories=part.free_text,
        ),
    )


def schema_health(part: HealthPartial, as_of_source: str) -> SchemaHealth:
    return SchemaHealth(
        rows=part.rows,
        columns=len(part.columns),
        required_columns_missing=list(part.required_columns_missing),
        optional_columns_missing=list(part.optional_columns_missing),
        columns_not_in_data_dictionary=list(part.columns_not_in_data_dictionary),
        quarantined_rows=part.quarantined_rows,
        export_timestamp_supplied=not as_of_source.startswith("inferred"),
        as_of_source=as_of_source,
        header_words_with_transposed_letters=transposed_header_words(part.columns),
    )


def transposed_header_words(columns: list[str]) -> list[list[str]]:
    """Two header words made of the same letters in a different order (Collaretal/Collateral)."""
    words = sorted({w.lower() for c in columns for w in re.findall(r"[A-Za-z]+", str(c))})
    by_letters: dict[str, list[str]] = {}
    for w in words:
        if len(w) >= 5:
            by_letters.setdefault("".join(sorted(w)), []).append(w)
    return [group for group in by_letters.values() if len(group) > 1]


def column_health(part: HealthPartial) -> ColumnHealth:
    n = part.rows
    low_information: dict[str, LowInformationColumn] = {}
    for c in part.columns:
        counts = part.value_counts.get(c)
        rows_filled = part.filled.get(c, 0)
        if c == "payments" or not counts or rows_filled < LOW_INFORMATION_MIN_ROWS:
            continue
        value, rows_with_it = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        share = rows_with_it / rows_filled
        if share >= LOW_INFORMATION_SHARE:
            low_information[c] = LowInformationColumn(
                dominant_value=value,
                rows_with_it=rows_with_it,
                rows_filled=rows_filled,
                share=round(share, 3),
            )
    return ColumnHealth(
        columns_100pct_empty=[c for c in part.columns if part.filled.get(c, 0) == 0],
        columns_mostly_empty={
            c: part.filled[c]
            for c in part.columns
            if 0 < part.filled.get(c, 0) <= (1 - MOSTLY_EMPTY_SHARE) * n
        },
        columns_mostly_empty_threshold=MOSTLY_EMPTY_SHARE,
        low_information_columns=low_information,
        value_counts_omitted=list(part.value_counts_omitted),
        decimal_columns_stored_as_integers=[
            c
            for c in DECIMAL_EXPECTED_COLUMNS
            if part.numeric_seen.get(c, False) and part.integer_valued.get(c, False)
        ],
    )


def date_formats(part: HealthPartial) -> DateFormats:
    loan_level = Counter(part.loan_dates)
    diary = Counter(part.diary_dates)
    distinct = set(loan_level) | set(diary)
    return DateFormats(
        loan_level=dict(loan_level.most_common()),
        payments_diary=dict(diary.most_common()),
        distinct_formats_in_file=len(distinct),
        single_format=len(distinct) <= 1,
    )


def _shape(value: str) -> str:
    """'2022-05-12T00:00:00.000' -> 'YYYY-MM-DDThh:mm:ss.sss', '26/04/2024' -> 'DD/MM/YYYY'."""
    iso = re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?)?$", value)
    if iso:
        time_part = ("Thh:mm:ss" + (".sss" if iso.group(2) else "")) if iso.group(1) else ""
        return "YYYY-MM-DD" + time_part
    if re.match(r"^\d{2}/\d{2}/\d{4}$", value):
        return "DD/MM/YYYY"
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", value):
        return "DD.MM.YYYY"
    return "unrecognized"


def diary_health(part: HealthPartial, config: Config) -> DiaryHealth:
    n = part.loans or 1
    truncated = len(part.diary_truncated_loan_ids)
    return DiaryHealth(
        stored_as="one text cell per loan holding a list of records (one-to-many in one cell)",
        records_parsed=part.diary_records,
        max_cell_chars=part.diary_max_cell_chars,
        excel_cell_limit=config.tape.excel_cell_limit,
        cells_at_excel_limit=part.diary_cells_at_limit,
        diaries_truncated=truncated,
        diaries_truncated_share=round(truncated / n, 3),
        truncated_loan_ids=list(part.diary_truncated_loan_ids),
        dropped_tail_chars_total=part.diary_dropped_tail_chars,
        diaries_unreadable=part.diary_unreadable,
        loans_with_record_of_another_loan=part.diary_record_of_another_loan,
    )


def _categorical_dictionary(config: Config) -> dict[str, list[str]]:
    schema = config.schema_
    return {
        "Loan status": schema.loan_status_values,
        "Loan type": schema.loan_type_values,
        "Borrower type": schema.borrower_type_values,
        "Credit score": schema.credit_score_values,
    }
