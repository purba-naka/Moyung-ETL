"""Run a new Excel file through saved specs, with no agent in the loop.

An agent (and a person) builds a spec once per *shape* of workbook. After that each new file
only needs `auto`: every saved spec is tried with `rebind`, the first one whose sources are all
found (exact or shifted) is run. When no spec fits, nothing is written and the report says
which spec came closest, so a person (or the agent) can repair it.

CLI: python -m excel_etl.auto <file.xlsx> <specs_dir> <out_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from excel_etl.engine import RunReport, run
from excel_etl.pipeline import parse_spec
from excel_etl.rebind import BindReport, rebind
from excel_etl.workbook import Workbook


class AutoReport(BaseModel):
    status: str  # "ok" | "failed" (spec fit, run failed) | "no_spec" (nothing fit)
    spec: str = ""  # the spec that was run, or the closest one when nothing fit
    out_dir: str = ""
    bind: str = ""
    run: RunReport | None = None
    tried: dict[str, str] = {}  # spec name -> why it was skipped

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
    file_role.
    """
    # ponytail: one file per run, all roles map to it; upgrade: a match rule per spec for
    # pipelines that join several workbooks.
    wb = Workbook.open(str(file))
    tried: dict[str, str] = {}
    best: tuple[float, str, BindReport] | None = None
    for path in sorted(Path(specs_dir).glob("*.json")):
        name = path.stem
        try:
            spec = parse_spec(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001  (a broken spec file must not stop the others)
            tried[name] = f"bad spec: {e}"
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
    sys.exit(0 if r.status == "ok" else 1)
