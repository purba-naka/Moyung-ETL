from __future__ import annotations

import hashlib
import json

import duckdb
import pytest
from conftest import make_xlsx

from excel_etl.engine import run, validate
from excel_etl.pipeline import SpecError, parse_spec
from excel_etl.rebind import rebind
from excel_etl.state import Job, new_state
from excel_etl.tablefile import TableFile, TableFileError, kind_of_filename
from excel_etl.tools import JobTools
from excel_etl.workbook import Workbook

CSV = "item,qty,price\npen,2,1.5\nink,1,2.0\npen,5,1.5\n"


@pytest.fixture
def parquet(tmp_path):
    path = tmp_path / "p.parquet"
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT 'pen' AS item, 3::INTEGER AS qty, 1.5::FLOAT AS price "
        f"UNION ALL SELECT 'pad', 4, 2.0) TO '{path.as_posix()}' (FORMAT parquet)"
    )
    return path


@pytest.fixture
def csv(tmp_path):
    path = tmp_path / "s.csv"
    path.write_bytes(CSV.encode())
    return path


def _spec(**extra):
    base = {
        "sources": {"c": {"kind": "csv", "file_role": "c"}},
        "outputs": {"o": {"input": "c", "format": "csv"}},
    }
    return parse_spec(json.dumps({**base, **extra}))


def _excel_source(role="c"):
    return {"file_role": role, "sheet": {"name": "A"}, "region": {"header_anchor": ["x"]}}


def test_csv_source_reads_with_engine_types(csv):
    rep = validate(_spec(), {"c": csv})
    assert rep.ok
    assert [(c.name, c.type) for c in rep.schemas["c"]] == [
        ("item", "VARCHAR"),
        ("qty", "BIGINT"),
        ("price", "DOUBLE"),
    ]


def test_parquet_narrow_types_read_as_engine_types_so_a_union_lines_up(csv, parquet):
    spec = parse_spec(
        json.dumps(
            {
                "sources": {
                    "c": {"kind": "csv", "file_role": "c"},
                    "p": {"kind": "parquet", "file_role": "p"},
                },
                "steps": [{"id": "u", "node": "union", "inputs": ["c", "p"]}],
                "outputs": {"o": {"input": "u", "format": "csv"}},
            }
        )
    )
    rep = run(spec, {"c": csv, "p": parquet.read_bytes()}, parquet.parent / "out")
    assert rep.status == "ok", rep.issues
    assert rep.outputs[0].rows == 5
    assert [n.node for n in rep.nodes if n.kind == "source"] == ["read_csv", "read_parquet"]


def test_csv_delimiter_and_header_options(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text("1;a\n2;b\n")
    source = {"kind": "csv", "file_role": "c", "delimiter": ";", "header": False}
    rep = validate(_spec(sources={"c": source}), {"c": path})
    assert rep.ok and [c.name for c in rep.schemas["c"]] == ["column0", "column1"]


def test_expect_columns_works_for_a_csv(csv):
    expect = [{"match": "qty", "type": "text"}, {"match": "gone"}]
    spec = _spec(sources={"c": {"kind": "csv", "file_role": "c", "expect_columns": expect}})
    messages = " ".join(i.message for i in validate(spec, {"c": csv}).issues)
    assert "qty" in messages and "says text" in messages and "'gone' not found" in messages


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "csv", "file_role": "c", "sheet": {"name": "A"}},
        {"kind": "parquet", "file_role": "c", "region": {"header_anchor": ["x"]}},
        {"kind": "parquet", "file_role": "c", "delimiter": ";"},
        {"kind": "excel", "file_role": "c"},
        {**_excel_source(), "delimiter": ";"},
    ],
)
def test_source_fields_must_fit_the_kind(source):
    with pytest.raises(SpecError):
        parse_spec(json.dumps({"sources": {"c": source}, "outputs": {}}))


def test_kind_mismatch_and_bad_files_are_issues_not_exceptions(parquet, tmp_path):
    spec = _spec()

    def first(file):
        return validate(spec, {"c": file}).issues[0].message

    assert "is parquet" in first(TableFile.open(parquet, "parquet"))
    assert "binary" in first(parquet)
    assert "Excel workbook" in first(Workbook.open(make_xlsx({"A": [[1]]})))
    assert "empty" in first(b"")
    assert "cannot read" in first(tmp_path / "nope.csv")
    assert "no file for file_role" in validate(spec, {}).issues[0].message


def test_an_excel_source_on_a_csv_file_is_an_issue(csv):
    spec = parse_spec(json.dumps({"sources": {"s": _excel_source()}, "outputs": {}}))
    rep = validate(spec, {"c": TableFile.open(csv, "csv")})
    assert not rep.ok and "csv file, but this source reads Excel" in rep.issues[0].message


def test_a_run_cannot_read_a_file_the_spec_did_not_name(csv, tmp_path):
    other = tmp_path / "secret.csv"
    other.write_text("a\n1\n")
    where = f"\"qty\" IN (SELECT a FROM read_csv('{other.as_posix()}'))"
    spec = _spec(
        steps=[{"id": "x", "node": "filter_rows", "inputs": ["c"], "config": {"where": where}}]
    )
    assert not validate(spec, {"c": csv}).ok


def test_table_file_hash_is_the_content_hash(csv):
    want = hashlib.sha256(CSV.encode()).hexdigest()
    assert TableFile.open(csv, "csv").sha256 == want
    assert TableFile.open(CSV.encode(), "csv").sha256 == want


def test_kind_of_filename():
    names = ("a.CSV", "b.tsv", "c.parquet", "d.xlsx", "e")
    assert [kind_of_filename(n) for n in names] == ["csv", "csv", "parquet", "excel", "excel"]


def test_rebind_judges_columns_and_types_of_a_csv(tmp_path):
    spec = _spec()
    first = tmp_path / "a.csv"
    first.write_bytes(CSV.encode())
    prev = rebind(spec, {"c": first}).records()
    assert rebind(spec, {"c": first}, prev).ok
    typed = tmp_path / "b.csv"
    typed.write_text("item,qty,price\npen,many,1.5\n")
    rep = rebind(spec, {"c": typed}, prev)
    assert not rep.ok and "was BIGINT, is now VARCHAR" in rep.text()
    added = tmp_path / "c.csv"
    added.write_text("item,qty,price,extra\npen,1,1.5,x\n")
    assert "columns added" in rebind(spec, {"c": added}, prev).text()
    assert rebind(spec, {}, prev).sources["c"].status == "missing"


class Provider:
    def __init__(self, blobs):
        self.blobs = blobs

    def open(self, file_id):
        return self.blobs[file_id]


def _tools(tmp_path, blobs, **files):
    t = JobTools(Job(new_state("j1")), Provider(blobs), tmp_path / "out")
    for fid, (name, role) in files.items():
        assert "registered" in t.add_file(fid, name, role)["text"]
    return t


def test_tools_register_csv_and_parquet_and_show_their_kind(tmp_path, parquet):
    blobs = {"a": CSV.encode(), "b": parquet.read_bytes(), "x": make_xlsx({"S": [["h"], [1]]})}
    t = _tools(
        tmp_path,
        blobs,
        a=("sales.csv", "c"),
        b=("p.parquet", "p"),
        x=("book.xlsx", "x"),
    )
    rows = t.list_files()["text"].splitlines()
    assert [r.split("|")[-1] for r in rows] == ["csv", "parquet", "excel"]


def test_workbook_map_of_a_csv_gives_columns_rows_and_the_source(tmp_path):
    t = _tools(tmp_path, {"a": CSV.encode()}, a=("sales.csv", "c"))
    text = t.workbook_map("F1")["text"]
    assert "F1|sales.csv|csv|3 rows|3 columns" in text
    assert "qty BIGINT" in text and "pen|2|1.5" in text
    assert 'source: {"kind":"csv","file_role":"c"' in text


def test_sheet_tools_refuse_a_csv_with_advice(tmp_path):
    t = _tools(tmp_path, {"a": CSV.encode()}, a=("sales.csv", "c"))
    res = t.sheet_regions("F1", "S")
    assert "no sheets or regions" in res["error"] and "workbook_map" in res["error"]


def test_full_csv_flow_through_the_tools(tmp_path):
    t = _tools(tmp_path, {"a": CSV.encode()}, a=("sales.csv", "c"))
    aggs = [{"name": "n", "fn": "sum", "column": "qty"}]
    spec = {
        "sources": {"sales": {"kind": "csv", "file_role": "c"}},
        "steps": [
            {
                "id": "by_item",
                "node": "group_by",
                "inputs": ["sales"],
                "config": {"by": ["item"], "aggs": aggs},
            }
        ],
        "outputs": {"totals": {"input": "by_item"}},
        "checks": {"rows": {"kind": "row_count", "table": "sales", "min": 1}},
    }
    assert t.save_spec(json.dumps(spec), "")["saved"]
    t.approve("after_propose_datasets", "dina", "v1")
    assert t.rebind_sources()["ok"]
    res = t.run_pipeline()
    assert res["ok"], res["text"]
    assert (tmp_path / "out" / "totals.parquet").exists()


def test_content_that_does_not_fit_the_extension_is_refused_at_registration(tmp_path):
    t = JobTools(Job(new_state("j1")), Provider({"a": b"\x00\x01", "b": b"text"}), tmp_path)
    assert "cannot be opened" in t.add_file("a", "sales.csv", "c")["error"]
    assert "cannot be opened" in t.add_file("b", "p.parquet", "p")["error"]


def test_old_state_without_file_kind_still_loads():
    state = new_state("j")
    raw = json.loads(state.model_dump_json())
    raw["files"] = [{"id": "f", "filename": "a.xlsx", "sha256": "0" * 64}]
    assert type(state).model_validate(raw).files[0].kind == "excel"


def test_a_directory_is_not_a_table_file(tmp_path):
    with pytest.raises(TableFileError):
        TableFile.open(tmp_path, "csv")


def test_two_sources_on_one_role_with_different_kinds_name_the_mismatch(csv):
    sources = {
        "a": {"kind": "csv", "file_role": "r"},
        "b": {"kind": "parquet", "file_role": "r"},
    }
    spec = _spec(sources=sources, outputs={"o": {"input": "a"}})
    issues = validate(spec, {"r": csv}).issues
    assert [i.where for i in issues] == ["sources.b"]
    assert "is csv, but this source is parquet" in issues[0].message


def test_utf16_csv_is_named_not_called_binary():
    with pytest.raises(TableFileError, match="UTF-16"):
        TableFile.open("a,b\n1,2\n".encode("utf-16"), "csv")


def test_a_csv_whose_delimiter_was_not_detected_warns(tmp_path):
    path = tmp_path / "single.csv"
    path.write_bytes(b"a;b\n1\n1;2;3\n")
    warnings = validate(_spec(), {"c": path}).warnings
    assert all("set `delimiter`" in w for w in warnings if "delimiter" in w)


def test_workbook_map_of_a_csv_shows_delimiter_and_the_last_rows(tmp_path):
    rows = "item;qty\n" + "".join(f"r{i};{i}\n" for i in range(20)) + "Total;190\n"
    t = _tools(tmp_path, {"a": rows.encode()}, a=("big.csv", "c"))
    text = t.workbook_map("F1")["text"]
    assert "delimiter ';' (detected" in text
    assert "last rows" in text and "Total|190" in text
    assert '"delimiter":";"' in text


def test_the_default_file_kind_is_not_written_to_state():
    from excel_etl.state import add_file

    state = new_state("j")
    filled = add_file(state, "f", "a.csv", "0" * 64, "c", "csv")
    assert '"kind":"csv"' in filled.model_dump_json()
    assert type(state).model_validate_json(filled.model_dump_json()).files[0].kind == "csv"
    plain = add_file(state, "g", "a.xlsx", "0" * 64, "x")
    assert '"kind"' not in plain.model_dump_json()


def test_parquet_decimal_and_timestamp_variants_satisfy_expect_columns():
    from excel_etl.nodes.source import type_fits

    assert type_fits("float", "DECIMAL(18,3)") and type_fits("datetime", "TIMESTAMP_NS")
    assert not type_fits("int", "DECIMAL(18,3)") and not type_fits("text", "DECIMAL(18,3)")


TOTAL_CSV = b"order;amt\n1;1,5\n2;2,5\nTotal;4,0\n"


def _typed_spec(**settings):
    source = {"kind": "csv", "file_role": "c", "delimiter": ";", **settings}
    return _spec(sources={"c": source})


def test_a_total_row_makes_a_column_text_until_types_says_otherwise(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(TOTAL_CSV)
    schema = validate(_typed_spec(), {"c": path}).schemas["c"]
    assert [(c.name, c.type) for c in schema] == [("order", "VARCHAR"), ("amt", "VARCHAR")]
    typed = _typed_spec(decimal=",", types={"order": "text"})
    schema = validate(typed, {"c": path}).schemas["c"]
    assert [(c.name, c.type) for c in schema] == [("order", "VARCHAR"), ("amt", "DOUBLE")]


def test_types_pin_a_column_so_a_file_without_the_total_row_keeps_the_same_type(tmp_path):
    spec = _typed_spec(types={"order": "text"})
    with_total, without = tmp_path / "a.csv", tmp_path / "b.csv"
    with_total.write_bytes(TOTAL_CSV)
    without.write_bytes(b"order;amt\n1;x\n2;y\n")
    first = validate(spec, {"c": with_total}).schemas["c"]
    second = validate(spec, {"c": without}).schemas["c"]
    assert first[0].type == second[0].type == "VARCHAR"


def test_types_naming_a_missing_column_is_an_issue_not_an_exception(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(TOTAL_CSV)
    rep = validate(_typed_spec(types={"nope": "text"}), {"c": path})
    assert not rep.ok and "nope" in rep.issues[0].message


def test_types_that_cannot_hold_the_values_are_an_issue(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes(TOTAL_CSV)
    rep = validate(_typed_spec(types={"order": "int"}), {"c": path})
    assert not rep.ok and "order" in rep.issues[0].message


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "parquet", "file_role": "c", "types": {"a": "text"}},
        {"kind": "parquet", "file_role": "c", "decimal": ","},
        {**_excel_source(), "decimal": ","},
        {"kind": "csv", "file_role": "c", "types": {"a": "money"}},
        {"kind": "csv", "file_role": "c", "decimal": ";"},
    ],
)
def test_csv_only_settings_and_their_values_are_checked(source):
    with pytest.raises(SpecError):
        parse_spec(json.dumps({"sources": {"c": source}, "outputs": {}}))


def test_defaults_of_the_new_settings_are_left_out_of_the_saved_spec():
    from excel_etl.pipeline import dump_spec

    plain = dump_spec(_spec())
    assert '"types"' not in plain and '"decimal"' not in plain and '"header"' not in plain
    set_ = dump_spec(_typed_spec(decimal=",", types={"a": "text"}))
    assert '"decimal": ","' in set_ and '"types"' in set_
    assert parse_spec(set_) == _typed_spec(decimal=",", types={"a": "text"})
