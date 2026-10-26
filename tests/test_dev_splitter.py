"""Developer unit tests for the restore splitter and util helpers (no database needed)."""
import io

import pytest

from engine.restore import split_sql
from engine.util import CycleError, RestoreError, lit, stmt, topo_sort


def split(text: str):
    out = []
    for st in split_sql(io.BytesIO(text.encode("utf-8"))):
        data = b"".join(st.data) if st.data is not None else None
        out.append((st.sql, st.kind, st.label_kind, st.label_name, st.line, data))
    return out


def sqls(text: str):
    return [s[0] for s in split(text)]


def test_simple_and_labels():
    res = split("-- Type: TABLE; Name: app.t\nCREATE TABLE t (a int);\nALTER TABLE t OWNER TO x;\n"
                "\n-- Type: VIEW; Name: \"sales data\".v\nCREATE VIEW v AS SELECT 1;\n")
    assert [r[0] for r in res] == ["CREATE TABLE t (a int);", "ALTER TABLE t OWNER TO x;",
                                   "CREATE VIEW v AS SELECT 1;"]
    assert [(r[2], r[3]) for r in res] == [("TABLE", "app.t"), ("TABLE", "app.t"),
                                           ("VIEW", '"sales data".v')]
    assert [r[4] for r in res] == [2, 3, 6]


@pytest.mark.parametrize("text", [
    "SELECT 'a;b''c;';",
    "SELECT E'it\\'s; \\\\';",
    "SELECT e'x''; y';",
    'SELECT "a;""b" FROM t;',
    "SELECT $$ ; $$;",
    "SELECT $f$ $$ ; $f$;",
    "SELECT $1; ",
    "SELECT /* a /* nested ; */ still ; */ 1;",
    "SELECT 1 -- comment ; here\n + 2;",
    "SELECT U&'d\\0061t; ';",
    "CREATE VIEW v AS SELECT CASE WHEN a THEN 1 END FROM t;",
])
def test_single_statement(text):
    assert sqls(text) == [text.strip()]


def test_dollar_tag_not_after_identifier_and_param():
    assert sqls("SELECT a$b$ FROM t; SELECT 2;") == ["SELECT a$b$ FROM t;", "SELECT 2;"]
    assert sqls("PREPARE p AS SELECT $1;\nSELECT 3;") == ["PREPARE p AS SELECT $1;", "SELECT 3;"]


def test_begin_atomic_with_case():
    fn = ("CREATE OR REPLACE FUNCTION app.f(x integer)\n RETURNS integer\n LANGUAGE sql\nBEGIN ATOMIC\n"
          " SELECT CASE WHEN x > 0 THEN 1 ELSE 2 END;\n SELECT x * 2;\nEND;")
    assert sqls(fn + "\nSELECT 1;\n") == [fn, "SELECT 1;"]
    proc = "CREATE PROCEDURE p() LANGUAGE sql BEGIN ATOMIC INSERT INTO t VALUES (1); END;"
    assert sqls(proc) == [proc]


def test_plpgsql_body_words_ignored():
    fn = "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 1; END $$;"
    assert sqls(fn + "SELECT 2;") == [fn, "SELECT 2;"]


def test_multi_action_rule():
    rule = "CREATE RULE r AS ON INSERT TO t DO ALSO (INSERT INTO a VALUES (1); INSERT INTO b VALUES (2));"
    assert sqls(rule + "\nSELECT 1;") == [rule, "SELECT 1;"]


def test_copy_block():
    data = "1\ttab\\there\n2\t\\N\n\n3\tback\\\\slash\n"
    res = split(f"COPY t (a, b) FROM stdin;\n{data}\\.\nSELECT 1;\n")
    assert res[0][1] == "copy" and res[0][5] == data.encode()
    assert res[1][0] == "SELECT 1;" and res[1][4] == 7


def test_copy_unread_data_is_skipped():
    stream = split_sql(io.BytesIO(b"COPY t FROM stdin;\nx\ny\n\\.\nSELECT 2;\n"))
    first = next(stream)
    assert first.kind == "copy"
    assert next(stream).sql == "SELECT 2;"


def test_copy_batches_large():
    line = b"x" * 1000 + b"\n"
    body = line * 3000
    st = next(split_sql(io.BytesIO(b"COPY t FROM stdin;\n" + body + b"\\.\n")))
    batches = list(st.data)
    assert len(batches) >= 2 and b"".join(batches) == body


@pytest.mark.parametrize("text", ["SELECT 'abc", "COPY t FROM stdin;\n1\n2\n", "SELECT $$ x",
                                  "SELECT /* x", "CREATE FUNCTION f() BEGIN ATOMIC SELECT 1;"])
def test_truncated(text):
    with pytest.raises(RestoreError):
        split(text)


def test_last_statement_without_semicolon():
    assert sqls("SELECT 1;\nSELECT 2\n") == ["SELECT 1;", "SELECT 2"]


def test_crlf_and_unicode():
    assert sqls("SELECT 'üñí 日本語';\r\nSELECT 2;\r\n") == ["SELECT 'üñí 日本語';", "SELECT 2;"]


def test_util_helpers():
    assert lit("it's") == "'it''s'" and lit(None) == "NULL" and lit(False) == "false"
    assert stmt(" SELECT 1 ;; ") == "SELECT 1;\n"
    assert topo_sort(["b", "a", "c"], {"a": ["c"]}) == ["b", "c", "a"]
    with pytest.raises(CycleError):
        topo_sort([1, 2], {1: [2], 2: [1]})
