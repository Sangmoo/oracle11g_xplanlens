"""SQL_ID 수집 스크립트 생성과 출력 해석."""
import pytest

from xplanlens.collect import (
    analyze_collected,
    build_collect_script,
    is_collected,
    normalize_sql_id,
    parse_collected,
    render_collected,
)

SAMPLE = "05_sqlid_collected.txt"


def test_normalize_sql_id():
    assert normalize_sql_id(" 8FK2ZQ1M3X7CD ") == "8fk2zq1m3x7cd"
    for bad in ("", "abc", "8fk2zq1m3x7cd'--", "8fk2zq1m3x7c;"):
        with pytest.raises(ValueError):
            normalize_sql_id(bad)


def test_script_is_single_readonly_select():
    s = build_collect_script("8fk2zq1m3x7cd", child_number=2)
    body = s[s.index("WITH objs"):]
    assert body.count(";") == 1 and body.rstrip().endswith(";")
    assert "DISPLAY_CURSOR('8fk2zq1m3x7cd', 2, 'ALLSTATS LAST +PEEKED_BINDS')" in s
    assert "@@BIND|" in s
    for word in ("INSERT", "UPDATE ", "DELETE", "MERGE", "DROP", "ALTER", "EXECUTE", "BEGIN"):
        assert word not in body.upper()
    # SQL*Plus: 문장 안의 빈 줄은 문장을 끊고, 줄 맨 앞의 @ 는 스크립트 실행으로 해석된다
    stmt = body.split("\n")
    assert all(ln.strip() for ln in stmt[:-1])
    assert not any(ln.lstrip().startswith("@") for ln in stmt)


def test_script_without_binds_and_all_children():
    s = build_collect_script("8fk2zq1m3x7cd", include_binds=False)
    assert "@@BIND|" not in s and "DISPLAY_CURSOR('8fk2zq1m3x7cd', NULL" in s


def test_parse_sample(sample):
    c = parse_collected(sample(SAMPLE))
    assert c.sql_id == "8fk2zq1m3x7cd" and c.db_version == "11.2.0.4.0"
    assert sorted(c.plans) == [0, 1]
    assert c.selected_child == 0  # 가장 최근 LAST_ACTIVE_TIME
    assert len(c.cursors) == 2 and c.cursors[0].elapsed_s == pytest.approx(4965.6)
    assert c.cursors[0].module == "JDBC Thin Client"
    assert "\n" in c.sql_text and c.sql_text.startswith("SELECT")
    assert c.index("IX_ITEMS_PRDT").columns == ["PRDT_ID", "ORDER_ID"]
    assert c.table_of("IX_ORDERS_DT").name == "ORDERS"
    assert [b.value for b in c.binds if b.child == 1] == ["CANCELLED", "10/01/2026 00:00:00"]


def test_analyze_collected_findings(sample):
    a, c = analyze_collected(sample(SAMPLE))
    rules = {f.rule for f in a.findings}
    assert {"MULTIPLE_PLANS", "STALE_STATS", "HISTOGRAM_CANDIDATE", "UNUSABLE_INDEX",
            "TABLE_DEGREE", "BIND_AWARE", "POOR_CLUSTERING", "CARDINALITY_MISESTIMATE"} <= rules
    hist = next(f for f in a.findings if f.rule == "HISTOGRAM_CANDIDATE")
    assert hist.node_ids == [5] and "ORDER_DT" in hist.suggestion
    assert a.plan.plan_hash_value == "2817364519"
    sev = [f.severity for f in a.findings]
    assert sev == sorted(sev, key=["critical", "warning", "info"].index)


def test_child_selection_and_index_candidate(sample):
    text = sample(SAMPLE).replace("@@META|8fk2zq1m3x7cd|2026-11-03 10:15:00|11.2.0.4.0|NULL",
                                  "@@META|8fk2zq1m3x7cd|2026-11-03 10:15:00|11.2.0.4.0|1")
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("@@CUR|0|"))
    a, c = analyze_collected(text)
    assert c.selected_child == 1 and a.plan.plan_hash_value == "1934450212"
    cand = [f for f in a.findings if f.rule == "INDEX_CANDIDATE"]
    assert cand and cand[0].node_ids == [4] and "STATUS" in cand[0].title


def test_grid_copy_with_quotes_and_noise(sample):
    lines = sample(SAMPLE).splitlines()
    noisy = ["XPLANLENS", "-" * 30] + [f'"{ln}"' for ln in lines] + ["", "54 rows selected."]
    c = parse_collected("\r\n".join(noisy))
    assert sorted(c.plans) == [0, 1] and len(c.tables) == 3
    assert c.binds[0].value == "SHIPPED"


def test_cursor_not_found():
    text = ("@@META|abcdabcdabcd1|2026-11-03 10:00:00|11.2.0.4.0|NULL|xplanlens-collect-1\n"
            "@@PLAN|SQL_ID: abcdabcdabcd1, child number: 0 cannot be found\n")
    assert is_collected(text)
    a, c = analyze_collected(text)
    assert not a.plan.nodes and not c.plans
    f = next(f for f in a.findings if f.rule == "CURSOR_NOT_FOUND")
    assert "cannot be found" in f.detail


def test_non_11g_and_no_stats():
    text = ("@@META|abcdabcdabcd1|2026-11-03 10:00:00|19.0.0.0.0|NULL|x\n"
            "@@PLAN|SQL_ID  abcdabcdabcd1, child number 0\n@@PLAN|Plan hash value: 1\n@@PLAN|\n"
            "@@PLAN|-------------------------------------------------\n"
            "@@PLAN|| Id  | Operation         | Name | Rows  | Cost  |\n"
            "@@PLAN|-------------------------------------------------\n"
            "@@PLAN||   0 | SELECT STATEMENT  |      |       |    3 |\n"
            "@@PLAN||   1 |  TABLE ACCESS FULL| T1   |    10 |    3 |\n"
            "@@PLAN|-------------------------------------------------\n"
            "@@TAB|APP|T1|||||||NO|1|N\n")
    a, _ = analyze_collected(text)
    rules = {f.rule: f for f in a.findings}
    assert "DB_VERSION" in rules and rules["NO_TABLE_STATS"].node_ids == [1]


def test_render_collected(sample):
    a, c = analyze_collected(sample(SAMPLE))
    md = render_collected(c, {n.name for n in a.plan.nodes})
    assert "자식 커서별 누적 통계" in md and "| ✔ | IX_ORDERS_DT" in md
    assert "바인드 캡처" in md and "UNUSABLE" in md


def test_plain_plan_is_not_collected(sample):
    assert not is_collected(sample("02_nested_loops_misestimate.txt"))
