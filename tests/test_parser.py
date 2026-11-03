import pytest

from xplanlens.parser import parse_bytes, parse_cost, parse_count, parse_duration, parse_mem, parse_plan


@pytest.mark.parametrize("text,expected", [
    ("48210", 48210), ("193K", 193_000), ("1502K", 1_502_000), ("2M", 2_000_000), ("   ", None), ("", None), ("abc", None),
])
def test_parse_count(text, expected):
    assert parse_count(text) == expected


def test_parse_bytes_and_mem():
    assert parse_bytes("11M") == 11 * 1024**2
    assert parse_mem("9876K (0)") == (9876 * 1024, 0)
    assert parse_mem("  26M (1)") == (26 * 1024**2, 1)
    assert parse_mem("") == (None, None)


def test_parse_cost_and_duration():
    assert parse_cost("  2186K  (1)") == (2_186_000, 1)
    assert parse_cost("     2   (0)") == (2, 0)
    assert parse_cost("") == (None, None)
    assert parse_duration("00:00:41.37") == pytest.approx(41.37)
    assert parse_duration("07:17:21") == 7 * 3600 + 17 * 60 + 21


def test_explain_plan_basic(sample):
    p = parse_plan(sample("01_explain_plan_basic.txt"))
    assert p.plan_hash_value == "2537846791"
    assert not p.has_runtime_stats
    assert [n.id for n in p.nodes] == [0, 1, 2]
    assert [n.depth for n in p.nodes] == [0, 1, 2]
    assert p.node(2).operation == "TABLE ACCESS FULL"
    assert p.node(2).name == "T_CLOSE_SALE_BASE"
    assert p.node(2).parent_id == 1
    assert p.node(1).has_predicate
    assert p.node(1).predicates[0].kind == "filter"
    assert p.node(1).predicates[0].text == "ROWNUM<=10"
    assert p.node(0).cost == 2
    assert p.notes == ["'PLAN_TABLE' is old version"]
    assert p.unsupported == []


def test_display_cursor_allstats(sample):
    p = parse_plan(sample("02_nested_loops_misestimate.txt"))
    assert p.sql_id == "8fk2zq1m3x7cd"
    assert p.child_number == 0
    assert p.plan_hash_value == "2817364519"
    assert "order_items i" in p.sql_text
    assert p.has_runtime_stats
    assert len(p.nodes) == 10

    nl_inner = p.node(8)
    assert nl_inner.parent_id == 2
    assert nl_inner.starts == 48210
    assert nl_inner.a_rows == 193_000
    assert nl_inner.a_time == pytest.approx(27.95)
    assert nl_inner.buffers == 1_066_000
    assert p.node(2).children == [3, 8]
    assert p.node(3).children == [4, 6]

    assert p.node(1).used_mem == 9876 * 1024 and p.node(1).used_mem_passes == 0
    assert [b.name for b in p.peeked_binds] == [":B1", ":B2"]
    assert p.peeked_binds[0].datatype == "VARCHAR2(30), CSID=846"
    assert p.peeked_binds[0].value == "'SHIPPED'"
    assert p.node(4).predicates[0].text == '"O"."STATUS"=:B1'
    assert p.notes == ["cardinality feedback used for this statement"]


def test_partition_and_temp_columns(sample):
    p = parse_plan(sample("03_hash_join_temp_spill.txt"))
    hj = p.node(2)
    assert hj.used_tmp == 306 * 1024**2
    assert hj.used_mem_passes == 1
    assert p.node(4).pstart == "1" and p.node(4).pstop == "28"
    assert p.node(5).parent_id == 4


def test_multiline_predicate(sample):
    p = parse_plan(sample("04_filter_subquery_cartesian.txt"))
    pred = p.node(1).predicates[0].text
    assert pred.startswith("EXISTS (SELECT 0")
    assert pred.endswith('TO_NUMBER("B"."BONUS_YM")>=201001)')
    assert p.node(6).parent_id == 1
    assert p.node(0).cost == 2_186_000
    assert p.node(0).e_time == 7 * 3600 + 17 * 60 + 21


def test_sqlplus_wrapper_and_second_predicate(fixture):
    p = parse_plan(fixture("sqlplus_wrapped.txt"))
    assert p.sql_id == "a1b2c3d4e5f6g"
    assert len(p.nodes) == 3
    preds = p.node(2).predicates
    assert [x.text for x in preds] == ['"DEPTNO"=10', '"SAL">0']  # "22 rows selected." 는 붙지 않아야 함


def test_adaptive_plan_marked_unsupported(fixture):
    p = parse_plan(fixture("adaptive_12c.txt"))
    assert [n.id for n in p.nodes if n.inactive] == [2, 3, 5]
    assert any("12c" in m for m in p.unsupported)


@pytest.mark.parametrize("text", ["", "   ", "hello world", "| broken | table |", "Id Operation\n-----\nfoo"])
def test_garbage_input_does_not_raise(text):
    p = parse_plan(text)
    assert p.nodes == []
    assert p.unsupported


def test_crlf_and_tabs(sample):
    text = sample("02_nested_loops_misestimate.txt").replace("\n", "\r\n")
    p = parse_plan(text)
    assert len(p.nodes) == 10 and p.node(9).name == "IX_ITEMS_ORDER"


def test_unknown_column_kept_raw():
    text = (
        "| Id  | Operation        | Name | Rows  | Foo |\n"
        "|   0 | SELECT STATEMENT |      |     1 |  x  |\n"
        "|   1 |  TABLE ACCESS FULL| T   |     1 |  y  |\n"
    )
    p = parse_plan(text)
    assert len(p.nodes) == 2
    assert p.node(1).raw["x_foo"] == "y"
    assert any("Foo" in m for m in p.unsupported)
