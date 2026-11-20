"""CSV and Parquet files as sources.

Unlike a workbook, these files have no sheets, cells or regions: they already are a table.
duckdb reads them itself, so a `TableFile` is a path plus a hash. Bytes given by the host are
written once to a private temporary directory that is removed with the object. The engine
lets its locked duckdb connection read exactly these paths and no others.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import Literal

FileKind = Literal["excel", "csv", "parquet", "json", "jsonl"]
TableFormat = Literal["csv", "parquet", "json", "jsonl"]
_TEXT_FORMATS = ("csv", "json", "jsonl")

_SUFFIXES: dict[str, FileKind] = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
}
_CHUNK = 1 << 20
_PARQUET_MAGIC = b"PAR1"


class TableFileError(Exception):
    """The file cannot be used as a table; the message says why."""


def kind_of_filename(filename: str) -> FileKind:
    """What a registered file is, from its extension. Anything unknown is a workbook, which
    is what files were before other kinds existed."""
    return _SUFFIXES.get(Path(filename).suffix.lower(), "excel")


class TableFile:
    """A CSV or Parquet file on disk: `path`, `sha256` (of the content) and `format`."""

    def __init__(self, path: Path, sha256: str, format: TableFormat) -> None:
        self.path = path
        self.sha256 = sha256
        self.format = format

    @classmethod
    def open(cls, source: str | os.PathLike[str] | bytes, format: TableFormat) -> TableFile:
        """Open a file by path or from its bytes. Raises TableFileError if it cannot be a
        table of this format (missing, empty, binary where text is expected, not Parquet)."""
        if isinstance(source, bytes):
            folder = Path(tempfile.mkdtemp(prefix="moyung_"))
            path = folder / f"data.{format}"
            path.write_bytes(source)
            tf = cls(path, hashlib.sha256(source).hexdigest(), format)
            weakref.finalize(tf, shutil.rmtree, folder, ignore_errors=True)
        else:
            try:
                path = Path(source).resolve(strict=True)
            except (OSError, TypeError, ValueError) as e:
                raise TableFileError(f"cannot read file: {e}") from e
            if not path.is_file():
                raise TableFileError(f"{str(path)!r} is not a file")
            digest = hashlib.sha256()
            try:
                with path.open("rb") as f:
                    while block := f.read(_CHUNK):
                        digest.update(block)
            except OSError as e:
                raise TableFileError(f"cannot read file: {e}") from e
            tf = cls(path, digest.hexdigest(), format)
        tf._check_shape()
        return tf

    def _ends_with(self, last: bytes) -> bool:
        """Whether the last non-blank byte of the file is `last`."""
        size = self.path.stat().st_size
        with self.path.open("rb") as f:
            f.seek(max(0, size - 1024))
            return f.read().rstrip()[-1:] == last

    def _check_shape(self) -> None:
        with self.path.open("rb") as f:
            head = f.read(8192)
        if not head:
            raise TableFileError("the file is empty")
        if self.format == "parquet" and not head.startswith(_PARQUET_MAGIC):
            raise TableFileError("not a Parquet file (it does not start with PAR1)")
        if self.format in _TEXT_FORMATS and head.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise TableFileError("the file is UTF-16 text; only UTF-8 is read, save it as UTF-8")
        if self.format in _TEXT_FORMATS and (b"\x00" in head or head.startswith(_PARQUET_MAGIC)):
            raise TableFileError(
                f"not a text file: it looks binary, not {self.format} (only UTF-8 is read)"
            )
        first = head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1]
        if self.format == "json" and first != b"[":
            raise TableFileError("a json file is one array of objects: it should start with [")
        if self.format == "json" and not self._ends_with(b"]"):
            raise TableFileError("the json file looks cut off: it should end with ]")
        if self.format == "jsonl" and first != b"{":
            raise TableFileError(
                "a jsonl file has one object per line: it should start with { "
                "(a file that starts with [ is json)"
            )


def as_table_file(ref: object, kind: str) -> TableFile:
    """`ref` (a TableFile, a path or bytes) as a table file of `kind`. A workbook or a file of
    another format is refused with a message, so every caller judges a mismatch the same way."""
    from moyung.workbook import Workbook

    if isinstance(ref, Workbook):
        raise TableFileError(f"the file is an Excel workbook, but this source is {kind}")
    if isinstance(ref, TableFile):
        if ref.format != kind:
            raise TableFileError(f"the file is {ref.format}, but this source is {kind}")
        return ref
    return TableFile.open(ref, kind)  # type: ignore[arg-type]
