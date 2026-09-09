"""Ingestion: format irregularities must degrade one field or one row, never the run."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from loan_dq.config import Config
from loan_dq.engine import run
from loan_dq.ingest.diary import parse_diary
from loan_dq.ingest.normalize import parse_date, parse_float, parse_identifier, parse_int
from loan_dq.ingest.schema import ingest
from loan_dq.io.reader import TapeReadError, read_tape
from tests.conftest import SyntheticLoan, make_frame

# --- scalar parsing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-02-14T00:00:00.000", date(2024, 2, 14)),
        ("2024-02-14", date(2024, 2, 14)),
        ("2024-02-14 10:30:00", date(2024, 2, 14)),
        ("27/02/2024", date(2024, 2, 27)),
        (date(2024, 2, 14), date(2024, 2, 14)),
        (pd.Timestamp("2024-02-14"), date(2024, 2, 14)),
    ],
)
def test_parse_date_formats(raw: object, expected: date) -> None:
    assert parse_date(raw).value == expected


def test_parse_date_keeps_both_readings_when_ambiguous() -> None:
    parsed = parse_date("03/04/2024")
    assert parsed.value == date(2024, 4, 3)  # day-first preferred for this lender
    assert parsed.ambiguous
    assert set(parsed.alternatives) == {date(2024, 4, 3), date(2024, 3, 4)}


def test_parse_date_unambiguous_slash_has_single_reading() -> None:
    parsed = parse_date("27/02/2024")
    assert not parsed.ambiguous


@pytest.mark.parametrize("raw", ["", None, float("nan"), "NaT", "31/02/2024", "not a date", 12345])
def test_parse_date_garbage_is_none_not_exception(raw: object) -> None:
    parsed = parse_date(raw)
    assert parsed.value is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1365", 1365.0), (1365, 1365.0), ("1,365.50", 1365.5), ("12,5", 12.5), ("EUR 10", 10.0)],
)
def test_parse_float_accepts_common_encodings(raw: object, expected: float) -> None:
    value, error = parse_float(raw)
    assert value == expected and error is None


def test_parse_float_rejects_text() -> None:
    value, error = parse_float("twelve")
    assert value is None and error


def test_parse_int_rejects_fraction() -> None:
    assert parse_int("7.5")[0] is None
    assert parse_int("7.0")[0] == 7


@pytest.mark.parametrize("raw", ["99981632", 99981632, 99981632.0, " 99981632 "])
def test_parse_identifier_coerces_to_common_form(raw: object) -> None:
    assert parse_identifier(raw)[0] == 99981632


# --- diary parsing ------------------------------------------------------------------------------


def _record(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Loan ID": "1",
        "Payment date": "15/02/2024",
        "Repayment date": "15/02/2024",
        "Type": "principal",
        "State": "paid on time",
        "Amount": 100.0,
        "Pending amount": 0,
    }
    base.update(over)
    return base


def test_parse_diary_python_literal() -> None:
    diary = parse_diary(str([_record(), _record(Type="interest", Amount=1.5)]))
    assert diary.readable and diary.complete
    assert len(diary.payments) == 2
    assert diary.payments[0].due_date == date(2024, 2, 15)


def test_parse_diary_json_fallback() -> None:
    text = '[{"Loan ID": "1", "Payment date": "15/02/2024", "Repayment date": null, '
    text += '"Type": "principal", "State": "pending", "Amount": 100.0, "Pending amount": 100.0}]'
    diary = parse_diary(text)
    assert diary.readable
    assert diary.payments[0].actual_date is None


def test_parse_diary_unreadable_is_flagged_not_raised() -> None:
    diary = parse_diary("[{'Loan ID': '1', 'Payment date': ")
    assert not diary.readable
    assert diary.parse_error


def test_parse_diary_empty_cell_is_unreadable_not_exception() -> None:
    for raw in (None, float("nan"), ""):
        diary = parse_diary(raw)
        assert diary.payments == []
        assert not diary.readable
        assert diary.parse_error == "payments cell is empty"


def test_parse_diary_salvages_excel_truncation() -> None:
    records = [_record(Amount=float(i)) for i in range(400)]
    text = str(records)[:32767]
    diary = parse_diary(text, excel_cell_limit=32767)
    assert diary.readable
    assert diary.truncated
    assert not diary.complete
    assert diary.salvaged_records > 100
    assert diary.dropped_tail_chars > 0
    # every salvaged record is a complete one
    assert all(p.amount is not None for p in diary.payments)


def test_parse_diary_missing_keys_recorded_per_row() -> None:
    diary = parse_diary(str([{"Loan ID": "1", "Amount": 5}]))
    assert diary.readable
    assert diary.payments[0].issues


# --- frame-level ingestion ------------------------------------------------------------------------


def test_missing_required_column_reported_without_crash(config: Config) -> None:
    frame = SyntheticLoan().frame().drop(columns=["Loan amount"])
    result = ingest(frame, config)
    assert result.schema.required_missing == ["Loan amount"]
    assert not result.schema.ok
    assert result.loans == []


def test_missing_optional_column_makes_rules_not_evaluable(config: Config) -> None:
    frame = SyntheticLoan().frame().drop(columns=["Repaid interest"])
    result = ingest(frame, config)
    assert result.schema.ok
    assert "Repaid interest" in result.schema.optional_missing
    assert result.loans[0].repaid_interest is None


def test_unnamed_transport_column_is_ignored(config: Config) -> None:
    result = ingest(SyntheticLoan().frame(), config)
    assert "Unnamed: 0" not in result.schema.unexpected


def test_unreadable_row_is_quarantined_and_others_continue(config: Config) -> None:
    good = SyntheticLoan(loan_id=1)
    bad = SyntheticLoan(loan_id=2)
    bad.summary_overrides["Loan ID"] = "not-an-id"
    frame = make_frame(good, bad, SyntheticLoan(loan_id=3))
    result = ingest(frame, config)
    assert [loan.loan_id for loan in result.loans] == [1, 3]
    assert len(result.quarantined) == 1
    assert result.quarantined[0].row_index == 1
    assert "Loan ID" in result.quarantined[0].reason


def test_bad_numeric_cell_degrades_field_not_row(config: Config) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides["Loan amount"] = "one thousand"
    result = ingest(loan.frame(), config)
    assert len(result.loans) == 1
    parsed = result.loans[0]
    assert parsed.loan_amount is None
    assert any(i.field == "Loan amount" for i in parsed.parse_issues)


def test_diary_loan_id_coerced_to_parent(config: Config) -> None:
    loan = SyntheticLoan(loan_id=42)
    # Diary stores IDs as strings; parent stores an int. Must compare equal after coercion.
    result = ingest(loan.frame(), config)
    assert all(p.loan_id_raw == "42" for p in result.loans[0].diary.payments)


def test_inferred_as_of_is_latest_date_in_tape(config: Config) -> None:
    result = ingest(SyntheticLoan().frame(), config)
    assert result.inferred_as_of == date(2025, 1, 15)


# --- reader ------------------------------------------------------------------------------------


def test_reader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TapeReadError):
        read_tape(tmp_path / "nope.xlsx")


def test_reader_rejects_unknown_suffix(tmp_path: Path) -> None:
    target = tmp_path / "tape.txt"
    target.write_text("x")
    with pytest.raises(TapeReadError):
        read_tape(target)


def test_csv_round_trip_through_engine(tmp_path: Path, config: Config) -> None:
    csv_path = tmp_path / "tape.csv"
    make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2)).to_csv(csv_path, index=False)
    report = run(csv_path, config)
    assert [r.loan_id for r in report.loans] == [1, 2]
    assert all(r.verdict == "normal" for r in report.loans)


def test_jsonl_round_trip_through_engine(tmp_path: Path, config: Config) -> None:
    path = tmp_path / "tape.jsonl"
    make_frame(SyntheticLoan(loan_id=7)).to_json(path, orient="records", lines=True)
    report = run(path, config)
    assert report.loans[0].loan_id == 7
    assert report.loans[0].verdict == "normal"


@pytest.mark.parametrize("raw", [pd.NaT, pd.NA])
def test_pandas_missing_scalars(raw: object) -> None:
    assert parse_date(raw).value is None
    assert parse_float(raw) == (None, None)
    assert parse_int(raw) == (None, None)
    assert parse_identifier(raw) == (None, "")
    assert not parse_diary(raw).readable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", 1234.56),
        ("1.234.567,89", 1234567.89),
        ("1,234,567.89", 1234567.89),
        ("1,234,567", 1234567.0),
    ],
)
def test_explicit_numeric_separator_conventions(raw: str, expected: float) -> None:
    assert parse_float(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["1,234", "12,34,56", "1.23,456", "1,23.45"])
def test_ambiguous_or_malformed_numeric_separators_rejected(raw: str) -> None:
    value, error = parse_float(raw)
    assert value is None and error


@pytest.mark.parametrize("raw", ["9007199254740993", "9007199254740993.0"])
def test_integral_strings_do_not_round_through_float(raw: str) -> None:
    assert parse_int(raw) == (9007199254740993, None)


@pytest.mark.parametrize("field", ["Loan amount", "Interest rate", "Loan term", "Disbursal date"])
def test_required_blank_cells_record_parse_issues(config: Config, field: str) -> None:
    loan = SyntheticLoan()
    loan.summary_overrides[field] = ""
    parsed = ingest(loan.frame(), config).loans[0]
    assert any(issue.field == field for issue in parsed.parse_issues)


@pytest.mark.parametrize("field", ["Amount", "Pending amount"])
def test_diary_blank_cash_values_are_parse_issues(field: str) -> None:
    payment = parse_diary([_record(**{field: ""})]).payments[0]
    assert any(issue.field == field for issue in payment.issues)


def test_parse_errors_do_not_disclose_raw_values() -> None:
    secret = "private.person@example.org"
    assert secret not in str(parse_float(secret)[1])
    assert secret not in str(parse_int(secret)[1])
    assert secret not in str(parse_date(secret).error)
    diary = parse_diary([_record(Type=secret, State=secret, **{"Payment date": secret})])
    assert secret not in str(diary.payments[0].issues)


def test_duplicate_canonical_headers_are_schema_failure(config: Config) -> None:
    frame = SyntheticLoan().frame()
    frame[" Loan amount "] = frame["Loan amount"]
    result = ingest(frame, config)
    assert not result.schema.ok
    assert result.loans == []


@pytest.mark.parametrize("duplicate", ["Loan amount", " Loan amount "])
def test_reader_preserves_duplicate_header_failure(
    tmp_path: Path, config: Config, duplicate: str
) -> None:
    frame = SyntheticLoan().frame()
    frame.insert(2, duplicate, 1200, allow_duplicates=True)
    path = tmp_path / "duplicate.csv"
    frame.to_csv(path, index=False)
    report = run(path, config.model_copy(update={"as_of": date(2025, 2, 1)}))
    assert not report.schema_ok
    assert not report.loans


def test_duplicate_excel_headers_reject_before_health(tmp_path: Path, config: Config) -> None:
    frame = SyntheticLoan().frame()
    frame.insert(2, "Loan amount", 1200, allow_duplicates=True)
    path = tmp_path / "duplicate.xlsx"
    frame.to_excel(path, index=False)
    report = run(path, config.model_copy(update={"as_of": date(2025, 2, 1)}))
    assert not report.schema_ok
    assert report.tape_health is None


def test_reader_shard_keeps_snapshot_handle_open(tmp_path: Path) -> None:
    path = tmp_path / "shards.csv"
    frame = make_frame(SyntheticLoan(loan_id=1), SyntheticLoan(loan_id=2))
    frame.to_csv(path, index=False)
    whole = read_tape(path)
    shard = read_tape(path, rows=range(0, 1))
    assert shard.attrs["input_sha256"] == whole.attrs["input_sha256"]
    pd.testing.assert_frame_equal(shard, whole.iloc[:1].reset_index(drop=True))


def test_hash_describes_loaded_snapshot_not_later_replacement(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    path = tmp_path / "snapshot.csv"
    SyntheticLoan(loan_id=1).frame().to_csv(path, index=False)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    def read_then_replace(source: Path) -> pd.DataFrame:
        loaded = read_tape(source)
        SyntheticLoan(loan_id=2).frame().to_csv(source, index=False)
        return loaded

    monkeypatch.setattr("loan_dq.engine.read_tape", read_then_replace)
    report = run(path, config)
    assert report.input_sha256 == expected
    assert report.loans[0].loan_id == 1


def test_xls_is_not_claimed_without_an_installed_engine(tmp_path: Path) -> None:
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not an xlsx workbook")
    with pytest.raises(TapeReadError, match="unsupported file type"):
        read_tape(path)
