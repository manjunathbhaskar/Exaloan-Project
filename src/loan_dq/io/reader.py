"""Read a loan tape from disk into a raw DataFrame without interpreting any cell.

Excel/CSV/Parquet/JSON-lines are accepted. Interpretation (types, dates, the nested diary)
happens in ``loan_dq.ingest`` so a different transport never changes the rules.

``rows`` selects a contiguous row range (0-based positions in the file, header excluded) for a
sharded run. CSV is streamed chunk by chunk so a shard never holds more than its own rows plus
one chunk in memory. Excel, Parquet and JSON-lines are loaded whole and sliced - fine for a
workbook, and the reason a multi-gigabyte tape should arrive as CSV (Parquet row-group
selection is the natural next step and needs pyarrow, which is not a dependency today).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO

import pandas as pd

CSV_CHUNK_ROWS = 20_000


class TapeReadError(Exception):
    """The file itself cannot be read (missing, wrong format, empty)."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_path(path: Path) -> str:
    if not path.exists():
        raise TapeReadError(f"input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".xlsm", ".csv", ".parquet", ".jsonl", ".ndjson"}:
        raise TapeReadError(f"unsupported file type {suffix!r}")
    return suffix


def count_rows(path: Path, *, sheet: str | int = 0) -> int:
    """Data rows in the tape (header excluded), without materialising the whole tape for
    CSV/Parquet."""
    suffix = _check_path(path)
    try:
        if suffix == ".csv":
            return sum(
                len(chunk)
                for chunk in pd.read_csv(
                    path,
                    dtype=object,
                    keep_default_na=False,
                    usecols=[0],
                    chunksize=CSV_CHUNK_ROWS,
                )
            )
        return len(_read_whole(path, suffix, sheet))
    except TapeReadError:
        raise
    except Exception as exc:
        raise TapeReadError(f"could not read tape ({exc.__class__.__name__})") from exc


def read_tape(path: Path, *, sheet: str | int = 0, rows: range | None = None) -> pd.DataFrame:
    suffix = _check_path(path)
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
            source.seek(0)
            headers: list[str] | None = None
            if suffix == ".csv":
                header = pd.read_csv(
                    source, header=None, nrows=1, dtype=object, keep_default_na=False
                )
                headers = [str(c).strip() or f"Unnamed: {i}" for i, c in enumerate(header.iloc[0])]
                source.seek(0)
            if rows is None:
                frame = _read_whole(source, suffix, sheet)
            elif suffix == ".csv":
                frame = _read_csv_rows(source, rows)
            else:
                frame = _read_whole(source, suffix, sheet).iloc[rows.start : rows.stop]
            if headers is not None:
                frame.columns = headers
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise TapeReadError("input changed while being read; retry with an immutable tape")
            frame.attrs["input_sha256"] = digest.hexdigest()
    except TapeReadError:
        raise
    except Exception as exc:
        raise TapeReadError(f"could not read tape ({exc.__class__.__name__})") from exc
    if frame.empty:
        where = "" if rows is None else f" in rows {rows.start}-{rows.stop - 1}"
        raise TapeReadError(f"{path.name} contains no rows{where}")
    frame = frame.drop(columns=[c for c in frame.columns if str(c).startswith("Unnamed")])
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame if rows is None else frame.reset_index(drop=True)


def _read_whole(path: Path | BinaryIO, suffix: str, sheet: str | int) -> pd.DataFrame:
    if suffix in {".xlsx", ".xlsm"}:
        frame = pd.read_excel(path, sheet_name=sheet, dtype=object, header=None, engine="openpyxl")
        if frame.empty:
            return frame
        frame.columns = [
            f"Unnamed: {i}" if pd.isna(c) else str(c).strip() for i, c in enumerate(frame.iloc[0])
        ]
        return frame.iloc[1:].reset_index(drop=True)
    if suffix == ".csv":
        return pd.read_csv(path, dtype=object, keep_default_na=False)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_json(path, lines=True, dtype=False)


def _read_csv_rows(path: Path | BinaryIO, rows: range) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    seen = 0
    columns: list[str] | None = None
    with pd.read_csv(path, dtype=object, keep_default_na=False, chunksize=CSV_CHUNK_ROWS) as chunks:
        for chunk in chunks:
            columns = [str(c) for c in chunk.columns]
            lo = max(rows.start - seen, 0)
            hi = min(rows.stop - seen, len(chunk))
            if hi > lo:
                parts.append(chunk.iloc[lo:hi])
            seen += len(chunk)
            if seen >= rows.stop:
                break
    if parts:
        return pd.concat(parts)
    return pd.DataFrame(columns=columns or [])
