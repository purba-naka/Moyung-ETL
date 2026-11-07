"""Spec versions: a repair makes a new version and a diff, it never overwrites.

`diff_specs` lists what changed between two specs, by path (`sources.pkg.region.header_anchor`).
`SpecHistory` is a list of versions never edited in place; `add` returns a new history.
It is plain data (pydantic, JSON), so the host stores it wherever it likes; the library keeps
no state.

ponytail: lists inside a source or step (anchors, expect_columns, config lists) are compared
as a whole, not item by item; upgrade: item-level diff if agents need finer repair notes.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from excel_etl.pipeline import PipelineSpec

_SHOW = 80


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


MAX_NOTE = 500


class Change(_Model):
    where: str
    kind: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None

    def text(self) -> str:
        if self.kind == "added":
            return f"+ {self.where} = {self.after}"
        if self.kind == "removed":
            return f"- {self.where} (was {self.before})"
        return f"~ {self.where}: {self.before} -> {self.after}"


def _show(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= _SHOW else s[: _SHOW - 3] + "..."


def _walk(old: Any, new: Any, path: str, out: list[Change]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old.keys() | new.keys():
            sub = f"{path}.{k}" if path else k
            if k not in new:
                out.append(Change(where=sub, kind="removed", before=_show(old[k])))
            elif k not in old:
                out.append(Change(where=sub, kind="added", after=_show(new[k])))
            else:
                _walk(old[k], new[k], sub, out)
    elif old != new:
        out.append(Change(where=path, kind="changed", before=_show(old), after=_show(new)))


def diff_specs(old: PipelineSpec, new: PipelineSpec) -> tuple[Change, ...]:
    """Every difference, sorted by path. Steps are matched by id; a moved step is one change."""
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    steps_a = {s["id"]: s for s in a.pop("steps")}
    steps_b = {s["id"]: s for s in b.pop("steps")}
    a["steps"], b["steps"] = steps_a, steps_b
    out: list[Change] = []
    _walk(a, b, "", out)
    kept_a = [i for i in steps_a if i in steps_b]
    kept_b = [i for i in steps_b if i in steps_a]
    if kept_a != kept_b:
        out.append(
            Change(where="steps.order", kind="changed", before=_show(kept_a), after=_show(kept_b))
        )
    return tuple(sorted(out, key=lambda c: (c.where, c.kind)))


class SpecVersion(_Model):
    number: int  # 1, 2, 3 ...
    spec: PipelineSpec
    note: str = Field(max_length=MAX_NOTE)  # why this version exists (what was repaired)
    diff: tuple[Change, ...]  # against the previous version; empty for version 1


class SpecHistory(_Model):
    versions: tuple[SpecVersion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> SpecHistory:
        """Numbers and diffs are derived facts: a stored history that disagrees is refused."""
        if [v.number for v in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("version numbers must be 1, 2, 3 ... in order")
        if self.versions[0].diff:
            raise ValueError("version 1 has no diff")
        for prev, v in itertools.pairwise(self.versions):
            if v.diff != diff_specs(prev.spec, v.spec):
                raise ValueError(f"the diff of version {v.number} does not match its spec")
        return self

    @classmethod
    def start(cls, spec: PipelineSpec, note: str = "first version") -> SpecHistory:
        return cls(versions=(SpecVersion(number=1, spec=spec, note=note, diff=()),))

    @property
    def current(self) -> SpecVersion:
        return self.versions[-1]

    def add(self, spec: PipelineSpec, note: str) -> SpecHistory:
        """A new history with `spec` as the next version. A spec equal to the current one is
        refused: a repair that changes nothing would hide that it did not repair anything.
        """
        diff = diff_specs(self.current.spec, spec)
        if not diff:
            raise ValueError("the new spec is the same as the current version; nothing to add")
        if not note.strip():
            raise ValueError("give a note: what was repaired and why")
        spec = spec.model_copy(
            deep=True
        )  # the history must not change when the caller edits `spec`
        nxt = SpecVersion(number=self.current.number + 1, spec=spec, note=note, diff=diff)
        return SpecHistory(versions=(*self.versions, nxt))

    def get(self, number: int) -> SpecVersion:
        for v in self.versions:
            if v.number == number:
                return v
        raise KeyError(f"no version {number}; versions: 1..{self.current.number}")

    def text(self) -> str:
        lines = [f"spec history: {len(self.versions)} version(s), current v{self.current.number}"]
        for v in self.versions:
            lines.append(f"  v{v.number}: {v.note} ({len(v.diff)} change(s))")
            lines += [f"    {c.text()}" for c in v.diff]
        return "\n".join(lines)


__all__ = ["Change", "SpecHistory", "SpecVersion", "diff_specs"]
