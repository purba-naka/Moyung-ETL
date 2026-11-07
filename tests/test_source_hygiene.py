"""Whole-repo checks that a green pytest run can hide (a cached .pyc hides a bad escape)."""

from __future__ import annotations

import warnings
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_every_python_file_compiles_without_warnings():
    bad = []
    for path in sorted([*ROOT.glob("excel_etl/**/*.py"), *ROOT.glob("tests/*.py")]):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        bad += [f"{path.relative_to(ROOT)}:{w.lineno}: {w.message}" for w in caught]
    assert not bad, "\n".join(bad)
