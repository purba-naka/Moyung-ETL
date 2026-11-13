"""Pipeline spec: what to read from a workbook and which steps to run, as plain data.

A spec holds no cell addresses. A source finds its table by meaning (sheet name, header
text), so a changed workbook can be bound again without rewriting the spec. This module
checks the shape of a spec; node names and configs are checked by `engine.validate`.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_serializer,
    model_validator,
)

from excel_etl.nodes.base import _NAME_PATTERN, describe
from excel_etl.quality import Check, tables_of

# Letters, digits and `_`, up to 64 characters, not starting with `__` (helper columns and
# views inside queries use that prefix). No lookahead: pydantic's regex engine has none.
_IDENT = _NAME_PATTERN
_END = re.compile(r"until_blank_rows\(([0-9]+)\)")
# Output names become file names; Windows cannot create these whatever the extension.
_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

# a name becomes a relation name in duckdb, so letters, digits and _ only
Ident = Annotated[str, StringConstraints(pattern=_IDENT)]


class SpecError(Exception):
    """The spec text is not valid. The message names each problem and where it is."""


class _Model(BaseModel):
    # a misspelled key must fail loudly instead of silently dropping what the agent meant
    model_config = ConfigDict(extra="forbid")


class SheetRef(_Model):
    """How to find one sheet, or all matching sheets for a repeating table.

    Use `name`, `name_like`, or `fallback_contains` for one sheet. For many sheets with the
    same table, use `{"name_regex": "[0-9]{4}", "sheet_column": "period"}`: the full sheet
    name must match, and every output row records its sheet in `sheet_column`.
    """

    name: str | None = None
    name_like: str | None = None  # the sheet name as seen when the spec was written
    fallback_contains: list[str] = []  # texts that must appear on the sheet if the name moved
    name_regex: str | None = None
    sheet_column: str | None = None

    @model_validator(mode="after")
    def _some_way_to_find_it(self) -> SheetRef:
        if self.name_regex is not None:
            if self.name or self.name_like or self.fallback_contains:
                raise ValueError("name_regex cannot be combined with another sheet matcher")
            if self.sheet_column is None:
                raise ValueError("name_regex requires sheet_column")
            try:
                re.compile(self.name_regex)
            except re.error as e:
                raise ValueError(f"name_regex {self.name_regex!r} is not a regex: {e}") from e
        elif self.sheet_column is not None:
            raise ValueError("sheet_column requires name_regex")
        elif not (self.name or self.name_like or self.fallback_contains):
            raise ValueError("give name, name_like or fallback_contains, or use name_regex")
        if self.sheet_column is not None:
            if not self.sheet_column.strip() or self.sheet_column.startswith("__"):
                raise ValueError("sheet_column must be non-blank and cannot start with __")
            if len(self.sheet_column) > 100:
                raise ValueError("sheet_column must be at most 100 characters")
        return self


class RegionRef(_Model):
    """Where a source table is, by meaning.

    `header_anchor`: texts that must all be in the header; `header_rows`: how many rows the
    header takes. With 2+ rows the column names join the levels as `PARENT|LEAF` (a `Sales`
    over `Qty` is `Sales|Qty`; a parent spanning several columns names each of them).
    `end`: `until_blank_rows(N)` stops the table after N empty rows in a row. A total row
    below a gap of N or more empty rows is outside the table: raise N to read it (the run
    warns when filled rows lie below the end).
    """

    header_anchor: list[str] = Field(min_length=1)  # texts that must be in the header
    header_rows: int = Field(default=1, ge=1, le=10)
    end: str = "until_blank_rows(2)"

    @model_validator(mode="after")
    def _end_is_known(self) -> RegionRef:
        if not _END.fullmatch(self.end):
            raise ValueError(f"end must look like until_blank_rows(2), got {self.end!r}")
        return self

    @property
    def blank_gap(self) -> int:
        """How many empty rows in a row end the table (the one place `end` is read)."""
        m = _END.fullmatch(self.end)
        assert m is not None  # checked when the spec was built
        return int(m[1])


class ColumnExpect(_Model):
    match: str | None = None  # header text, letter case and spacing ignored
    match_regex: str | None = None
    # checked against the duckdb type of every matched column ("float" also accepts whole
    # numbers: a column of 1, 2, 3 is a number either way)
    type: Literal["text", "int", "float", "date", "datetime", "bool"] | None = None
    required: bool = True

    @model_validator(mode="after")
    def _one_matcher(self) -> ColumnExpect:
        if (self.match is None) == (self.match_regex is None):
            raise ValueError("give exactly one of match or match_regex")
        if self.match_regex is not None:
            try:
                re.compile(self.match_regex)
            except re.error as e:
                raise ValueError(f"match_regex {self.match_regex!r} is not a regex: {e}") from e
        return self


class Source(_Model):
    file_role: str = Field(min_length=1)  # a free label; the host maps it to a file in `files`
    sheet: SheetRef
    region: RegionRef
    expect_columns: list[ColumnExpect] = []


class Step(_Model):
    """One node of the pipeline. `inputs` is always a LIST of names (`["sales"]`, two for a
    join), read in that order; a step may read sources and earlier steps only."""

    id: Ident
    node: str = Field(min_length=1)
    inputs: list[Ident] = Field(min_length=1)
    config: dict[str, Any] = {}


class LoadTarget(_Model):
    """Where an output goes after the file is written, as a plan the host runs (see `load.py`).

    Only the table name and the way to write it. The database, schema, stage and credentials
    belong to the host, so a spec can move between environments and holds no secret.
    `replace` swaps the whole table, `append` adds rows, `merge` updates rows with the same
    `keys` and adds the rest (`keys` are the columns that identify one row).
    """

    table: Ident
    mode: Literal["replace", "append", "merge"] = "replace"
    keys: list[str] = []

    @model_validator(mode="after")
    def _keys_fit_mode(self) -> LoadTarget:
        if self.mode == "merge" and not self.keys:
            raise ValueError("merge needs keys: the columns that identify one row")
        if self.mode != "merge" and self.keys:
            raise ValueError(f"keys are only for merge, not {self.mode}")
        if len({k.casefold() for k in self.keys}) != len(self.keys):
            raise ValueError("keys lists a column twice (letter case is ignored)")
        return self


class Output(_Model):
    """A table written to a file. `input` is ONE name (not a list): the source or step to write.

    `load` (optional) says which database table the file is loaded into afterwards. Writing
    the file never needs it; only `plan_load` reads it.
    """

    input: Ident
    format: Literal["parquet", "csv"] = "parquet"  # the host picks the path, not the spec
    load: LoadTarget | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_load(self, handler: Any) -> dict[str, Any]:
        # most outputs have no load; a `"load": null` on each would only be noise in get_spec
        data = handler(self)
        if data.get("load") is None:
            data.pop("load", None)
        return data


class PipelineSpec(_Model):
    sources: dict[Ident, Source] = {}
    steps: list[Step] = []
    outputs: dict[Ident, Output] = {}
    checks: dict[Ident, Check] = {}  # rules and totals to hold before anything is written

    @model_validator(mode="after")
    def _names_and_wiring(self) -> PipelineSpec:
        # duckdb ignores letter case in names, so `Data` and `data` would be one table
        lower: dict[str, str] = {}
        for name in self.sources:
            if (other := lower.setdefault(name.lower(), name)) != name:
                raise ValueError(f"source names {other!r} and {name!r} differ only in letter case")
        known = set(self.sources)
        for step in self.steps:
            if step.id in known:
                raise ValueError(f"step id {step.id!r} is already used by a source or step")
            if (other := lower.setdefault(step.id.lower(), step.id)) != step.id:
                raise ValueError(f"names {other!r} and {step.id!r} differ only in letter case")
            for inp in step.inputs:
                # only earlier names count, so the steps form an order and cannot loop
                if inp not in known:
                    raise ValueError(
                        f"step {step.id!r} reads {inp!r}, which is not a source or an earlier step"
                    )
            known.add(step.id)
        for name, chk in self.checks.items():
            for t in tables_of(chk):
                if t not in known:
                    raise ValueError(f"check {name!r} reads {t!r}, which is not defined")
        files: dict[str, str] = {}
        for name, out in self.outputs.items():
            if out.input not in known:
                raise ValueError(f"output {name!r} reads {out.input!r}, which is not defined")
            if name.lower() in _DEVICE_NAMES:
                raise ValueError(f"output name {name!r} is a reserved file name on Windows")
            if (other := files.setdefault(name.lower(), name)) != name:
                raise ValueError(f"output names {other!r} and {name!r} would be the same file")
        tables: dict[str, str] = {}
        for name, out in self.outputs.items():
            if out.load is None:
                continue
            if out.format != "parquet":
                raise ValueError(f"output {name!r}: only a parquet file can be loaded, not csv")
            if (other := tables.setdefault(out.load.table.lower(), name)) != name:
                raise ValueError(
                    f"outputs {other!r} and {name!r} load into the same table {out.load.table!r}"
                )
        return self


def parse_spec(text: str) -> PipelineSpec:
    """JSON text -> PipelineSpec. Raises SpecError listing what is wrong (at most 10 items)."""
    if not isinstance(text, str):
        raise SpecError(f"spec must be a JSON string, got {type(text).__name__}")
    try:
        return PipelineSpec.model_validate_json(text)
    except ValidationError as e:
        msg = describe(e, "spec")
        hint = "\nThe spec must be JSON (YAML is not supported)." if "json" in msg.lower() else ""
        raise SpecError(msg + hint) from None


def patch_spec(spec: PipelineSpec, patch_json: str) -> PipelineSpec:
    """Apply a small edit instead of rewriting the whole spec.

    The patch is JSON: `steps` (a list of steps: one whose id exists replaces it in place, a
    new one goes right before the first step that reads it, else at the end), `remove` (step
    ids), `sources` and `outputs` (objects: a name replaces that entry, `null` removes it).
    The result is checked like any spec, so a step still read by another one cannot be
    removed. Raises SpecError.
    """
    try:
        patch = json.loads(patch_json)
    except (TypeError, ValueError) as e:
        raise SpecError(f"the patch must be a JSON object: {e}") from None
    if not isinstance(patch, dict):
        raise SpecError("the patch must be a JSON object")
    unknown = set(patch) - {"steps", "remove", "sources", "outputs"}
    if unknown:
        raise SpecError(
            f"patch: {sorted(unknown)} is not a setting here; use steps, remove, sources, outputs"
        )
    data = spec.model_dump(mode="json")
    steps: list[dict] = data["steps"]
    for gone in patch.get("remove", []):
        if gone not in {s["id"] for s in steps}:
            raise SpecError(f"remove: there is no step {gone!r}")
        steps[:] = [s for s in steps if s["id"] != gone]
    for new in patch.get("steps", []):
        if not isinstance(new, dict) or "id" not in new:
            raise SpecError("steps: each step needs an id")
        at = next((i for i, s in enumerate(steps) if s["id"] == new["id"]), None)
        if at is not None:
            steps[at] = new
            continue
        reader = next(
            (i for i, s in enumerate(steps) if new["id"] in s.get("inputs", [])), len(steps)
        )
        steps.insert(reader, new)
    for key in ("sources", "outputs"):
        for name, value in (patch.get(key) or {}).items():
            if value is None:
                data[key].pop(name, None)
            else:
                data[key][name] = value
    return parse_spec(json.dumps(data))


def dump_spec(spec: PipelineSpec) -> str:
    """PipelineSpec -> JSON text that parse_spec reads back to an equal spec."""
    return spec.model_dump_json(indent=2)
