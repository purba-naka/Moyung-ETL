"""Run a new Excel file through saved specs, with no agent in the loop.

An agent (and a person) builds a spec once per *shape* of workbook. After that each new file
only needs `auto`: every saved spec is tried with `rebind`, the first one whose sources are all
found (exact or shifted) is run. When no spec fits, nothing is written and the report says
which spec came closest, so a person (or the agent) can repair it.

The CLI exits 0 when every output was written, 2 when a failed check held some outputs back
(partial), and 1 for anything else.

CLI: python -m moyung.auto <file.xlsx> <specs_dir> <out_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from moyung.engine import RunReport, run
from moyung.pipeline import SpecError, parse_spec
from moyung.rebind import BindReport, rebind
from moyung.workbook import Workbook, WorkbookError


class AutoReport(BaseModel):
    status: str
    spec: str = ""
    out_dir: str = ""
    bind: str = ""
    run: RunReport | None = None
    tried: dict[str, str] = {}

    def text(self) -> str:
        lines = [f"auto: {self.status}" + (f" with spec {self.spec!r}" if self.spec else "")]
        lines += [f"  skipped {k}: {v}" for k, v in self.tried.items()]
        if self.bind:
            lines.append(self.bind)
        if self.run:
            lines.append(self.run.text())
        return "\n".join(lines)


def _found(b: BindReport) -> int:
    return sum(s.status in ("exact", "shifted") for s in b.sources.values())


def auto(file: str | Path, specs_dir: str | Path, out_dir: str | Path) -> AutoReport:
    """Try every `specs_dir/*.json` on `file` (in name order); run the first that fits into
    `out_dir/<spec>/<file stem>/`. Every source of a spec reads this one file, whatever its
    file_role. A file that cannot be opened as a workbook gives status `unreadable`.
    """
    try:
        wb = Workbook.open(str(file))
    except WorkbookError as e:
        return AutoReport(status="unreadable", tried={"-": f"cannot open {Path(file).name}: {e}"})
    tried: dict[str, str] = {}
    best: tuple[float, str, BindReport] | None = None
    for path in sorted(Path(specs_dir).glob("*.json")):
        name = path.stem
        try:
            spec = parse_spec(path.read_text(encoding="utf-8"))
        except (SpecError, OSError, UnicodeDecodeError) as e:
            tried[name] = f"bad spec: {e}"
            continue
        if not spec.sources:
            tried[name] = "bad spec: it has no sources, so it fits every file"
            continue
        files = {s.file_role: wb for s in spec.sources.values()}
        b = rebind(spec, files)
        if not b.ok:
            tried[name] = f"{_found(b)}/{len(b.sources)} sources found"
            score = _found(b) / max(len(b.sources), 1)
            if best is None or score > best[0]:
                best = (score, name, b)
            continue
        target = Path(out_dir) / name / Path(file).stem
        rep = run(spec, files, target)
        return AutoReport(
            status=rep.status, spec=name, out_dir=str(target), bind=b.text(), run=rep, tried=tried
        )
    if best is None:
        tried = tried or {"-": f"no saved spec in {specs_dir}; publish a job's spec first"}
        return AutoReport(status="no_spec", tried=tried)
    return AutoReport(status="no_spec", spec=best[1], bind=best[2].text(), tried=tried)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__.strip().splitlines()[-1])
    r = auto(*sys.argv[1:])
    print(r.text())
    sys.exit({"ok": 0, "partial": 2}.get(r.status, 1))
