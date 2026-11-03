"""분석 결과 시각화: 요약 Markdown, 단계별 HTML 표, SVG 트리 다이어그램.

Gradio 의 gr.HTML 은 스크립트를 실행하지 않으므로 순수 HTML/SVG + 인라인 스타일만 사용한다.
"""
from __future__ import annotations

from html import escape
from typing import Optional

from .models import PlanNode
from .rules import Analysis, _fmt

SEVERITY_LABEL = {"critical": "🔴 심각", "warning": "🟠 경고", "info": "🔵 참고"}
SEVERITY_COLOR = {"critical": "#d93025", "warning": "#e8710a", "info": "#1a73e8"}


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

def _weight(a: Analysis, n: PlanNode) -> float:
    """노드의 '무게'(0~1): 실행 통계가 있으면 자체 시간 비중, 없으면 Cost 비중."""
    if a.total_time and n.self_time is not None:
        return min(n.self_time / a.total_time, 1.0)
    if a.total_buffers and n.self_buffers is not None:
        return min(n.self_buffers / a.total_buffers, 1.0)
    if a.total_cost and n.cost is not None:
        return min(n.cost / a.total_cost, 1.0)
    return 0.0


def _heat(w: float) -> str:
    """0 -> 거의 흰색, 1 -> 진한 빨강."""
    light = 97 - w * 40
    sat = 60 + w * 25
    return f"hsl(8, {sat:.0f}%, {light:.0f}%)"


def _ratio_color(r: Optional[float]) -> str:
    if r is None:
        return "#9aa0a6"
    if r >= 100:
        return "#d93025"
    if r >= 10:
        return "#e8710a"
    return "#188038"


def _worst_severity(a: Analysis, node_id: int) -> Optional[str]:
    # 카디널리티 괴리가 전파된 상위 단계는 근본 원인이 아니므로 강조하지 않는다
    sev = [
        f.severity for f in a.findings_for(node_id)
        if f.rule != "CARDINALITY_MISESTIMATE" or f.node_ids[0] == node_id
    ]
    for s in ("critical", "warning", "info"):
        if s in sev:
            return s
    return None


def _sec(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}s"


# ---------------------------------------------------------------------------
# 요약 Markdown
# ---------------------------------------------------------------------------

def render_summary(a: Analysis) -> str:
    p = a.plan
    if not p.nodes:
        msgs = "\n".join(f"- {m}" for m in p.unsupported) or "- 실행계획을 찾지 못했습니다."
        return "### ⚠️ 실행계획을 해석하지 못했습니다\n\n" + msgs

    out = ["### 📋 실행계획 요약"]
    meta = []
    if p.sql_id:
        meta.append(f"**SQL_ID** `{p.sql_id}`" + (f" (child {p.child_number})" if p.child_number is not None else ""))
    if p.plan_hash_value:
        meta.append(f"**Plan hash value** `{p.plan_hash_value}`")
    meta.append(f"**단계 수** {len(p.nodes)}")
    meta.append("**실행 통계** " + ("있음 (ALLSTATS)" if p.has_runtime_stats else "없음 (추정치만)"))
    if a.total_time is not None:
        meta.append(f"**총 수행시간** {a.total_time:,.2f}초")
    if a.total_buffers is not None:
        meta.append(f"**총 Buffers** {_fmt(a.total_buffers)}")
    if a.total_cost is not None:
        meta.append(f"**Cost** {_fmt(a.total_cost)}")
    out.append(" · ".join(meta))

    counts = {s: sum(1 for f in a.findings if f.severity == s) for s in ("critical", "warning", "info")}
    out.append(f"\n**진단 결과**: {SEVERITY_LABEL['critical']} {counts['critical']}건 · "
               f"{SEVERITY_LABEL['warning']} {counts['warning']}건 · {SEVERITY_LABEL['info']} {counts['info']}건")

    # 가장 무거운 단계 Top 3
    ranked = sorted((n for n in p.nodes if not n.inactive), key=lambda n: _weight(a, n), reverse=True)
    top = [n for n in ranked if _weight(a, n) > 0][:3]
    if top:
        basis = "자체 수행시간" if a.total_time else ("자체 Buffers" if a.total_buffers else "Cost")
        out.append(f"\n**가장 무거운 단계 ({basis} 기준)**")
        for n in top:
            out.append(f"- Id {n.id} `{n.label}` — {_weight(a, n):.0%}")

    if a.findings:
        out.append("\n---\n### 🔎 진단 상세")
        for f in a.findings:
            out.append(f"\n#### {SEVERITY_LABEL.get(f.severity, f.severity)} · {f.title}")
            out.append(f"{f.detail}")
            if f.suggestion:
                out.append(f"\n> 💡 **개선 방향**: {f.suggestion}")
    else:
        out.append("\n규칙 기반 진단에서 눈에 띄는 문제를 찾지 못했습니다.")

    if p.unsupported:
        out.append("\n---\n### ⚙️ 미지원 항목 (분석에서 제외)")
        out.extend(f"- {m}" for m in p.unsupported)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 단계별 HTML 표 (explain.depesz 스타일)
# ---------------------------------------------------------------------------

_TABLE_CSS = """
<style>
.xpl-wrap{overflow-x:auto;font-family:ui-sans-serif,system-ui,-apple-system,'Segoe UI','Malgun Gothic',sans-serif;font-size:13px}
.xpl{border-collapse:collapse;width:100%;min-width:760px}
.xpl th{position:sticky;top:0;background:#202124;color:#fff;font-weight:600;padding:6px 8px;text-align:right;white-space:nowrap}
.xpl th.l,.xpl td.l{text-align:left}
.xpl td{padding:5px 8px;border-bottom:1px solid rgba(128,128,128,.25);text-align:right;vertical-align:top;white-space:nowrap;color:#202124}
.xpl tr td{background:var(--xpl-bg)}
.xpl .op{font-family:ui-monospace,Consolas,'D2Coding',monospace;white-space:pre}
.xpl .pred{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#5f6368;white-space:normal;max-width:520px;margin-top:2px}
.xpl .badge{display:inline-block;min-width:44px;padding:1px 6px;border-radius:10px;color:#fff;font-weight:600;text-align:center}
.xpl .bar{display:inline-block;height:8px;border-radius:4px;background:#d93025;vertical-align:middle;margin-right:6px}
.xpl .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-left:4px}
.xpl .inactive td{opacity:.45;text-decoration:line-through}
.xpl-legend{margin:6px 0 10px;color:inherit;font-size:12px}
/* Gradio 다크 테마가 글자색을 덮어쓰지 않도록 표는 항상 밝은 카드로 고정 */
.xpl{background:#fff}
.xpl td,.xpl td *{color:#202124 !important}
.xpl th{color:#fff !important}
.xpl .pred,.xpl .pred *{color:#5f6368 !important}
.xpl .badge{color:#fff !important}
</style>
"""


def render_table(a: Analysis) -> str:
    p = a.plan
    if not p.nodes:
        return "<p>표시할 실행계획이 없습니다.</p>"
    rt = p.has_runtime_stats
    heads = ["Id", "Operation", "Name"]
    if rt:
        heads += ["Starts", "E-Rows×Starts", "A-Rows", "괴리", "자체 시간", "자체 Buffers", "Reads", "Mem/Temp"]
    else:
        heads += ["Rows", "Bytes", "Cost", "Cost 비중", "Time"]
    heads.append("진단")

    th = "".join(f'<th class="{"l" if h in ("Operation", "Name", "진단") else ""}">{escape(h)}</th>' for h in heads)
    body = []
    for n in p.nodes:
        w = _weight(a, n)
        indent = "  " * n.depth + ("└ " if n.depth else "")
        star = "*" if n.has_predicate else " "
        preds = "".join(
            f'<div class="pred"><b>{escape(pr.kind)}</b>: {escape(pr.text)}</div>' for pr in n.predicates
        )
        cells = [
            f"<td>{star}{n.id}</td>",
            f'<td class="l"><span class="op">{escape(indent + n.operation)}</span>{preds}</td>',
            f'<td class="l">{escape(n.name)}</td>',
        ]
        if rt:
            ratio = n.misestimate_ratio
            arrow = "" if ratio is None or ratio < 2 else ("↑" if n.underestimated else "↓")
            badge = (
                f'<span class="badge" style="background:{_ratio_color(ratio)}" '
                f'title="실제/추정 괴리 배수 (↑ 과소추정, ↓ 과대추정)">{arrow}{ratio:,.0f}x</span>'
                if ratio is not None else "-"
            )
            t_share = (n.self_time / a.total_time) if (a.total_time and n.self_time is not None) else 0
            b_share = (n.self_buffers / a.total_buffers) if (a.total_buffers and n.self_buffers is not None) else 0
            mem = n.raw.get("used_mem", "")
            if n.raw.get("used_tmp"):
                mem += f" / tmp {n.raw['used_tmp']}"
            cells += [
                f"<td>{_fmt(n.starts)}</td>",
                f"<td>{_fmt(n.est_total_rows)}</td>",
                f"<td>{_fmt(n.a_rows)}</td>",
                f"<td>{badge}</td>",
                f'<td><span class="bar" style="width:{max(t_share * 80, 0):.0f}px"></span>{_sec(n.self_time)}</td>',
                f'<td><span class="bar" style="width:{max(b_share * 80, 0):.0f}px;background:#1a73e8"></span>{_fmt(n.self_buffers)}</td>',
                f"<td>{_fmt(n.reads)}</td>",
                f"<td>{escape(mem) or '-'}</td>",
            ]
        else:
            c_share = (n.cost / a.total_cost) if (a.total_cost and n.cost is not None) else 0
            cells += [
                f"<td>{_fmt(n.e_rows)}</td>",
                f"<td>{escape(n.raw.get('e_bytes', '-'))}</td>",
                f"<td>{escape(n.raw.get('cost', '-'))}</td>",
                f'<td><span class="bar" style="width:{c_share * 80:.0f}px"></span>{c_share:.0%}</td>',
                f"<td>{escape(n.raw.get('e_time', '-'))}</td>",
            ]
        fs = [f for f in a.findings_for(n.id) if f.rule != "CARDINALITY_MISESTIMATE" or f.node_ids[0] == n.id]
        dots = "".join(
            f'<span class="dot" style="background:{SEVERITY_COLOR[f.severity]}" title="{escape(f.title)}"></span>'
            for f in fs
        )
        cells.append(f'<td class="l">{dots}</td>')
        cls = ' class="inactive"' if n.inactive else ""
        body.append(f'<tr{cls} style="--xpl-bg:{_heat(w)}">{"".join(cells)}</tr>')

    legend = (
        '<div class="xpl-legend">행 배경색: 해당 단계 자체 부하 비중(진할수록 무거움) · '
        '괴리: <span style="color:#188038">■</span> &lt;10배 <span style="color:#e8710a">■</span> 10~100배 '
        '<span style="color:#d93025">■</span> ≥100배 · E-Rows는 11g 기준 Starts 1회당 값이므로 Starts를 곱해 비교합니다.</div>'
        if rt else
        '<div class="xpl-legend">실행 통계가 없어 Cost 비중으로 색을 표시합니다. 정확한 분석을 위해 ALLSTATS LAST 출력을 권장합니다.</div>'
    )
    return f'{_TABLE_CSS}<div class="xpl-wrap">{legend}<table class="xpl"><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


# ---------------------------------------------------------------------------
# SVG 트리 다이어그램
# ---------------------------------------------------------------------------

NODE_W, NODE_H, H_GAP, V_GAP, PAD = 220, 70, 22, 46, 20


def _layout(a: Analysis) -> dict[int, tuple[float, float]]:
    by_id = {n.id: n for n in a.plan.nodes}
    pos: dict[int, tuple[float, float]] = {}
    next_x = [0.0]

    def place(nid: int) -> float:
        node = by_id[nid]
        kids = [c for c in node.children if c in by_id]
        y = node.depth * (NODE_H + V_GAP)
        if not kids:
            x = next_x[0]
            next_x[0] += NODE_W + H_GAP
        else:
            xs = [place(c) for c in kids]
            x = (xs[0] + xs[-1]) / 2
        pos[nid] = (x, y)
        return x

    roots = [n.id for n in a.plan.nodes if n.parent_id is None]
    for r in roots:
        place(r)
    return pos


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render_tree_svg(a: Analysis) -> str:
    p = a.plan
    if not p.nodes:
        return "<p>표시할 실행계획이 없습니다.</p>"
    pos = _layout(a)
    min_x = min(x for x, _ in pos.values())
    width = max(x for x, _ in pos.values()) - min_x + NODE_W + PAD * 2
    height = max(y for _, y in pos.values()) + NODE_H + PAD * 2
    ox = PAD - min_x
    oy = PAD
    by_id = {n.id: n for n in p.nodes}

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="ui-sans-serif,system-ui,\'Segoe UI\',\'Malgun Gothic\',sans-serif">'
    ]
    # 간선: 자식 -> 부모 (데이터가 아래에서 위로 흐름)
    max_rows = max([n.a_rows or n.e_rows or 0 for n in p.nodes] + [1])
    for n in p.nodes:
        if n.parent_id is None or n.parent_id not in pos:
            continue
        x1, y1 = pos[n.id]
        x2, y2 = pos[n.parent_id]
        sx, sy = x1 + ox + NODE_W / 2, y1 + oy
        ex, ey = x2 + ox + NODE_W / 2, y2 + oy + NODE_H
        rows = n.a_rows if n.a_rows is not None else (n.e_rows or 0)
        stroke = 1.5 + 6 * (rows / max_rows) ** 0.5
        my = (sy + ey) / 2
        label = _fmt(rows) if rows else ""
        parts.append(
            f'<path d="M{sx:.1f},{sy:.1f} C{sx:.1f},{my:.1f} {ex:.1f},{my:.1f} {ex:.1f},{ey:.1f}" '
            f'fill="none" stroke="#9aa0a6" stroke-width="{stroke:.1f}" stroke-opacity="0.8"/>'
        )
        if label:
            parts.append(
                f'<text x="{sx + 6:.1f}" y="{sy - 8:.1f}" font-size="10" fill="#80868b">{escape(label)} rows</text>'
            )

    for n in p.nodes:
        x, y = pos[n.id]
        x += ox
        y += oy
        w = _weight(a, n)
        sev = _worst_severity(a, n.id)
        border = SEVERITY_COLOR.get(sev, "#5f6368") if sev else "#5f6368"
        bw = 2.5 if sev in ("critical", "warning") else 1
        dash = ' stroke-dasharray="5,4"' if n.inactive else ""
        line1 = _trunc(f"{n.id}{'*' if n.has_predicate else ''}  {n.operation}", 27)
        line2 = _trunc(n.name, 30) if n.name else ""
        if p.has_runtime_stats:
            ratio = f" · {n.misestimate_ratio:,.0f}x" if n.misestimate_ratio and n.misestimate_ratio >= 10 else ""
            line3 = f"A {_fmt(n.a_rows)} / E {_fmt(n.est_total_rows)}{ratio}"
            line4 = f"{_sec(n.self_time)} · buf {_fmt(n.self_buffers)} · x{_fmt(n.starts)}"
        else:
            line3 = f"Rows {_fmt(n.e_rows)} · Cost {_fmt(n.cost)}"
            line4 = f"Cost 비중 {w:.0%}"
        tip = escape(
            f"Id {n.id} {n.label}\n" + "\n".join(f"{pr.kind}: {pr.text}" for pr in n.predicates)
            + "".join(f"\n[{f.severity}] {f.title}" for f in a.findings_for(n.id))
        )
        parts.append(
            f'<g><title>{tip}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" rx="8" ry="8" width="{NODE_W}" height="{NODE_H}" '
            f'fill="{_heat(w)}" stroke="{border}" stroke-width="{bw}"{dash}/>'
            f'<text x="{x + 10:.1f}" y="{y + 17:.1f}" font-size="12" font-weight="700" fill="#202124">{escape(line1)}</text>'
            f'<text x="{x + 10:.1f}" y="{y + 32:.1f}" font-size="11" fill="#1a73e8">{escape(line2)}</text>'
            f'<text x="{x + 10:.1f}" y="{y + 47:.1f}" font-size="11" fill="#3c4043">{escape(line3)}</text>'
            f'<text x="{x + 10:.1f}" y="{y + 61:.1f}" font-size="11" fill="#5f6368">{escape(line4)}</text>'
            "</g>"
        )
        if sev:
            parts.append(f'<circle cx="{x + NODE_W - 12:.1f}" cy="{y + 12:.1f}" r="6" fill="{SEVERITY_COLOR[sev]}"/>')
    parts.append("</svg>")

    legend = (
        '<div style="font-size:12px;margin:4px 0 8px">위에서 아래로 부모→자식, 데이터는 아래에서 위로 흐릅니다. '
        '선 굵기 = 전달 건수, 박스 색 = 자체 부하 비중, 테두리/점 색 = 진단 심각도. 박스에 마우스를 올리면 조건절과 진단이 보입니다.</div>'
    )
    return f'{legend}<div style="overflow:auto;max-height:720px;border:1px solid rgba(128,128,128,.3);border-radius:8px;background:#fff">{"".join(parts)}</div>'
