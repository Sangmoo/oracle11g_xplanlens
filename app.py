"""XPlanLens - Oracle 11gR2 실행계획 분석기 (Hugging Face Space 진입점)."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import gradio as gr  # noqa: E402

from xplanlens import __version__, analyze  # noqa: E402
from xplanlens.collect import analyze_collected, build_collect_script, is_collected, render_collected  # noqa: E402
from xplanlens.compare import compare_plans  # noqa: E402
from xplanlens.llm import PROVIDERS, LLMError, LLMRequest, build_compare_messages, build_messages, call_llm  # noqa: E402
from xplanlens.render import render_summary, render_table, render_tree_svg  # noqa: E402
from xplanlens.report import build_html_report, read_plan_file  # noqa: E402

try:  # HF Space 전용 패키지. ZeroGPU 하드웨어는 시작 시 @spaces.GPU 함수가 하나 이상 있어야 한다.
    import spaces

    @spaces.GPU
    def _zerogpu_placeholder() -> None:
        """이 앱은 GPU 를 쓰지 않는다. ZeroGPU 에서도 기동되도록 두는 빈 함수 (CPU Space 에서는 no-op)."""
        return None
except ImportError:
    pass

SAMPLES_DIR = ROOT / "src" / "xplanlens" / "samples"
SAMPLE_LABELS = {
    "01_explain_plan_basic.txt": "① EXPLAIN PLAN 기본 (실제 11g 출력)",
    "02_nested_loops_misestimate.txt": "② NL 조인 + 카디널리티 오류 (ALLSTATS LAST)",
    "03_hash_join_temp_spill.txt": "③ 해시 조인 TEMP 사용 + 파티션 (ALLSTATS LAST)",
    "04_filter_subquery_cartesian.txt": "④ FILTER 서브쿼리 + 카티션 + 형변환 (EXPLAIN)",
    "05_sqlid_collected.txt": "⑤ SQL_ID 수집 결과 (실행계획 + 통계 + 바인드)",
}
SAMPLES = {SAMPLE_LABELS.get(p.name, p.stem): p for p in sorted(SAMPLES_DIR.glob("*.txt"))}

HOW_TO = """
**실행계획 뽑는 방법 (Oracle 11gR2)** — 실제 실행 통계가 있는 `ALLSTATS LAST` 출력을 권장합니다.
```sql
-- 1) 힌트를 넣어 SQL 을 한 번 실행 (또는 ALTER SESSION SET STATISTICS_LEVEL = ALL;)
SELECT /*+ GATHER_PLAN_STATISTICS */ ... ;
-- 2) 같은 세션에서 직전 커서의 실행계획 조회 (SQL*Plus: SET LINESIZE 300 PAGESIZE 0)
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST +PEEKED_BINDS'));
-- 이미 수행된 SQL 이라면 SQL_ID 로 조회
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('&sql_id', NULL, 'ALLSTATS LAST +PEEKED_BINDS'));
```
`EXPLAIN PLAN FOR ...` + `DBMS_XPLAN.DISPLAY` 출력도 분석하지만, 추정치만 있어 정확도가 떨어집니다.
"""

SQLID_HELP = """
**DB 접속 정보를 입력하지 않고** SQL_ID 하나로 실행계획과 통계를 함께 분석합니다. XPlanLens 는 사용자 DB 에 접속하지 않습니다.

1. SQL_ID 를 입력하고 **수집 스크립트**를 만듭니다. (읽기 전용 SELECT 한 문장)
2. 평소 쓰는 **SQL*Plus · SQL Developer · Toad** 에서 실행하고, 출력 전체를 복사해 아래에 붙여넣습니다.
3. **분석하기**를 누르면 실행계획 + 자식 커서 통계 + 테이블/인덱스/컬럼 통계 + 바인드 값을 함께 진단합니다.

수집 항목: `DBMS_XPLAN.DISPLAY_CURSOR(ALLSTATS LAST)`, `V$SQL`(자식 커서·plan hash·누적 통계), `V$SQL_BIND_CAPTURE`,
`ALL_TABLES`/`ALL_TAB_STATISTICS`(건수·STALE), `ALL_INDEXES`(구성·상태·클러스터링 팩터), `ALL_TAB_COLUMNS`(조건절 컬럼 NDV·히스토그램).
필요 권한: `SELECT_CATALOG_ROLE` 또는 `SELECT ANY DICTIONARY` 수준의 V$ 조회 권한. 실제 실행 통계(A-Rows)는 SQL 이
`GATHER_PLAN_STATISTICS` 힌트나 `STATISTICS_LEVEL=ALL` 로 실행된 경우에만 나옵니다. 바인드 값이 민감하면 체크를 해제하세요.
"""

PRIVACY = (
    "🔒 API Key 는 이 요청을 처리하는 동안에만 메모리에서 사용되고 저장·로그되지 않습니다. "
    "실행계획과 SQL 은 선택한 LLM 공급자로 전송되므로, 민감한 리터럴 값은 붙여넣기 전에 가려 주세요. "
    "API Key 를 입력하지 않으면 LLM 을 호출하지 않고 규칙 기반 분석만 수행합니다."
)


# ---------------------------------------------------------------------------
# 이벤트 핸들러
# ---------------------------------------------------------------------------

def load_sample(label: str) -> str:
    path = SAMPLES.get(label)
    return path.read_text(encoding="utf-8") if path else ""


def load_file(file) -> str:
    if file is None:
        return gr.skip()
    path = file if isinstance(file, str) else getattr(file, "name", None)
    try:
        return read_plan_file(path)
    except Exception as e:
        raise gr.Error(f"파일을 읽지 못했습니다: {e}")


NO_DB_STATS = "SQL_ID 탭에서 만든 수집 스크립트의 출력을 분석하면 커서·테이블·인덱스·컬럼 통계가 여기에 표시됩니다."


def prepare(text: str):
    """일반 DBMS_XPLAN 출력과 SQL_ID 수집 출력을 모두 받아 (분석, 실행계획 원문, DB 통계 Markdown, 수집 결과) 를 돌려준다."""
    if is_collected(text):
        a, c = analyze_collected(text)
        return a, c.plan_text or text, render_collected(c, {n.name for n in a.plan.nodes if n.name}), c
    return analyze(text), text, "", None


def run_analysis(plan_text: str):
    if not plan_text or not plan_text.strip():
        raise gr.Error("실행계획을 붙여넣거나 파일/샘플을 불러와 주세요.")
    a, _, stats_md, c = prepare(plan_text)
    js = a.to_dict()
    if c is not None:
        js["collected"] = c.to_dict()
    return render_summary(a), render_tree_svg(a), render_table(a), js, "", stats_md or NO_DB_STATS


def _llm_request(provider, api_key, model, base_url, language) -> LLMRequest:
    return LLMRequest(provider=provider, api_key=api_key or "", model=model or "", base_url=base_url or "", language=language)


def run_ai(plan_text, sql_text, extra, provider, api_key, model, base_url, language, progress=gr.Progress()):
    if not (api_key or "").strip():
        return "🔑 **AI 개선안은 API Key 를 입력한 사용자만 사용할 수 있습니다.** 상단 'AI 설정'에 본인의 API Key 를 입력해 주세요. (규칙 기반 분석은 Key 없이 사용 가능합니다.)"
    if not plan_text or not plan_text.strip():
        raise gr.Error("실행계획을 먼저 입력해 주세요.")
    a, raw_plan, stats_md, _ = prepare(plan_text)
    if not a.plan.nodes:
        raise gr.Error("실행계획을 해석하지 못해 AI 분석을 진행할 수 없습니다.")
    progress(0.2, desc="LLM 에 개선안을 요청하는 중…")
    extra = "\n\n".join(x for x in ((extra or "").strip(), stats_md) if x)
    system, user = build_messages(a, raw_plan, sql_text or "", extra, language)
    try:
        return call_llm(_llm_request(provider, api_key, model, base_url, language), system, user)
    except LLMError as e:
        raise gr.Error(str(e))


def run_compare(before_text: str, after_text: str):
    if not (before_text or "").strip() or not (after_text or "").strip():
        raise gr.Error("튜닝 전/후 실행계획을 모두 입력해 주세요.")
    b, a = prepare(before_text)[0], prepare(after_text)[0]
    return compare_plans(b, a), render_tree_svg(b), render_tree_svg(a), ""


def run_compare_ai(before_text, after_text, provider, api_key, model, base_url, language):
    if not (api_key or "").strip():
        return "🔑 **AI 비교 해설은 API Key 를 입력한 사용자만 사용할 수 있습니다.**"
    if not (before_text or "").strip() or not (after_text or "").strip():
        raise gr.Error("튜닝 전/후 실행계획을 모두 입력해 주세요.")
    (b, raw_b, _, _), (a, raw_a, _, _) = prepare(before_text), prepare(after_text)
    system, user = build_compare_messages(b, a, compare_plans(b, a), raw_b, raw_a, language)
    try:
        return call_llm(_llm_request(provider, api_key, model, base_url, language), system, user)
    except LLMError as e:
        raise gr.Error(str(e))


def export_report(plan_text: str, ai_md: str):
    if not plan_text or not plan_text.strip():
        raise gr.Error("보고서로 만들 실행계획이 없습니다.")
    a, raw_plan, stats_md, _ = prepare(plan_text)
    html = build_html_report(a, raw_plan, ai_advice=ai_md or "", db_stats_md=stats_md)
    tag = re.sub(r"\W+", "", a.plan.sql_id or a.plan.plan_hash_value or "plan")
    out_dir = Path(tempfile.mkdtemp(prefix="xplanlens_"))
    path = out_dir / f"xplanlens_report_{tag}.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


def make_script(sql_id: str, child: str, include_binds: bool):
    child = (child or "").strip()
    if child and not child.isdigit():
        raise gr.Error("child 번호는 0 이상의 정수입니다. 비워 두면 모든 자식 커서를 수집합니다.")
    try:
        return build_collect_script(sql_id, int(child) if child else None, include_binds)
    except ValueError as e:
        raise gr.Error(str(e))


def send_collected(text: str):
    if not (text or "").strip():
        raise gr.Error("수집 스크립트의 실행 결과를 붙여넣어 주세요.")
    if not is_collected(text):
        raise gr.Error("'@@PLAN|' 으로 시작하는 행을 찾지 못했습니다. 스크립트 출력 전체(또는 그리드 전체 행)를 복사해 붙여넣어 주세요.")
    return text, gr.Tabs(selected="analyze")


def on_provider_change(provider: str):
    info = PROVIDERS[provider]
    is_compat = provider.startswith("OpenAI 호환")
    return (
        gr.update(choices=info["models"], value=info["default"]),
        gr.update(visible=is_compat),
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

CSS = """
.gradio-container{max-width:1400px !important}
#plan-input textarea{font-family:ui-monospace,Consolas,'D2Coding',monospace !important;font-size:12px !important;white-space:pre !important}
#cmp-before textarea,#cmp-after textarea,#collected-input textarea{font-family:ui-monospace,Consolas,monospace !important;font-size:12px !important;white-space:pre !important}
"""

FILE_TYPES = [".txt", ".sql", ".log", ".lst", ".html", ".htm"]

with gr.Blocks(title="XPlanLens - Oracle 11g 실행계획 분석기", css=CSS, theme=gr.themes.Soft()) as demo:
    gr.Markdown(
        f"# 🔍 XPlanLens — Oracle 11g 실행계획 분석기\n"
        f"Oracle **11gR2 `DBMS_XPLAN`** 출력을 붙여넣으면 실행계획을 트리로 시각화하고, 추정/실제 건수 괴리·반복 실행·TEMP 사용 등 "
        f"병목을 규칙 기반으로 진단합니다. API Key 를 입력하면 LLM 이 11g 에서 적용 가능한 개선안을 제안합니다. `v{__version__}`"
    )

    with gr.Accordion("🤖 AI 설정 (선택) — 본인 API Key 를 입력한 경우에만 LLM 을 호출합니다", open=False):
        gr.Markdown(PRIVACY)
        with gr.Row():
            provider = gr.Dropdown(list(PROVIDERS), value="Anthropic (Claude)", label="공급자")
            model = gr.Dropdown(
                PROVIDERS["Anthropic (Claude)"]["models"], value=PROVIDERS["Anthropic (Claude)"]["default"],
                label="모델 (직접 입력 가능)", allow_custom_value=True,
            )
            language = gr.Radio(["한국어", "English"], value="한국어", label="응답 언어")
        with gr.Row():
            api_key = gr.Textbox(label="API Key", type="password", placeholder="sk-... (저장되지 않음)")
            base_url = gr.Textbox(label="Base URL (OpenAI 호환 공급자)", placeholder="https://openrouter.ai/api/v1", visible=False)
        provider.change(on_provider_change, provider, [model, base_url])

    with gr.Tabs() as main_tabs:
        # ----------------------------------------------------------- 분석 탭
        with gr.Tab("📊 실행계획 분석", id="analyze"):
            with gr.Row():
                with gr.Column(scale=5):
                    plan_input = gr.Textbox(
                        label="DBMS_XPLAN 출력", lines=18, max_lines=40, elem_id="plan-input",
                        placeholder="DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST') 결과 전체를 붙여넣으세요.",
                    )
                    with gr.Accordion("SQL 원문 / 추가 정보 (선택, AI 분석 정확도 향상)", open=False):
                        sql_input = gr.Textbox(label="SQL 원문", lines=6, placeholder="DISPLAY_CURSOR 출력에 SQL 이 없으면 입력하세요.")
                        extra_input = gr.Textbox(
                            label="추가 정보", lines=4,
                            placeholder="예) ORDERS 2,000만 건, IX_ORDERS_DT(ORDER_DT), STATUS 값 5종 중 SHIPPED 90% ...",
                        )
                    with gr.Row():
                        analyze_btn = gr.Button("🔍 분석", variant="primary")
                        ai_btn = gr.Button("🤖 AI 개선안 받기")
                with gr.Column(scale=2):
                    sample_dd = gr.Dropdown(list(SAMPLES), label="샘플 실행계획", value=None)
                    upload = gr.File(label="파일 업로드 (.txt .sql .log .lst .html)", file_types=FILE_TYPES, type="filepath")
                    gr.Markdown(HOW_TO)

            with gr.Tabs():
                with gr.Tab("요약 · 진단"):
                    summary_out = gr.Markdown()
                with gr.Tab("트리 다이어그램"):
                    tree_out = gr.HTML()
                with gr.Tab("단계별 표"):
                    table_out = gr.HTML()
                with gr.Tab("AI 개선안"):
                    ai_out = gr.Markdown("API Key 를 입력하고 'AI 개선안 받기'를 누르세요.")
                with gr.Tab("DB 통계"):
                    stats_out = gr.Markdown(NO_DB_STATS)
                with gr.Tab("JSON"):
                    json_out = gr.JSON()

            with gr.Row():
                report_btn = gr.Button("📥 HTML 보고서 만들기")
                report_file = gr.File(label="보고서 다운로드", interactive=False)

            sample_dd.change(load_sample, sample_dd, plan_input).then(
                run_analysis, plan_input, [summary_out, tree_out, table_out, json_out, ai_out, stats_out]
            )
            upload.upload(load_file, upload, plan_input).then(
                run_analysis, plan_input, [summary_out, tree_out, table_out, json_out, ai_out, stats_out]
            )
            analyze_btn.click(run_analysis, plan_input, [summary_out, tree_out, table_out, json_out, ai_out, stats_out])
            ai_btn.click(
                run_ai,
                [plan_input, sql_input, extra_input, provider, api_key, model, base_url, language],
                ai_out,
                api_name=False,
            )
            report_btn.click(export_report, [plan_input, ai_out], report_file)

        # ----------------------------------------------------------- SQL_ID 탭
        with gr.Tab("🆔 SQL_ID 로 분석", id="sqlid"):
            gr.Markdown(SQLID_HELP)
            with gr.Row():
                sqlid_input = gr.Textbox(label="SQL_ID", placeholder="예) 8fk2zq1m3x7cd", max_lines=1, scale=3)
                child_input = gr.Textbox(label="child 번호 (선택)", placeholder="비우면 전체", max_lines=1, scale=1)
                binds_chk = gr.Checkbox(value=True, label="바인드 캡처 값 포함", scale=1)
                script_btn = gr.Button("① 수집 스크립트 만들기", variant="primary", scale=1)
            script_out = gr.Code(label="수집 스크립트 (오른쪽 위 버튼으로 복사)", language="sql", interactive=False)
            with gr.Row():
                with gr.Column(scale=5):
                    collected_input = gr.Textbox(
                        label="② 실행 결과 붙여넣기", lines=12, max_lines=30, elem_id="collected-input",
                        placeholder="@@META|… @@PLAN|… 로 시작하는 출력 전체를 붙여넣으세요.",
                    )
                with gr.Column(scale=2):
                    collected_file = gr.File(label="또는 스풀 파일 업로드", file_types=FILE_TYPES, type="filepath")
                    collected_btn = gr.Button("③ 분석하기", variant="primary")

            script_btn.click(make_script, [sqlid_input, child_input, binds_chk], script_out)
            collected_file.upload(load_file, collected_file, collected_input)
            collected_btn.click(send_collected, collected_input, [plan_input, main_tabs]).then(
                run_analysis, plan_input, [summary_out, tree_out, table_out, json_out, ai_out, stats_out]
            )

        # ----------------------------------------------------------- 비교 탭
        with gr.Tab("⚖️ 튜닝 전/후 비교"):
            gr.Markdown("같은 SQL 의 튜닝 전/후 실행계획을 넣으면 수행시간·Buffers·접근 경로·조인 방식·진단 결과의 변화를 비교합니다.")
            with gr.Row():
                with gr.Column():
                    before_file = gr.File(label="Before 파일", file_types=FILE_TYPES, type="filepath")
                    before_input = gr.Textbox(label="Before (튜닝 전)", lines=14, elem_id="cmp-before")
                with gr.Column():
                    after_file = gr.File(label="After 파일", file_types=FILE_TYPES, type="filepath")
                    after_input = gr.Textbox(label="After (튜닝 후)", lines=14, elem_id="cmp-after")
            with gr.Row():
                cmp_btn = gr.Button("⚖️ 비교", variant="primary")
                cmp_ai_btn = gr.Button("🤖 AI 비교 해설")
            cmp_out = gr.Markdown()
            cmp_ai_out = gr.Markdown()
            with gr.Row():
                with gr.Column():
                    gr.Markdown("**Before 트리**")
                    before_tree = gr.HTML()
                with gr.Column():
                    gr.Markdown("**After 트리**")
                    after_tree = gr.HTML()

            before_file.upload(load_file, before_file, before_input)
            after_file.upload(load_file, after_file, after_input)
            cmp_btn.click(run_compare, [before_input, after_input], [cmp_out, before_tree, after_tree, cmp_ai_out])
            cmp_ai_btn.click(
                run_compare_ai,
                [before_input, after_input, provider, api_key, model, base_url, language],
                cmp_ai_out,
                api_name=False,
            )

    gr.Markdown(
        "<small>XPlanLens 는 Oracle Corporation 과 무관한 오픈소스 프로젝트입니다. Oracle 은 Oracle 및 그 계열사의 등록상표입니다. "
        "AI 개선안은 참고용이며 운영 적용 전 반드시 검증하세요.</small>"
    )


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=8).launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
    )
