"""Build a pipeline from the output a person wants, without an LLM.

Only existing nodes are assembled. Candidates are scored by running them, because schema
binding alone cannot see a bad cast or an empty result. Synthesis is a convenience layer:
its spec is an ordinary PipelineSpec and remains editable by an agent or a person.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from excel_etl.engine import run, validate
from excel_etl.inspect.encode import column_names
from excel_etl.inspect.lineage import region_role
from excel_etl.inspect.regions import Region, SheetGrid, detect_regions
from excel_etl.knowledge import Knowledge
from excel_etl.nodes.base import _NAME_PATTERN, NodeError
from excel_etl.nodes.source import _type, type_fits
from excel_etl.nodes.sqlexpr import check_expression
from excel_etl.pipeline import Output, PipelineSpec, Step, dump_spec
from excel_etl.workbook import Workbook

Ident = str
_TARGET_TYPES = {
    "text": "VARCHAR",
    "int": "BIGINT",
    "float": "DOUBLE",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "bool": "BOOLEAN",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetColumn(_Model):
    name: Ident
    type: Literal["text", "int", "float", "date", "datetime", "bool"]
    hints: list[str] = []
    from_header: bool = False
    required: bool = True

    @model_validator(mode="after")
    def _valid_name(self) -> TargetColumn:
        if not re.fullmatch(_NAME_PATTERN, self.name):
            raise ValueError(f"column name {self.name!r} must be a simple identifier")
        return self


class Target(_Model):
    name: Ident
    columns: list[TargetColumn] = Field(min_length=1)
    grain: list[str] = Field(min_length=1)
    total_regex: str = r"(?i)^\s*(?:(?:sub\s*)?(?:total|jumlah|rekap)\b|jml(?:\.|\b))"
    filters: list[str] = []
    decimal: Literal[".", ","] = "."  # how numbers typed as text read: 1,234.56 or 1.234,56

    @model_validator(mode="after")
    def _shape(self) -> Target:
        if not re.fullmatch(_NAME_PATTERN, self.name):
            raise ValueError("target name must be a simple identifier")
        names = [c.name for c in self.columns]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("target columns differ only in letter case or are repeated")
        unknown = [g for g in self.grain if g not in names]
        if unknown:
            raise ValueError(f"grain columns are not target columns: {unknown}")
        headers = [c for c in self.columns if c.from_header]
        if len(headers) > 1:
            raise ValueError("one from_header column is supported per target")
        if headers and not any(
            c.name not in self.grain and not c.from_header for c in self.columns
        ):
            raise ValueError("a from_header target needs at least one value column")
        if headers and headers[0].name in self.grain and headers[0].type != "text":
            raise ValueError("a from_header grain column must have type text")
        try:
            re.compile(self.total_regex)
        except re.error as e:
            raise ValueError(f"total_regex is invalid: {e}") from e
        try:
            self.filters = [check_expression(f) for f in self.filters]
        except NodeError as e:
            raise ValueError(str(e)) from e
        return self


class Question(_Model):
    id: str
    text: str
    options: list[str]


class Candidate(_Model):
    region: str
    spec: str
    score: float
    reasons: list[str]
    problems: list[str]
    preview: list[dict] = []


class SynthReport(_Model):
    status: Literal["ok", "ask", "none"]
    best: Candidate | None = None
    others: list[Candidate] = []
    questions: list[Question] = []
    notes: list[str] = []  # why tables were passed over; a bare "none" helps nobody

    def text(self) -> str:
        lines = [f"synth: {self.status}"]
        if self.best:
            lines.append(f"best: {self.best.region}, score {self.best.score:.3f}")
            lines.extend(f"  {r}" for r in self.best.reasons)
            lines.extend(f"  problem: {p}" for p in self.best.problems)
        lines.extend(f"  question {q.id}: {q.text}" for q in self.questions)
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


class Proposal(_Model):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str
    sheet: str
    sheet_index: int
    region: Region
    grid: object
    role: str
    name_regex: str | None = None


def _representative(indexes: list[int], wb: Workbook) -> int:
    """The sheet of a group that stands for it: the first one that holds numbers. An empty
    template or a sheet with only headers shares the layout but cannot show how data reads."""
    key = f"synth-rep:{indexes[0]}:{len(indexes)}"
    if (hit := wb.memo.get(key)) is not None:
        return hit
    pick = indexes[0]
    for i in indexes:
        g = SheetGrid(wb, wb.sheets[i].name)
        if any(
            isinstance(v, (int, float)) and not isinstance(v, bool) for row in g.values for v in row
        ):
            pick = i
            break
    wb.memo[key] = pick
    return pick


def proposal_regions(wb: Workbook, file_id: str = "F1") -> list[Proposal]:
    """Structured source/mixed tables. Derived tables never become candidates."""
    key = f"synth-proposals:{file_id}"
    if (hit := wb.memo.get(key)) is not None:
        return hit
    from excel_etl.inspect.encode import _shape_groups

    groups = {i: (idx, pat) for idx, pat in _shape_groups(wb) for i in idx}
    out: list[Proposal] = []
    for i, sheet in enumerate(wb.sheets):
        grid = SheetGrid(wb, sheet.name)
        for j, region in enumerate(detect_regions(wb, sheet.name, grid), start=1):
            if not region.header_rows:
                continue
            role = region_role(wb, sheet.name, region.box)
            if role not in ("source", "mixed"):
                continue
            grouped = groups.get(i)
            if grouped and _representative(grouped[0], wb) != i:
                continue
            out.append(
                Proposal(
                    id=f"{file_id}.S{i + 1}.R{j}",
                    sheet=sheet.name,
                    sheet_index=i,
                    region=region,
                    grid=grid,
                    role=role,
                    name_regex=grouped[1] if grouped else None,
                )
            )
    wb.memo[key] = out
    return out


def _key(text: str) -> str:
    return " ".join(text.strip().casefold().replace("_", " ").split())


def _score_column(col: TargetColumn, header: str, answers: dict[str, str]) -> float:
    if answers.get(f"col:{col.name}") == header:
        return 1.0
    h, name = _key(header), _key(col.name)
    leaf = _key(header.rsplit("|", 1)[-1])
    hints = {_key(x) for x in col.hints}
    if h == name:
        return 1.0
    if h in hints or leaf in hints:
        return 0.95
    if leaf == name:
        return 0.9
    # "STOK FISIK" should still find "STOK FISIK (ON HAND)": a hint or name that starts the
    # header is a strong sign, just weaker than an exact hit.
    if any(x and leaf.startswith(x) for x in (*hints, name)):
        return 0.85
    ratio = max(
        difflib.SequenceMatcher(None, name, h).ratio(),
        difflib.SequenceMatcher(None, name, leaf).ratio(),
    )
    return ratio * 0.8 if ratio >= 0.75 else 0.0


def match_columns(
    target: Target,
    names: list[str],
    types: list[str],
    answers: dict[str, str] | None = None,
    knowledge: Knowledge | None = None,
) -> tuple[dict[str, str], dict[str, float], list[Question]]:
    """Map target columns to headers, with explicit answers taking precedence."""
    answers = answers or {}
    glossary = (knowledge.glossary if knowledge else {}) or {}
    used: set[str] = set()
    mapping: dict[str, str] = {}
    scores: dict[str, float] = {}
    questions: list[Question] = []
    # Key columns (province, regency, code...) sit left of the repeated "group|measure" blocks.
    # Once the first "group|measure" header appears, every header after it must be one too.
    first_group = next((i for i, n in enumerate(names) if "|" in n), len(names))
    grouped = (
        any(c.from_header for c in target.columns)
        and first_group < len(names)
        and all("|" in n for n in names[first_group:])
    )
    value_targets = [c for c in target.columns if not c.from_header and c.name not in target.grain]
    for col in target.columns:
        if col.from_header:
            continue
        enriched = col.model_copy(update={"hints": [*col.hints, glossary.get(col.name, "")]})
        if grouped and col in value_targets:
            leaf_names = list(dict.fromkeys(n.rsplit("|", 1)[-1] for n in names if "|" in n))
            leaf_types = []
            for leaf in leaf_names:
                kinds = [
                    t for n, t in zip(names, types, strict=True) if n.rsplit("|", 1)[-1] == leaf
                ]
                leaf_types.append(
                    "DOUBLE" if all(t in ("BIGINT", "DOUBLE") for t in kinds) else kinds[0]
                )
            scores_for_col = [
                (_score_column(enriched, n, answers), -i, n) for i, n in enumerate(leaf_names)
            ]
            scores_for_col = [x for x in scores_for_col if x[0]]
            scores_for_col.sort(reverse=True)
            if scores_for_col:
                mapping[col.name] = scores_for_col[0][2]
                scores[col.name] = scores_for_col[0][0]
            else:
                # Ask even for optional columns: an unmapped value column breaks the cast
                # step, so the region would vanish without telling the user why.
                questions.append(
                    Question(
                        id=f"col:{col.name}",
                        text=f"Which repeated measure supplies {col.name}?",
                        options=leaf_names,
                    )
                )
            continue
        candidates = []
        for i, (name, kind) in enumerate(zip(names, types, strict=True)):
            if name in used:
                continue
            score = _score_column(enriched, name, answers)
            if score and not type_fits(col.type, kind) and col.type != "float":
                score *= 0.5
            if answers.get(f"col:{col.name}") == name:
                score = 2.0  # the user chose this header: nothing else may outrank it
            if score:
                candidates.append((score, -i, name))
        candidates.sort(reverse=True)
        if not candidates:
            if col in value_targets and col.required:
                scores[col.name] = 1.0
                continue
            if col.required:
                questions.append(
                    Question(
                        id=f"col:{col.name}",
                        text=f"Which header supplies {col.name}?",
                        options=names,
                    )
                )
            continue
        best = candidates[0]
        if (
            best[0] < 2.0
            and len(candidates) > 1
            and best[0] - candidates[1][0] < 0.1
            and candidates[1][0] >= 0.6
        ):
            questions.append(
                Question(
                    id=f"col:{col.name}",
                    text=f"Which header supplies {col.name}?",
                    options=[c[2] for c in candidates[:5]],
                )
            )
            continue
        mapping[col.name], scores[col.name] = best[2], min(best[0], 1.0)
        used.add(best[2])
    return mapping, scores, questions


def _proposal_info(p: Proposal) -> tuple[list[str], list[str], list[list[object]]]:
    names = column_names(p.grid, p.region)
    left, right = p.region.box[1], p.region.box[3]
    last_header = p.region.header_rows[-1]
    rows = [list(p.grid.row(r, left, right)) for r in range(last_header + 1, p.region.box[2] + 1)]
    types = [_type([row[i] for row in rows if row[i] is not None]) for i in range(len(names))]
    return names, types, rows


_FROM_DUCK = {v: k for k, v in _TARGET_TYPES.items()}


def _slug(header: str, taken: set[str]) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", _key(header.rsplit("|", 1)[-1])).strip("_")[:50] or "col"
    if not re.fullmatch(_NAME_PATTERN, name):
        name = "c_" + name
    name = _free_name(name, taken)
    taken.add(name.casefold())
    return name


def draft_targets(wb: Workbook, file_id: str = "F1") -> list[dict]:
    """One editable Target guess per table, so a person crosses out columns instead of typing
    them. Text columns become the grain; "group|measure" headers become one from_header
    column plus one column per measure."""
    # ponytail: wide month headers (Jan, Feb) are not detected as from_header; the user ticks
    # it. Upgrade: reuse the unpivot detection in _build.
    out = []
    for p in proposal_regions(wb, file_id)[:40]:
        names, types, rows = _proposal_info(p)
        taken: set[str] = set()
        first = next((i for i, n in enumerate(names) if "|" in n), len(names))
        grouped = first < len(names) and all("|" in n for n in names[first:])
        cols: list[TargetColumn] = []
        grain: list[str] = []
        for n, t in zip(names[:first] if grouped else names, types, strict=False):
            c = TargetColumn(name=_slug(n, taken), type=_FROM_DUCK.get(t, "text"), hints=[n])
            cols.append(c)
            if c.type == "text":
                grain.append(c.name)
        if grouped:
            group = TargetColumn(name=_free_name("group", taken), type="text", from_header=True)
            taken.add(group.name)
            cols.append(group)
            grain.append(group.name)
            for leaf in dict.fromkeys(n.rsplit("|", 1)[-1] for n in names[first:]):
                kinds = {t for n, t in zip(names, types, strict=True) if n.endswith("|" + leaf)}
                kind = "float" if kinds <= {"BIGINT", "DOUBLE"} else "text"
                cols.append(TargetColumn(name=_slug(leaf, taken), type=kind, hints=[leaf]))
        if not grain:
            grain = [cols[0].name]
        out.append(
            {
                "region": p.id,
                "sheet": p.sheet,
                "rows": len(rows),
                "headers": [n.replace("|", " / ") for n in names[:6]],
                "target": Target(name="clean_data", columns=cols, grain=grain).model_dump(),
            }
        )
    return out


def _step(steps: list[Step], ident: str, node: str, current: str, config: dict) -> str:
    steps.append(Step(id=ident, node=node, inputs=[current], config=config))
    return ident


def _build(
    target: Target,
    p: Proposal,
    mapping: dict[str, str],
    names: list[str],
    types: list[str],
    rows: list[list[object]],
) -> tuple[PipelineSpec | None, list[str], list[str]]:
    header_target = next((c for c in target.columns if c.from_header), None)
    # The sheet name column must not clash with any source header or target column, in any
    # letter case (the loader compares case-insensitively).
    taken = {n.casefold() for n in names} | {c.name.casefold() for c in target.columns}
    sheet_col = _free_name("source_sheet", taken)
    source_sheet = (
        {"name_regex": p.name_regex, "sheet_column": sheet_col}
        if p.name_regex
        else {"name": p.sheet}
    )
    # An optional column the sheet does not have is left out; keeping it would break the
    # cast and select steps and the whole table would vanish.
    output_columns = [
        c
        for c in target.columns
        if c.required or c.from_header or c.name in mapping or _is_measure(c, target)
    ]
    grain = list(target.grain)
    if p.name_regex:
        output_columns.append(TargetColumn(name=sheet_col, type="text"))
        grain.append(sheet_col)
    anchors = list(
        dict.fromkeys(
            str(p.grid.label(r, c))
            for r in p.region.header_rows
            for c in range(p.region.box[1], p.region.box[3] + 1)
            if p.grid.label(r, c) is not None
        )
    )[:3]
    spec = PipelineSpec(
        sources={
            "input": {
                "file_role": "main",
                "sheet": source_sheet,
                "region": {
                    "header_anchor": anchors,
                    "header_rows": len(p.region.header_rows),
                },
            }
        }
    )
    steps: list[Step] = []
    reasons = [f"Selected {p.id} ({p.role}) on sheet {p.sheet!r}."]
    for c in target.columns:
        # A whole-number target would round 1.5 to 2 without a word; say so.
        if c.type == "int" and mapping.get(c.name) in names:
            col = names.index(mapping[c.name])
            if any(isinstance(r[col], float) and not r[col].is_integer() for r in rows):
                reasons.append(f"{c.name} is a whole number but the sheet has decimals: rounded.")
    if p.name_regex:
        reasons.append(
            f"Reads every sheet with this layout, named like /{p.name_regex}/, "
            "and keeps the sheet name in source_sheet."
        )
    current = "input"
    fill = []
    for c in target.columns:
        # Measure columns map to a leaf like "SAFETY STOCK" that is not a real header
        # ("UREA|SAFETY STOCK"), so there is no single column to fill down.
        if c.name not in mapping or c.type != "text" or mapping[c.name] not in names:
            continue
        idx = names.index(mapping[c.name])
        vals = [row[idx] for row in rows]
        if any(v is None for v in vals) and any(v is not None for v in vals):
            fill.append(mapping[c.name])
    if fill:
        current = _step(steps, "filled", "fill_down", current, {"columns": fill})
        reasons.append("Filled down blank repeated labels.")
    grain_text = [
        mapping[c.name]
        for c in target.columns
        if c.name in target.grain and c.name in mapping and c.type == "text"
    ]
    for i, column in enumerate(grain_text):
        current = _step(
            steps,
            "details" if i == 0 else f"details_{i}",
            "drop_rows",
            current,
            {"column": column, "drop_regex": target.total_regex},
        )
    if grain_text:
        reasons.append("Dropped total/subtotal rows from every text key column.")
    rename = {old: new for new, old in mapping.items()}
    # A source header nobody asked for may share a name with a target column (header "prov"
    # while "Provinsi" is renamed to prov). Rename would refuse the clash, so drop it first.
    target_keys = {c.name.casefold() for c in target.columns}
    unused = [
        n
        for n in names
        if n not in set(mapping.values()) and n.casefold() in target_keys and "|" not in n
    ]
    if unused and not header_target:
        kept = [n for n in names if n not in unused]
        current = _step(steps, "unused", "select", current, {"columns": kept})
        reasons.append(f"Dropped unused source columns that share a target name: {unused}.")
    # The sheet name column of a multi-sheet read is a key too, so unpivot must carry it.
    sheet_key = [sheet_col] if p.name_regex else []
    if header_target:
        candidates = [
            n
            for n, t in zip(names, types, strict=True)
            if n not in set(mapping.values())
            and t in ("BIGINT", "DOUBLE")
            and not re.match(target.total_regex, n)
        ]
        grouped = p.region.groups and all("|" in n for n in candidates)
        if grouped:
            total_columns = [n for n in names if re.match(target.total_regex, n)]
            if total_columns:
                current = _step(
                    steps,
                    "totals",
                    "select",
                    current,
                    {"columns": [n for n in names if n not in total_columns]},
                )
                reasons.append("Excluded total columns before unpivoting repeated blocks.")
            group_keep = [
                mapping[c.name]
                for c in target.columns
                if c.name in target.grain and c.name in mapping
            ] + sheet_key
            current = _step(
                steps,
                "long",
                "unpivot_groups",
                current,
                {
                    "pattern": r"^(?P<group>.+)[|](?P<measure>[^|]+)$",
                    "keep": group_keep,
                    "group_column": header_target.name,
                },
            )
            rename = {old: new for new, old in mapping.items() if "|" not in old}
            rename.update(
                {
                    old: new
                    for new, old in mapping.items()
                    if any(n.endswith("|" + old) for n in names)
                }
            )
            reasons.append("Unpivoted repeated column blocks into group rows.")
        else:
            if not candidates:
                return None, ["No numeric repeated columns for from_header."], grain
            keep = list(
                dict.fromkeys(
                    [mapping[c.name] for c in target.columns if c.name in mapping] + sheet_key
                )
            )
            current = _step(
                steps,
                "long",
                "unpivot",
                current,
                {
                    "keep": keep,
                    "columns": candidates,
                    "name_column": header_target.name,
                    "value_column": "__value",
                },
            )
            value_targets = [
                c for c in target.columns if not c.from_header and c.name not in target.grain
            ]
            if value_targets:
                rename["__value"] = value_targets[0].name
            reasons.append("Unpivoted repeated numeric columns into header/value rows.")
    if rename:
        current = _step(steps, "renamed", "rename", current, {"mapping": rename})
    casts = {c.name: _TARGET_TYPES[c.type] for c in output_columns}
    cast_config: dict = {"types": casts}
    if target.decimal != ".":
        cast_config["decimal"] = target.decimal
    current = _step(steps, "typed", "cast", current, cast_config)
    if target.filters:
        current = _step(
            steps,
            "filtered",
            "filter",
            current,
            {"where": " AND ".join(f"({f})" for f in target.filters)},
        )
    _step(
        steps,
        "selected",
        "select",
        current,
        {"columns": [c.name for c in output_columns]},
    )
    spec.steps = steps
    spec.outputs = {target.name: Output(input="selected")}
    return spec, reasons, grain


def _is_measure(c: TargetColumn, target: Target) -> bool:
    """A value column filled from the repeated blocks; it has no header of its own."""
    return any(x.from_header for x in target.columns) and c.name not in target.grain


def _free_name(base: str, taken: set[str]) -> str:
    name = base
    while name.casefold() in taken:
        name += "_"
    return name


def _json_value(v):
    if isinstance(v, (date, datetime, time, timedelta)):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _evaluate(
    target: Target,
    p: Proposal,
    spec: PipelineSpec,
    reasons: list[str],
    scores: dict[str, float],
    wb: Workbook,
    grain: list[str],
) -> tuple[Candidate | None, str]:
    """The scored candidate, or None and the reason it was dropped."""
    problems = []
    # Score on the sample sheet only. A file with 150 daily sheets would otherwise be read in
    # full for every candidate; the returned spec still reads them all.
    probe = spec
    if p.name_regex:
        probe = spec.model_copy(deep=True)
        sheet = probe.sources["input"].sheet
        probe.sources["input"].sheet = type(sheet)(
            name_regex=f"^{re.escape(p.sheet)}$", sheet_column=sheet.sheet_column
        )
    report = validate(probe, {"main": wb})
    if not report.ok:
        return None, "; ".join(f"{i.where}: {i.message}" for i in report.issues[:2])
    with tempfile.TemporaryDirectory() as tmp:
        result = run(probe, {"main": wb}, tmp)
        if result.status != "ok":
            return None, " ".join(result.text().splitlines()[1:3])[:240]
        path = Path(tmp) / f"{target.name}.parquet"
        with duckdb.connect() as con:
            con.execute("CREATE TABLE result AS SELECT * FROM read_parquet(?)", [str(path)])
            columns = [d[0] for d in con.execute("SELECT * FROM result LIMIT 0").description]
            rows = con.execute("SELECT * FROM result LIMIT 21").fetchall()
            count = con.execute("SELECT count(*) FROM result").fetchall()[0][0]
            if not count:
                return None, "the table has no data rows after the steps (filters too strict?)"
            quoted = [f'"{c}"' for c in columns]
            nulls_total = con.execute(
                "SELECT " + " + ".join(f"count(*) - count({c})" for c in quoted) + " FROM result"
            ).fetchall()[0][0]
            grain_cols = [f'"{c}"' for c in grain if c in columns]
            distinct = (
                con.execute(
                    f"SELECT count(*) FROM (SELECT DISTINCT {', '.join(grain_cols)} FROM result)"
                ).fetchall()[0][0]
                if grain_cols
                else count
            )
    required = [c for c in target.columns if c.required]
    coverage = sum(c.name in columns for c in required) / len(required)
    nulls = nulls_total / (count * len(columns))
    fill = 1.0 - nulls
    unique = distinct / count
    match = sum(scores.values()) / max(len(scores), 1)
    score = 0.35 * coverage + 0.25 * fill + 0.25 * unique + 0.15 * match
    if unique < 1:
        problems.append("Grain has duplicate rows.")
    if fill < 1:
        problems.append("Some output values are empty or could not be cast.")
    # What run() warned about (rows left below the table, Excel error cells) is just as true
    # of the real result, and the person reading `status == ok` would never see it otherwise.
    problems.extend(result.warnings)
    left_out = [c.name for c in target.columns if c.name not in columns]
    if left_out:
        problems.append(f"Not found in the sheet, left out of the output: {', '.join(left_out)}.")
    return Candidate(
        region=p.id,
        spec=dump_spec(spec),
        score=round(score, 6),
        reasons=reasons,
        problems=problems,
        preview=[
            {c: _json_value(v) for c, v in zip(columns, row, strict=True)} for row in rows[:20]
        ],
    ), ""


def synthesize(
    target: Target,
    wb: Workbook,
    file_id: str = "F1",
    answers: dict[str, str] | None = None,
    knowledge: Knowledge | None = None,
) -> SynthReport:
    answers = answers or {}
    candidates: list[Candidate] = []
    questions: list[Question] = []
    proposals = proposal_regions(wb, file_id)
    if answers.get("region"):
        proposals = [p for p in proposals if p.id == answers["region"]]
    asked: list[tuple[float, list[Question]]] = []
    notes: list[str] = []
    unmatched: list[tuple[Proposal, list[str]]] = []
    for p in proposals[:40]:
        names, types, rows = _proposal_info(p)
        mapping, scores, qs = match_columns(target, names, types, answers, knowledge)
        # Regions that match no target header at all (notes sheets) stay silent. The rest ask
        # in order of how many headers they matched, so the closest table is asked first.
        if scores:
            asked.append((-len(scores), qs))
        required = [c for c in target.columns if c.required and not c.from_header]
        missing = [c.name for c in required if c.name not in mapping and c.name not in scores]
        if missing:
            if scores:
                notes.append(f"{p.id} ({p.sheet!r}): no header matches {', '.join(missing)}.")
            else:
                unmatched.append((p, names))
            continue
        spec, reasons, grain = _build(target, p, mapping, names, types, rows)
        if spec is None:
            notes.append(f"{p.id} ({p.sheet!r}): {reasons[0]}")
            continue
        candidate, why = _evaluate(target, p, spec, reasons, scores, wb, grain)
        if candidate:
            candidates.append(candidate)
        else:
            notes.append(f"{p.id} ({p.sheet!r}) was dropped: {why}")
    candidates.sort(key=lambda c: (-c.score, c.region))
    seen: set[str] = set()
    for _, qs in sorted(asked, key=lambda x: x[0]):
        for q in qs:
            key = q.id + "|" + "|".join(q.options)
            if key not in seen:
                seen.add(key)
                questions.append(q)
    if len(proposals) > 40:
        notes.append(f"Only the first 40 of {len(proposals)} tables were tried.")
    if not candidates and not notes:
        # No table resembled the target at all: show what the first ones do contain, so the
        # user can fix the names or hints.
        for p, names in unmatched[:3]:
            shown = ", ".join(repr(n.replace("|", " / ")) for n in names[:8])
            notes.append(f"{p.id} ({p.sheet!r}) has the headers {shown}; none matched the target.")
    notes = notes[:8]
    if not candidates:
        status = "ask" if questions else "none"
        return SynthReport(status=status, questions=questions[:5], notes=notes)
    best = candidates[0]
    others = [c.model_copy(update={"preview": []}) for c in candidates[1:3]]
    if best.score >= 0.85:
        return SynthReport(status="ok", best=best, others=others, notes=notes)
    if questions:
        return SynthReport(
            status="ask", best=best, others=others, questions=questions[:5], notes=notes
        )
    return SynthReport(
        status="ask",
        best=best,
        others=others,
        notes=notes,
        questions=[
            Question(
                id="region",
                text="Which source table should be used?",
                options=[c.region for c in candidates[:3]],
            )
        ],
    )
