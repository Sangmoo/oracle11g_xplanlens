"""입력 파일 읽기와 HTML 보고서 내보내기."""
from __future__ import annotations

import re
from datetime import datetime
from html import escape, unescape
from html.parser import HTMLParser

from . import __version__
from .render import render_summary, render_table, render_tree_svg
from .rules import Analysis

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class _TextExtractor(HTMLParser):
    _BREAKS = {"br", "p", "div", "tr", "pre", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = unescape("".join(p.parts)).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text)


def decode_bytes(data: bytes) -> str:
    """UTF-8 → CP949(국내 Windows 기본) → Latin-1 순으로 디코딩."""
    for enc in ("utf-8-sig", "cp949", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_plan_file(path: str) -> str:
    """업로드된 .txt/.sql/.log/.html 파일을 실행계획 텍스트로 변환."""
    with open(path, "rb") as f:
        data = f.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("파일이 너무 큽니다 (최대 5MB).")
    text = decode_bytes(data)
    if re.search(r"<\s*(html|table|pre|body)\b", text[:5000], re.I):
        text = html_to_text(text)
    return text


_REPORT_CSS = """
body{font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI','Malgun Gothic',sans-serif;margin:24px auto;max-width:1280px;padding:0 16px;color:#202124;background:#fff;line-height:1.55}
h1{font-size:22px;margin-bottom:4px} h2{border-bottom:2px solid #e8eaed;padding-bottom:4px;margin-top:32px}
blockquote{border-left:4px solid #1a73e8;margin:8px 0;padding:4px 12px;background:#f1f6fe}
pre{background:#f8f9fa;padding:12px;overflow:auto;border-radius:6px;font-size:12px}
code{background:#f1f3f4;padding:1px 4px;border-radius:4px}
table{border-collapse:collapse} td,th{border:1px solid #dadce0;padding:4px 8px}
.meta{color:#5f6368;font-size:13px}
"""


def _md_to_html(md: str) -> str:
    try:
        from markdown_it import MarkdownIt

        return MarkdownIt("commonmark").enable("table").render(md)
    except Exception:
        return f"<pre>{escape(md)}</pre>"


def build_html_report(a: Analysis, raw_plan: str, ai_advice: str = "", compare_md: str = "", db_stats_md: str = "") -> str:
    """분석 결과 전체를 하나의 독립 HTML 파일로 만든다 (외부 리소스 없음)."""
    title = f"XPlanLens 실행계획 분석 보고서{' - ' + a.plan.sql_id if a.plan.sql_id else ''}"
    sections = [
        f"<h1>{escape(title)}</h1>",
        f'<div class="meta">생성: {datetime.now():%Y-%m-%d %H:%M} · xplanlens {__version__} · Oracle 11gR2 DBMS_XPLAN 전용</div>',
        "<h2>요약 및 규칙 기반 진단</h2>", _md_to_html(render_summary(a)),
        "<h2>실행계획 트리</h2>", render_tree_svg(a),
        "<h2>단계별 상세</h2>", render_table(a),
    ]
    if db_stats_md.strip():
        sections += ["<h2>DB 통계 (SQL_ID 수집)</h2>", _md_to_html(db_stats_md)]
    if compare_md.strip():
        sections += ["<h2>튜닝 전/후 비교</h2>", _md_to_html(compare_md)]
    if ai_advice.strip():
        sections += ["<h2>AI 개선안</h2>",
                     '<p class="meta">LLM 이 생성한 내용입니다. 운영 적용 전 반드시 검증하세요.</p>',
                     _md_to_html(ai_advice)]
    sections += ["<h2>DBMS_XPLAN 원문</h2>", f"<pre>{escape(raw_plan)}</pre>"]
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_REPORT_CSS}</style></head><body>"
        + "\n".join(sections) + "</body></html>"
    )
