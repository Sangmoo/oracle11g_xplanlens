import xml.etree.ElementTree as ET

from xplanlens import analyze
from xplanlens.compare import compare_plans
from xplanlens.render import render_summary, render_table, render_tree_svg
from xplanlens.report import build_html_report, decode_bytes, html_to_text, read_plan_file

ALL_SAMPLES = [
    "01_explain_plan_basic.txt",
    "02_nested_loops_misestimate.txt",
    "03_hash_join_temp_spill.txt",
    "04_filter_subquery_cartesian.txt",
]


def _svg_of(html: str) -> str:
    start = html.index("<svg")
    end = html.index("</svg>") + len("</svg>")
    return html[start:end]


def test_svg_is_well_formed_for_all_samples(sample):
    for name in ALL_SAMPLES:
        a = analyze(sample(name))
        svg = _svg_of(render_tree_svg(a))
        root = ET.fromstring(svg)  # XML 파싱 실패 시 예외
        rects = root.findall(".//{http://www.w3.org/2000/svg}rect")
        assert len(rects) == len(a.plan.nodes)


def test_tree_layout_parents_centered_over_children(sample):
    from xplanlens.render import _layout

    a = analyze(sample("02_nested_loops_misestimate.txt"))
    pos = _layout(a)
    xs = [pos[c][0] for c in a.plan.node(2).children]
    assert pos[2][0] == (xs[0] + xs[-1]) / 2
    assert all(pos[n.id][1] > pos[n.parent_id][1] for n in a.plan.nodes if n.parent_id is not None)


def test_table_and_summary(sample):
    a = analyze(sample("02_nested_loops_misestimate.txt"))
    html = render_table(a)
    assert html.count("<tr") == len(a.plan.nodes) + 1
    assert "IX_ORDERS_DT" in html and "E-Rows×Starts" in html
    md = render_summary(a)
    assert "8fk2zq1m3x7cd" in md and "심각" in md


def test_html_escaping():
    text = (
        "| Id  | Operation        | Name    | Rows  |\n"
        "|   0 | SELECT STATEMENT |         |     1 |\n"
        "|*  1 |  TABLE ACCESS FULL| <T&X>  |     1 |\n"
        "\nPredicate Information (identified by operation id):\n"
        "---------------------------------------------------\n"
        "   1 - filter(\"A\"<'<script>')\n"
    )
    a = analyze(text)
    assert "<script>" not in render_table(a)
    ET.fromstring(_svg_of(render_tree_svg(a)))


def test_empty_plan_renders_message():
    a = analyze("nothing here")
    assert "해석하지 못했습니다" in render_summary(a)
    assert "<svg" not in render_tree_svg(a)


def test_report_contains_everything(sample):
    raw = sample("03_hash_join_temp_spill.txt")
    a = analyze(raw)
    html = build_html_report(a, raw, ai_advice="## 제안\n- 인덱스 추가")
    assert html.startswith("<!doctype html>")
    assert "<svg" in html and "TEMP" in html and "인덱스 추가" in html
    assert "<h2>" in html and "CUSTOMERS" in html


def test_read_plan_file_handles_cp949_and_html(tmp_path, sample):
    raw = sample("01_explain_plan_basic.txt")
    p1 = tmp_path / "plan_cp949.txt"
    p1.write_bytes(("-- 한글 주석\n" + raw).encode("cp949"))
    assert "T_CLOSE_SALE_BASE" in read_plan_file(str(p1))
    assert len(analyze(read_plan_file(str(p1))).plan.nodes) == 3

    p2 = tmp_path / "plan.html"
    p2.write_text(f"<html><body><pre>{raw.replace('<', '&lt;')}</pre></body></html>", encoding="utf-8")
    assert len(analyze(read_plan_file(str(p2))).plan.nodes) == 3


def test_decode_and_html_to_text():
    assert decode_bytes("가나다".encode("utf-8")) == "가나다"
    assert decode_bytes("가나다".encode("cp949")) == "가나다"
    assert html_to_text("<p>a&amp;b</p><script>x</script>").strip() == "a&b"


def test_compare_detects_improvement(sample):
    before = analyze(sample("02_nested_loops_misestimate.txt"))
    after_text = """SQL_ID  8fk2zq1m3x7cd, child number 1
-------------------------------------
SELECT ...

Plan hash value: 1111111111

| Id  | Operation                | Name           | Starts | E-Rows | A-Rows |   A-Time   | Buffers | Reads  |
|   0 | SELECT STATEMENT         |                |      1 |        |  48210 |00:00:03.10 |   60123 |  50211 |
|   1 |  HASH GROUP BY           |                |      1 |  48000 |  48210 |00:00:03.10 |   60123 |  50211 |
|*  2 |   HASH JOIN              |                |      1 |    190K|    193K|00:00:02.80 |   60123 |  50211 |
|*  3 |    HASH JOIN             |                |      1 |  48000 |  48210 |00:00:01.10 |   21000 |  19000 |
|*  4 |     TABLE ACCESS FULL    | ORDERS         |      1 |  48000 |  48210 |00:00:00.60 |   15000 |  14000 |
|   5 |     TABLE ACCESS FULL    | CUSTOMERS      |      1 |    100K|    100K|00:00:00.20 |    6000 |   5000 |
|   6 |    TABLE ACCESS FULL     | ORDER_ITEMS    |      1 |   1900K|   1900K|00:00:00.90 |   39123 |  31211 |
"""
    after = analyze(after_text)
    md = compare_plans(before, after)
    assert "▼" in md and "✅" in md
    assert "ORDERS" in md and "TABLE ACCESS FULL" in md
    assert "HASH JOIN" in md
    assert "해결된 문제" in md


def test_compare_requires_both():
    assert "모두" in compare_plans(analyze(""), analyze(""))
