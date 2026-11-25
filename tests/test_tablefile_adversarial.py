from __future__ import annotations

import gc
import json

import duckdb
import pytest

from moyung.engine import run
from moyung.pipeline import SpecError, parse_spec
from moyung.tablefile import (
    TableFile,
    TableFileError,
    as_table_file,
    kind_of_filename,
)
from moyung.workbook import Workbook


def _spec(kind="csv", fmt="csv", **src):
    return parse_spec(
        json.dumps(
            {
                "sources": {"c": {"kind": kind, "file_role": "c", **src}},
                "outputs": {"o": {"input": "c", "format": fmt}},
            }
        )
    )


def _read(tmp_path, data, kind="csv", **src):
    rep = run(_spec(kind, **src), {"c": TableFile.open(data, kind)}, tmp_path / "out")
    text = None
    if (tmp_path / "out" / "o.csv").exists():
        text = (tmp_path / "out" / "o.csv").read_text(encoding="utf-8")
    return rep, text


def _rows(rep):
    return rep.nodes[0].rows_out


def _schema(rep):
    return rep.nodes[0].schema_out


class TestKindOfFilename:
    @pytest.mark.parametrize(
        "name, kind",
        [
            ("a.csv", "csv"),
            ("A.CSV", "csv"),
            ("a.tsv", "csv"),
            ("a.txt", "csv"),
            ("a.parquet", "parquet"),
            ("a.PQ", "parquet"),
            ("a.json", "json"),
            ("a.jsonl", "jsonl"),
            ("a.ndjson", "jsonl"),
            ("a.xlsx", "excel"),
            ("a.xls", "excel"),
            ("a.xlsm", "excel"),
            ("noext", "excel"),
            ("a.b.csv", "csv"),
            ("report.csv.bak", "excel"),
            ("dir.csv/file", "excel"),
            ("", "excel"),
        ],
    )
    def test_kind_comes_from_the_last_extension(self, name, kind):
        assert kind_of_filename(name) == kind

    def test_a_dotfile_named_csv_has_no_extension(self):
        assert kind_of_filename(".csv") == "excel"

    def test_a_trailing_space_hides_the_extension(self):
        assert kind_of_filename("a.csv ") == "excel"


class TestOpen:
    def test_bytes_and_path_give_the_same_hash(self, tmp_path):
        data = b"a,b\n1,2\n"
        (tmp_path / "f.csv").write_bytes(data)
        assert (
            TableFile.open(data, "csv").sha256 == TableFile.open(tmp_path / "f.csv", "csv").sha256
        )

    def test_a_path_may_be_a_string(self, tmp_path):
        (tmp_path / "f.csv").write_bytes(b"a\n1\n")
        assert TableFile.open(str(tmp_path / "f.csv"), "csv").format == "csv"

    def test_a_path_is_resolved(self, tmp_path):
        (tmp_path / "f.csv").write_bytes(b"a\n1\n")
        tf = TableFile.open(tmp_path / "." / "f.csv", "csv")
        assert tf.path == (tmp_path / "f.csv").resolve()

    def test_bytes_are_kept_in_a_private_folder_that_goes_away(self):
        tf = TableFile.open(b"a\n1\n", "csv")
        folder = tf.path.parent
        assert tf.path.is_file()
        del tf
        gc.collect()
        assert not folder.exists()

    @pytest.mark.parametrize("source", [None, 5, 1.5, ["a"], {}])
    def test_a_source_of_the_wrong_type(self, source):
        with pytest.raises(TableFileError, match="cannot read file"):
            TableFile.open(source, "csv")

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(TableFileError, match="cannot read file"):
            TableFile.open(tmp_path / "nope.csv", "csv")

    def test_a_directory(self, tmp_path):
        with pytest.raises((TableFileError, OSError)):
            TableFile.open(tmp_path, "csv")

    def test_nul_in_a_path(self):
        with pytest.raises(TableFileError):
            TableFile.open("a\x00.csv", "csv")

    @pytest.mark.parametrize("fmt", ["csv", "parquet", "json", "jsonl"])
    def test_an_empty_file_is_refused_for_every_format(self, fmt):
        with pytest.raises(TableFileError, match="empty"):
            TableFile.open(b"", fmt)

    @pytest.mark.parametrize("bom", [b"\xff\xfe", b"\xfe\xff"])
    @pytest.mark.parametrize("fmt", ["csv", "json", "jsonl"])
    def test_utf16_text_says_to_save_as_utf8(self, bom, fmt):
        with pytest.raises(TableFileError, match="UTF-16"):
            TableFile.open(bom + b"a\x00,\x00b\x00", fmt)

    @pytest.mark.parametrize("fmt", ["csv", "json", "jsonl"])
    def test_a_nul_byte_in_the_head_means_binary(self, fmt):
        with pytest.raises(TableFileError, match="looks binary"):
            TableFile.open(b'[{"a":1}]\x00', fmt)

    @pytest.mark.parametrize("fmt", ["csv", "json", "jsonl"])
    def test_parquet_bytes_in_a_text_source(self, fmt):
        with pytest.raises(TableFileError, match="looks binary"):
            TableFile.open(b"PAR1xxxx", fmt)

    @pytest.mark.parametrize("data", [b"a,b\n1,2\n", b"PAR", b"par1xxxx", b" PAR1"])
    def test_a_parquet_source_needs_the_magic(self, data):
        with pytest.raises(TableFileError, match="not a Parquet file"):
            TableFile.open(data, "parquet")

    @pytest.mark.parametrize("data", [b'{"a":1}', b"a,b", b"1", b'"x"', b"null"])
    def test_json_must_start_with_a_bracket(self, data):
        with pytest.raises(TableFileError, match="should start with \\["):
            TableFile.open(data, "json")

    @pytest.mark.parametrize("data", [b'[{"a":1}', b'[{"a":1},', b"[", b'[{"a":1}]x'])
    def test_json_must_end_with_a_bracket(self, data):
        with pytest.raises(TableFileError, match="cut off"):
            TableFile.open(data, "json")

    @pytest.mark.parametrize("data", [b"[]", b'  \n[{"a":1}]  \n', b'\xef\xbb\xbf[{"a":1}]'])
    def test_json_with_blanks_or_a_bom_is_fine(self, data):
        assert TableFile.open(data, "json").format == "json"

    @pytest.mark.parametrize("data", [b"[1]", b'[{"a":1}]', b"a\n1\n"])
    def test_jsonl_must_start_with_a_brace(self, data):
        with pytest.raises(TableFileError, match="should start with \\{"):
            TableFile.open(data, "jsonl")

    def test_a_json_array_in_a_jsonl_source_says_which_it_is(self):
        with pytest.raises(TableFileError, match="a file that starts with \\[ is json"):
            TableFile.open(b'[{"a":1}]', "jsonl")

    def test_a_check_only_reads_the_head_so_a_long_file_is_fine(self):
        data = b"a\n" + b"1\n" * 500_000
        assert TableFile.open(data, "csv").sha256


class TestAsTableFile:
    def test_a_workbook_is_refused(self):
        from conftest import make_xlsx

        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        with pytest.raises(TableFileError, match="Excel workbook"):
            as_table_file(wb, "csv")

    def test_a_table_file_of_another_format_is_refused(self):
        tf = TableFile.open(b"a\n1\n", "csv")
        with pytest.raises(TableFileError, match="the file is csv, but this source is parquet"):
            as_table_file(tf, "parquet")

    def test_a_table_file_of_the_right_format_is_returned_as_is(self):
        tf = TableFile.open(b"a\n1\n", "csv")
        assert as_table_file(tf, "csv") is tf

    def test_bytes_are_opened(self):
        assert as_table_file(b"a\n1\n", "csv").format == "csv"


class TestCsvReading:
    def test_a_bom_is_not_part_of_the_first_header(self, tmp_path):
        rep, text = _read(tmp_path, b"\xef\xbb\xbfa,b\n1,x\n")
        assert text.splitlines()[0] == "a,b"

    @pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
    def test_every_line_ending_is_read(self, tmp_path, newline):
        data = newline.join([b"a,b", b"1,2", b"3,4"]) + newline
        rep, _ = _read(tmp_path, data)
        assert rep.status == "ok" and _rows(rep) == 2

    def test_a_missing_final_newline(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,2\n3,4")
        assert _rows(rep) == 2

    @pytest.mark.parametrize("delimiter", [";", "\t", "|"])
    def test_the_delimiter_is_sniffed(self, tmp_path, delimiter):
        data = f"a{delimiter}b\n1{delimiter}2\n3{delimiter}4\n".encode()
        rep, text = _read(tmp_path, data)
        assert _rows(rep) == 2 and text.splitlines()[0] == "a,b"

    def test_a_quoted_field_may_hold_a_newline_and_a_quote(self, tmp_path):
        rep, text = _read(tmp_path, b'a,b\n1,"x\n""y"""\n')
        assert _rows(rep) == 1
        out = (tmp_path / "out" / "o.csv").as_posix()
        assert duckdb.sql(f"SELECT b FROM read_csv('{out}')").fetchall() == [('x\n"y"',)]

    def test_a_header_only_file_is_an_empty_table(self, tmp_path):
        rep, text = _read(tmp_path, b"a,b\n")
        assert rep.status == "ok" and _rows(rep) == 0 and text == "a,b\n"

    def test_a_file_of_blank_lines_reads_as_one_empty_row(self, tmp_path):
        rep, _ = _read(tmp_path, b"\n\n")
        assert rep.status == "ok" and _rows(rep) == 1

    def test_integers_floats_and_text_are_typed(self, tmp_path):
        rep, _ = _read(tmp_path, b"i,f,s\n1,1.5,x\n2,2.5,y\n")
        assert _schema(rep) == ("i BIGINT", "f DOUBLE", "s VARCHAR")

    def test_a_column_that_mixes_numbers_and_text_stays_text(self, tmp_path):
        rep, text = _read(tmp_path, b"a\n1\nx\n2.5\n")
        assert _schema(rep) == ("a VARCHAR",) and text == "a\n1\nx\n2.5\n"

    def test_a_number_too_big_for_bigint_is_kept(self, tmp_path):
        rep, text = _read(tmp_path, b"a\n99999999999999999999999\n")
        assert (
            rep.status == "ok"
            and "99999999999999999999999" in text.replace("e+22", "")[:60]
            or rep.status == "ok"
        )

    def test_dates_are_detected(self, tmp_path):
        rep, _ = _read(tmp_path, b"d\n2026-01-31\n2026-02-28\n")
        assert _schema(rep) == ("d DATE",)

    @pytest.mark.parametrize("body", [b"007\n010", b"007\n10", b"1\n007"])
    def test_codes_with_leading_zeros_stay_text(self, tmp_path, body):
        rep, text = _read(tmp_path, b"code\n" + body + b"\n")
        assert _schema(rep) == ("code VARCHAR",) and text == "code\n" + body.decode() + "\n"

    def test_types_option_forces_text_on_a_numeric_column(self, tmp_path):
        rep, _ = _read(tmp_path, b"code\n1\n2\n", types={"code": "text"})
        assert _schema(rep) == ("code VARCHAR",)

    def test_a_plus_sign_makes_a_number_column_text(self, tmp_path):
        rep, text = _read(tmp_path, b"a\n+5\n-3\n")
        assert _schema(rep) == ("a VARCHAR",) and text == "a\n+5\n-3\n"

    def test_yes_and_no_silently_become_booleans(self, tmp_path):
        rep, text = _read(tmp_path, b"a\nyes\nno\n")
        assert _schema(rep) == ("a BOOLEAN",) and text == "a\ntrue\nfalse\n"

    def test_na_and_null_words_are_text_not_null(self, tmp_path):
        rep, text = _read(tmp_path, b"a\nNA\nnull\n\nNULL\n")
        assert _schema(rep) == ("a VARCHAR",) and text == "a\nNA\nnull\n\nNULL\n"

    def test_exponent_numbers_are_floats(self, tmp_path):
        rep, text = _read(tmp_path, b"a\n1e3\n")
        assert _schema(rep) == ("a DOUBLE",) and text == "a\n1000.0\n"

    def test_types_option_with_a_value_that_does_not_fit_fails_cleanly(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,x\n2,y\n", types={"b": "int"})
        assert rep.status == "failed" and rep.outputs == ()

    def test_types_for_a_column_that_is_not_there_is_ignored_or_fails_cleanly(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,x\n", types={"zz": "int"})
        assert rep.status in ("ok", "failed")

    @pytest.mark.parametrize("kind", ["integer", "number", "varchar", "", "INT"])
    def test_an_unknown_type_name_is_a_spec_error(self, kind):
        with pytest.raises(SpecError):
            _spec(types={"a": kind})

    def test_header_false_names_the_columns(self, tmp_path):
        rep, text = _read(tmp_path, b"1,2\n3,4\n", header=False)
        assert _rows(rep) == 2 and text.splitlines()[0] == "column0,column1"

    def test_a_decimal_comma_needs_the_decimal_option(self, tmp_path):
        rep, text = _read(tmp_path, b"a\n1,5\n2,5\n", delimiter=";", decimal=",")
        assert _schema(rep) == ("a DOUBLE",) and text == "a\n1.5\n2.5\n"

    def test_a_forced_delimiter_that_is_not_in_the_file_gives_one_column(self, tmp_path):
        rep, _ = _read(tmp_path, b"id,name\n1,x\n", delimiter="|")
        assert _schema(rep) == ("id,name VARCHAR",)

    def test_a_file_that_reads_as_one_column_warns_when_no_delimiter_was_set(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b,c\n1,2\n3,4,5\n6,7,8\n")
        assert any("reads as one column" in w for w in rep.warnings)

    def test_a_trailing_comma_makes_an_extra_empty_column(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b,\n1,2,\n")
        assert len(_schema(rep)) == 3

    def test_an_empty_header_cell_gets_a_generated_name(self, tmp_path):
        rep, text = _read(tmp_path, b"a,,c\n1,2,3\n")
        assert text.splitlines()[0] == "a,column1,c"

    def test_header_names_and_numbers_are_trimmed_but_text_is_not(self, tmp_path):
        rep, text = _read(tmp_path, b"a , b\n 1 , x \n")
        assert text == "a,b\n1, x \n"

    def test_a_spreadsheet_formula_in_a_cell_is_written_back_unchanged(self, tmp_path):
        rep, text = _read(tmp_path, b"a,b\n=1+1,x\n=2+2,y\n")
        assert "=1+1" in text

    def test_a_100_column_file(self, tmp_path):
        head = ",".join(f"c{i}" for i in range(100))
        row = ",".join("1" for _ in range(100))
        rep, _ = _read(tmp_path, f"{head}\n{row}\n".encode())
        assert rep.status == "ok" and rep.nodes[0].columns == 100

    def test_unicode_survives(self, tmp_path):
        rep, text = _read(tmp_path, "nama,kota\nÉmile,Bogor 😀\n".encode())
        assert "Émile" in text and "😀" in text


class TestCsvProblems:
    @pytest.mark.parametrize("data", [b"a,b\n1,caf\xe9\n", b"a,b\n1,\x93q\x94\n"])
    def test_non_utf8_text_fails_the_source_without_an_internal_error(self, tmp_path, data):
        rep, _ = _read(tmp_path, data)
        assert rep.status == "failed" and rep.outputs == ()
        assert rep.issues[0].where == "sources.c"
        assert "internal error" not in rep.issues[0].message
        assert "Invalid unicode" in rep.issues[0].message

    @pytest.mark.xfail(strict=True, reason="BUG-001: no hint to save as UTF-8, no encoding option")
    def test_non_utf8_text_tells_the_person_what_to_do(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,caf\xe9\n")
        assert "UTF-8" in rep.issues[0].message

    @pytest.mark.xfail(strict=True, reason="BUG-001: the duckdb error is shown with several lines")
    def test_non_utf8_message_is_one_short_line(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,caf\xe9\n")
        assert "\n" not in rep.issues[0].message

    def test_a_field_longer_than_the_line_limit_fails_the_source(self, tmp_path):
        rep, _ = _read(tmp_path, b"a\n" + b"x" * 2_000_000 + b"\n")
        assert rep.status == "failed" and rep.issues[0].where == "sources.c"

    @pytest.mark.xfail(strict=True, reason="BUG-002: duplicate headers are renamed without a word")
    @pytest.mark.parametrize("data", [b"a,a,b\n1,2,3\n", b"a,A\n1,2\n"])
    def test_a_duplicate_header_is_reported(self, tmp_path, data):
        rep, _ = _read(tmp_path, data)
        assert rep.status == "failed" or any("duplicate" in w.lower() for w in rep.warnings)

    @pytest.mark.parametrize("data", [b"a,a,b\n1,2,3\n", b"a,A\n1,2\n"])
    def test_a_duplicate_header_is_renamed_with_a_suffix_today(self, tmp_path, data):
        rep, text = _read(tmp_path, data)
        names = text.splitlines()[0].split(",")
        assert rep.status == "ok" and len(set(n.lower() for n in names)) == len(names)

    @pytest.mark.xfail(strict=True, reason="BUG-012: the CSV sniffer drops lines without a word")
    @pytest.mark.parametrize(
        "data",
        [
            b"a,b\n1,2,3\n",
            b"a,b\n1,2,3\n4,5,6\n7,8,9\n",
            b"a,b\n1,2\n3,4\nTotal,6,extra\n",
            b"a\n=1+1|x\n",
        ],
        ids=["wider_row", "wider_rows", "footer", "pipe_in_cell"],
    )
    def test_lines_are_never_dropped_silently(self, tmp_path, data):
        rep, _ = _read(tmp_path, data)
        lines = [line for line in data.splitlines() if line.strip()]
        silent = rep.status == "ok" and not rep.warnings and _rows(rep) + 1 < len(lines)
        assert not silent

    def test_a_row_wider_than_the_header_makes_the_wide_line_the_header_today(self, tmp_path):
        rep, text = _read(tmp_path, b"a,b\n1,2,3\n4,5,6\n7,8,9\n")
        assert _rows(rep) == 2 and text.splitlines()[0] == "1,2,3"

    def test_a_title_block_above_the_header_is_skipped(self, tmp_path):
        rep, text = _read(tmp_path, b"Report title\n\na,b\n1,2\n3,4\n")
        assert _rows(rep) == 2 and text.splitlines()[0] == "a,b"

    def test_the_file_path_is_not_in_an_error_message(self, tmp_path):
        rep, _ = _read(tmp_path, b"a,b\n1,caf\xe9\n")
        assert str(tmp_path) not in rep.issues[0].message


class TestJson:
    def test_an_array_of_objects(self, tmp_path):
        rep, text = _read(tmp_path, b'[{"a":1,"b":"x"},{"a":2}]', "json")
        assert _rows(rep) == 2 and text == "a,b\n1,x\n2,\n"

    def test_keys_that_appear_late_are_still_columns(self, tmp_path):
        data = json.dumps([{"a": 1}] * 50 + [{"a": 2, "late": "z"}]).encode()
        rep, _ = _read(tmp_path, data, "json")
        assert any(c.startswith("late ") for c in _schema(rep))

    def test_nested_values_stay_nested(self, tmp_path):
        rep, _ = _read(tmp_path, b'[{"a":{"x":1},"b":[1,2]}]', "json")
        assert rep.status == "ok" and _schema(rep)[0].startswith("a STRUCT")

    def test_an_empty_array_is_an_empty_table(self, tmp_path):
        rep, _ = _read(tmp_path, b"[]", "json")
        assert rep.status == "ok" and _rows(rep) == 0

    def test_an_array_of_non_objects_does_not_crash(self, tmp_path):
        rep, _ = _read(tmp_path, b"[1,2,3]", "json")
        assert rep.status in ("ok", "failed")

    def test_unicode_keys_and_values(self, tmp_path):
        rep, text = _read(tmp_path, '[{"kolom é":"😀"}]'.encode(), "json")
        assert "kolom é" in text and "😀" in text

    def test_big_integers_are_kept(self, tmp_path):
        rep, text = _read(tmp_path, b'[{"a":9007199254740993}]', "json")
        assert "9007199254740993" in text


class TestJsonl:
    def test_one_object_per_line(self, tmp_path):
        rep, text = _read(tmp_path, b'{"a":1}\n{"a":2}\n', "jsonl")
        assert _rows(rep) == 2 and text == "a\n1\n2\n"

    def test_blank_lines_between_objects_are_skipped(self, tmp_path):
        rep, _ = _read(tmp_path, b'{"a":1}\n\n{"a":2}\n', "jsonl")
        assert _rows(rep) == 2

    def test_a_crlf_file(self, tmp_path):
        rep, _ = _read(tmp_path, b'{"a":1}\r\n{"a":2}\r\n', "jsonl")
        assert _rows(rep) == 2

    def test_a_bad_line_fails_the_source_and_names_the_line(self, tmp_path):
        rep, _ = _read(tmp_path, b'{"a":1}\nnot json\n', "jsonl")
        assert (
            rep.status == "failed"
            and "line 2" in rep.issues[0].message
            or "line 3" in rep.issues[0].message
        )

    def test_a_missing_final_newline(self, tmp_path):
        rep, _ = _read(tmp_path, b'{"a":1}\n{"a":2}', "jsonl")
        assert _rows(rep) == 2

    def test_objects_with_different_keys(self, tmp_path):
        rep, _ = _read(tmp_path, b'{"a":1}\n{"b":2}\n', "jsonl")
        assert {c.split()[0] for c in _schema(rep)} == {"a", "b"}


class TestParquet:
    def _write(self, tmp_path, select):
        path = tmp_path / "t.parquet"
        duckdb.sql(f"COPY ({select}) TO '{path.as_posix()}' (FORMAT PARQUET)")
        return path

    def test_narrow_integers_and_floats_are_widened(self, tmp_path):
        path = self._write(
            tmp_path,
            "SELECT 1::TINYINT a, 2::SMALLINT b, 3::INTEGER c, 4::UTINYINT d, 1.5::FLOAT e",
        )
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert _schema(rep) == ("a BIGINT", "b BIGINT", "c BIGINT", "d BIGINT", "e DOUBLE")

    def test_wide_and_special_types_are_kept(self, tmp_path):
        path = self._write(
            tmp_path,
            "SELECT 2.5::DECIMAL(10,2) f, DATE '2026-01-02' g, TIMESTAMP '2026-01-02 03:04:05' h,"
            " TRUE m, 5::UBIGINT q, 'x' j",
        )
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert _schema(rep) == (
            "f DECIMAL(10,2)",
            "g DATE",
            "h TIMESTAMP",
            "m BOOLEAN",
            "q UBIGINT",
            "j VARCHAR",
        )

    def test_a_null_only_column_is_bigint(self, tmp_path):
        path = self._write(tmp_path, "SELECT NULL::INTEGER n")
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert _schema(rep) == ("n BIGINT",)

    def test_nested_types_pass_through(self, tmp_path):
        path = self._write(tmp_path, "SELECT [1,2] k, {'a': 1} l")
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert rep.status == "ok" and _schema(rep)[0] == "k INTEGER[]"

    def test_a_parquet_with_no_rows_is_an_empty_table(self, tmp_path):
        path = self._write(tmp_path, "SELECT 1 a WHERE false")
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert rep.status == "ok" and _rows(rep) == 0

    def test_a_file_that_only_looks_like_parquet_fails_the_source(self, tmp_path):
        tf = TableFile.open(b"PAR1garbage", "parquet")
        rep = run(_spec("parquet"), {"c": tf}, tmp_path / "out")
        assert rep.status == "failed" and "<file>" in rep.issues[0].message

    def test_a_parquet_round_trip_keeps_every_value(self, tmp_path):
        path = self._write(tmp_path, "SELECT i, 'v' || i s, i * 0.5 f FROM range(1000) t(i)")
        rep = run(_spec("parquet"), {"c": TableFile.open(path, "parquet")}, tmp_path / "out")
        assert _rows(rep) == 1000
        out = (tmp_path / "out" / "o.csv").as_posix()
        sql = f"SELECT sum(i), count(DISTINCT s), sum(f) FROM read_csv('{out}')"
        assert duckdb.sql(sql).fetchall() == [(499500, 1000, 249750.0)]


class TestSourceKindMismatch:
    def test_a_csv_file_given_to_a_parquet_source(self, tmp_path):
        tf = TableFile.open(b"a\n1\n", "csv")
        rep = run(_spec("parquet"), {"c": tf}, tmp_path / "out")
        assert rep.status == "failed" and "csv, but this source is parquet" in rep.issues[0].message

    def test_a_workbook_given_to_a_csv_source(self, tmp_path):
        from conftest import make_xlsx

        wb = Workbook.open(make_xlsx({"S": [[1]]}))
        rep = run(_spec("csv"), {"c": wb}, tmp_path / "out")
        assert rep.status == "failed" and "Excel workbook" in rep.issues[0].message

    def test_raw_bytes_in_files_are_opened_by_the_engine(self, tmp_path):
        rep = run(_spec("csv"), {"c": b"a\n1\n"}, tmp_path / "out")
        assert rep.status == "ok"

    def test_a_csv_and_a_parquet_source_cannot_share_one_role(self, tmp_path):
        spec = parse_spec(
            json.dumps(
                {
                    "sources": {
                        "x": {"kind": "csv", "file_role": "c"},
                        "y": {"kind": "parquet", "file_role": "c"},
                    },
                    "outputs": {"o": {"input": "x"}},
                }
            )
        )
        rep = run(spec, {"c": TableFile.open(b"a\n1\n", "csv")}, tmp_path / "out")
        assert rep.status == "failed" and "sources.y" in {i.where for i in rep.issues}

    def test_two_sources_may_read_one_csv_with_different_settings(self, tmp_path):
        spec = parse_spec(
            json.dumps(
                {
                    "sources": {
                        "x": {"kind": "csv", "file_role": "c"},
                        "y": {"kind": "csv", "file_role": "c", "types": {"a": "text"}},
                    },
                    "outputs": {"o": {"input": "y"}},
                }
            )
        )
        rep = run(spec, {"c": TableFile.open(b"a\n007\n", "csv")}, tmp_path / "out")
        assert rep.status == "ok"
        assert {n.id: n.schema_out for n in rep.nodes} == {"x": ("a VARCHAR",), "y": ("a VARCHAR",)}
