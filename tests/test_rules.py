from xplanlens import analyze


def rules_of(a):
    return {f.rule for f in a.findings}


def by_rule(a, rule):
    return [f for f in a.findings if f.rule == rule]


def test_explain_plan_without_runtime(sample):
    a = analyze(sample("01_explain_plan_basic.txt"))
    assert "NO_RUNTIME_STATS" in rules_of(a)
    assert "OLD_PLAN_TABLE" in rules_of(a)
    assert not any(f.severity == "critical" for f in a.findings)


def test_derived_metrics(sample):
    a = analyze(sample("02_nested_loops_misestimate.txt"))
    p = a.plan
    # E-Rows 는 Starts 1회당 값 -> 48210 x 1 = 48210, 실제 48210 이므로 괴리 없음
    assert p.node(6).est_total_rows == 48210
    assert p.node(6).misestimate_ratio == 1
    # 자체 시간 = 누적 A-Time - 자식 A-Time
    assert abs(p.node(8).self_time - (27.95 - 2.14)) < 1e-6
    assert p.node(2).self_buffers == 1_502_000 - 436_000 - 1_066_000 or p.node(2).self_buffers == 0
    assert a.total_time == 41.37


def test_misestimate_reports_root_cause_only(sample):
    a = analyze(sample("02_nested_loops_misestimate.txt"))
    mis = by_rule(a, "CARDINALITY_MISESTIMATE")
    assert len(mis) == 1
    assert mis[0].node_ids[0] == 5  # 가장 안쪽 INDEX RANGE SCAN 이 근본 원인
    assert mis[0].severity == "critical"
    assert set(mis[0].node_ids) == {5, 4, 3, 2, 1}


def test_nested_loop_findings(sample):
    a = analyze(sample("02_nested_loops_misestimate.txt"))
    starts = {f.node_ids[0] for f in by_rule(a, "HIGH_STARTS")}
    assert starts == {6, 8}
    waste = by_rule(a, "INDEX_FILTER_WASTE")
    assert waste and waste[0].node_ids == [4, 5]
    hot = by_rule(a, "TIME_HOTSPOT")
    assert [f.node_ids[0] for f in hot] == [8]
    assert "PEEKED_BINDS" in rules_of(a)
    assert "CARDINALITY_FEEDBACK" in rules_of(a)


def test_hash_join_spill_and_partition(sample):
    a = analyze(sample("03_hash_join_temp_spill.txt"))
    r = rules_of(a)
    assert {"TEMP_SPILL", "NO_PARTITION_PRUNING", "DYNAMIC_SAMPLING"} <= r
    spill = by_rule(a, "TEMP_SPILL")[0]
    assert spill.node_ids == [2] and "one-pass" in spill.title
    mis_nodes = {f.node_ids[0] for f in by_rule(a, "CARDINALITY_MISESTIMATE")}
    assert 3 in mis_nodes  # CUSTOMERS 509배 과소 추정
    assert 2 not in mis_nodes  # HASH JOIN 은 자식에서 전파된 괴리
    # 파티션 반복자 하위의 E-Rows 는 Starts 를 곱하지 않는다
    assert a.plan.node(5).est_total_rows == 918_000


def test_filter_cartesian_conversion(sample):
    a = analyze(sample("04_filter_subquery_cartesian.txt"))
    r = rules_of(a)
    assert {"CARTESIAN_JOIN", "FILTER_SUBQUERY", "IMPLICIT_CONVERSION", "NO_RUNTIME_STATS"} <= r
    assert {f.node_ids[0] for f in by_rule(a, "IMPLICIT_CONVERSION")} == {1, 3, 6}
    assert a.findings[0].severity == "critical"  # 심각도 순 정렬


def test_small_fast_query_has_no_noise(fixture):
    a = analyze(fixture("sqlplus_wrapped.txt"))
    assert not [f for f in a.findings if f.severity in ("critical", "warning")]


def test_12c_note_is_unsupported(fixture):
    a = analyze(fixture("adaptive_12c.txt"))
    assert "UNSUPPORTED_12C" in rules_of(a)


def test_unknown_note_goes_to_unsupported():
    text = (
        "| Id  | Operation        | Name | Rows  |\n"
        "|   0 | SELECT STATEMENT |      |     1 |\n"
        "\nNote\n-----\n   - something brand new\n"
    )
    a = analyze(text)
    assert any("something brand new" in m for m in a.plan.unsupported)


def test_to_dict_is_json_serializable(sample):
    import json

    a = analyze(sample("02_nested_loops_misestimate.txt"))
    json.dumps(a.to_dict(), ensure_ascii=False)
