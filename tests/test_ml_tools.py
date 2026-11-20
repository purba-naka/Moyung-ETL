from __future__ import annotations

import json

import duckdb
import pytest
from conftest import make_xlsx

from moyung.state import Job, new_state
from moyung.tools import JobTools


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


def _train():
    lines = ["id,age,seg,churn,const"]
    for i in range(60):
        age = "" if i % 10 == 0 else str(20 + i % 30)
        lines.append(f"{i},{age},{'AB'[i % 2]},{'yes' if i % 4 == 0 else 'no'},k")
    return ("\n".join(lines) + "\n").encode()


NEW = b"id,age,seg,const\n1,25,A,k\n2,,B,k\n3,40,Z,k\n"
ML = {
    "name": "m",
    "input": "t",
    "target": "churn",
    "features": ["age", "seg"],
    "ids": ["id"],
    "exclude": ["const"],
    "split": {"strategy": "random", "test_size": 0.2, "seed": 3},
    "missing": {"age": {"strategy": "median"}},
    "numerical": {"age": {"strategy": "standard_scale"}},
    "categorical": {"seg": {"strategy": "one_hot", "unknown": "ignore"}},
}


@pytest.fixture
def tools(tmp_path):
    blobs = {"a": _train(), "b": NEW, "x": make_xlsx({"S": [["h"], [1]]})}
    t = JobTools(Job(new_state("j1")), Provider(blobs), tmp_path / "out")
    for fid, name, role in (("a", "t.csv", "t"), ("b", "new.csv", "n"), ("x", "b.xlsx", "x")):
        assert "registered" in t.add_file(fid, name, role)["text"]
    return t


def _run(t):
    spec = {
        "sources": {"t": {"kind": "csv", "file_role": "t"}},
        "ml": ML,
        "outputs": {"tr": {"input": "m_train"}, "te": {"input": "m_test"}},
    }
    assert t.save_spec(json.dumps(spec), "")["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    assert t.rebind_sources()["ok"]
    assert t.run_pipeline()["ok"]


def test_ml_candidates_profiles_a_file_and_the_target(tools):
    text = tools.ml_candidates("F1", "churn")["text"]
    assert "60 rows|5 columns" in text
    assert "id|BIGINT|0|60|almost every value differs" in text
    assert "const|VARCHAR|0|1|constant" in text
    assert "age|BIGINT|6|" in text and "has empty values" in text
    assert "classes false=45, true=15" in text


def test_ml_candidates_reads_a_run_output_and_refuses_bad_input(tools):
    assert "run_pipeline first" in tools.ml_candidates("tr")["error"]
    _run(tools)
    assert "column|type|empty|distinct|notes" in tools.ml_candidates("tr", "churn")["text"]
    assert "no column 'zzz'" in tools.ml_candidates("F1", "zzz")["error"]
    assert "workbook" in tools.ml_candidates("F3")["error"]


def test_apply_ml_preprocessing_prepares_new_rows_with_train_numbers(tools, tmp_path):
    assert "no preprocessing_metadata" in tools.apply_ml_preprocessing("F2", "fresh")["error"]
    _run(tools)
    res = tools.apply_ml_preprocessing("F2", "fresh")
    assert res["ok"] and "prepared 3 of 3 rows" in res["text"]
    assert "not seen in train" in res["text"]
    path = tmp_path / "out" / "prepared" / "fresh.parquet"
    rows = duckdb.connect().execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
    assert len(rows) == 3
    again = tools.apply_ml_preprocessing("F2", "fresh", format="csv")
    assert again["ok"] and (tmp_path / "out" / "prepared" / "fresh.csv").is_file()


def test_apply_ml_preprocessing_refuses_bad_names_and_workbooks(tools):
    _run(tools)
    assert "plain table name" in tools.apply_ml_preprocessing("F2", "../x")["error"]
    assert "workbook" in tools.apply_ml_preprocessing("F3", "fresh")["error"]


def test_apply_ml_preprocessing_reports_missing_columns_as_an_error_with_advice(tools):
    _run(tools)
    tools.provider.blobs["c"] = b"x,y\n1,2\n"
    assert "error" not in tools.add_file("c", "bad.csv", "bad")
    res = tools.apply_ml_preprocessing("F4", "fresh")
    assert "is missing" in res["error"] and "known only after the outcome" in res["error"]


def test_apply_ml_preprocessing_validates_format_and_hides_paths(tools, tmp_path):
    _run(tools)
    assert "parquet or csv" in tools.apply_ml_preprocessing("F2", "x", format="xlsx")["error"]
    ok = tools.apply_ml_preprocessing("F2", "fresh")
    assert str(tmp_path) not in ok["text"] and "prepared/fresh.parquet" in ok["text"]


def test_a_metadata_path_outside_the_output_folder_is_refused(tools, tmp_path):
    _run(tools)
    other = tmp_path / "elsewhere"
    other.mkdir()
    stray = other / "preprocessing_metadata.json"
    stray.write_text("{}")
    last = tools.job.state.last_run
    tools.job.update(
        lambda st: st.model_copy(
            update={"last_run": last.model_copy(update={"artifacts": (str(stray),)})}
        )
    )
    assert "outside the output folder" in tools.apply_ml_preprocessing("F2", "fresh")["error"]


def test_ml_candidates_pages_validates_and_accepts_a_file_name(tools):
    first = tools.ml_candidates("t.csv", limit=2)
    assert first["truncated"] and first["next_offset"] == 2
    assert "columns 1-2" in first["text"] and "seg|" not in first["text"].split("suggested")[0]
    last = tools.ml_candidates("F1", offset=4, limit=2)
    assert not last["truncated"] and last["next_offset"] is None
    for bad in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"offset": "2"}):
        assert "offset must be" in tools.ml_candidates("F1", **bad)["error"]


def test_ml_candidates_says_why_a_one_value_column_matters_and_suggests_roles(tools):
    text = tools.ml_candidates("F1", "churn")["text"]
    assert "suggested from the notes" in text and "ids: id" in text and "exclude: const" in text
    rows = "\n".join(f"{'x' if i % 3 == 0 else ''},{i % 3 == 0}" for i in range(30))
    tools.provider.blobs["d"] = ("tag,out\n" + rows).encode()
    tools.add_file("d", "d.csv", "d")
    assert "may reveal the target" in tools.ml_candidates("F4", "out")["text"]


def test_ml_candidates_describes_a_number_target_and_an_empty_one(tools):
    rows = "\n".join(f"{i},{i * 1.5}" for i in range(30))
    tools.provider.blobs["d"] = ("v,y\n" + rows).encode()
    tools.add_file("d", "d.csv", "d")
    assert "number to estimate" in tools.ml_candidates("F4", "y")["text"]
    tools.provider.blobs["e"] = b"v,y\n1,\n2,\n"
    tools.add_file("e", "e.csv", "e")
    assert "no values" in tools.ml_candidates("F5", "y")["text"]


def test_ml_candidates_reads_a_parquet_output(tools):
    _run(tools)
    assert "rows|" in tools.ml_candidates("tr", "churn")["text"]
