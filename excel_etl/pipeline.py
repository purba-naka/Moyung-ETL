"""Pipeline spec: what to read from a workbook and which steps to run, as plain data.

A spec holds no cell addresses. A source finds its table by meaning (sheet name, header
text), so a changed workbook can be bound again without rewriting the spec. This module
checks the shape of a spec; node names and configs are checked by `engine.validate`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from excel_etl.nodes.base import describe

# Letters, digits and `_`, up to 64 characters, not starting with `__` (helper columns and
# views inside queries use that prefix). No lookahead: pydantic's regex engine has none.
_IDENT = r"^(?:[A-Za-z]|_[A-Za-z0-9])[A-Za-z0-9_]{0,62}$"
_END = re.compile(r"^until_blank_rows\(\d+\)$")
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
    name: str | None = None
    name_like: str | None = None  # the sheet name as seen when the spec was written
    fallback_contains: list[str] = []  # texts that must appear on the sheet if the name moved

    @model_validator(mode="after")
    def _some_way_to_find_it(self) -> SheetRef:
        if not (self.name or self.name_like or self.fallback_contains):
            raise ValueError("give name, name_like or fallback_contains")
        return self


class RegionRef(_Model):
    header_anchor: list[str] = Field(min_length=1)  # texts that must be in the header
    header_rows: int = Field(default=1, ge=1, le=10)
    end: str = "until_blank_rows(2)"

    @model_validator(mode="after")
    def _end_is_known(self) -> RegionRef:
        if not _END.match(self.end):
            raise ValueError(f"end must look like until_blank_rows(2), got {self.end!r}")
        return self


class ColumnExpect(_Model):
    match: str | None = None  # header text, letter case and spacing ignored
    match_regex: str | None = None
    type: Literal["text", "int", "float", "date", "datetime", "bool"] | None = None
    required: bool = True
    group: str | None = None  # not used yet; kept so specs can name column families

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
    id: Ident
    node: str = Field(min_length=1)
    inputs: list[Ident] = Field(min_length=1)
    config: dict[str, Any] = {}


class Output(_Model):
    input: Ident
    format: Literal["parquet", "csv"] = "parquet"  # the host picks the path, not the spec


class PipelineSpec(_Model):
    sources: dict[Ident, Source] = {}
    steps: list[Step] = []
    outputs: dict[Ident, Output] = {}

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
        files: dict[str, str] = {}
        for name, out in self.outputs.items():
            if out.input not in known:
                raise ValueError(f"output {name!r} reads {out.input!r}, which is not defined")
            if name.lower() in _DEVICE_NAMES:
                raise ValueError(f"output name {name!r} is a reserved file name on Windows")
            if (other := files.setdefault(name.lower(), name)) != name:
                raise ValueError(f"output names {other!r} and {name!r} would be the same file")
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


def dump_spec(spec: PipelineSpec) -> str:
    """PipelineSpec -> JSON text that parse_spec reads back to an equal spec."""
    return spec.model_dump_json(indent=2)
