"""두 실행계획(튜닝 전/후) 규칙 기반 비교."""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .rules import SEVERITY_ORDER, Analysis, _fmt

_JOIN_PREFIXES = ("NESTED LOOPS", "HASH JOIN", "MERGE JOIN", "SORT MERGE")


def _change(before: Optional[float], after: Optional[float]) -> str:
    if before is None or after is None:
        return "-"
    if before == 0:
        return "-" if after == 0 else "▲ 신규"
    pct = (after - before) / before * 100
    if abs(pct) < 0.5:
        return "변화 없음"
    arrow = "▼" if pct < 0 else "▲"
    good = "✅" if pct < 0 else "⚠️"
    return f"{arrow} {abs(pct):,.1f}% {good}"


def _access_paths(a: Analysis) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for n in a.plan.nodes:
        if n.name and not n.inactive:
            out[n.name].append(n.operation)
    return out


def _joins(a: Analysis) -> list[str]:
    return [f"{n.operation}" for n in a.plan.nodes if n.operation.startswith(_JOIN_PREFIXES)]


def _finding_keys(a: Analysis) -> dict[str, str]:
    by_id = {n.id: n for n in a.plan.nodes}
    keys = {}
    for f in a.findings:
        label = by_id[f.node_ids[0]].label if f.node_ids and f.node_ids[0] in by_id else ""
        keys[f"{f.rule}|{label}"] = f"{f.severity}|{f.title}"
    return keys


def compare_plans(before: Analysis, after: Analysis) -> str:
    """Markdown 비교 보고서."""
    if not before.plan.nodes or not after.plan.nodes:
        return "### ⚠️ 두 실행계획을 모두 해석할 수 있어야 비교할 수 있습니다."

    b, a = before.plan, after.plan
    out = ["### ⚖️ 실행계획 비교 (Before → After)"]
    if b.plan_hash_value and a.plan_hash_value:
        same = b.plan_hash_value == a.plan_hash_value
        out.append(f"Plan hash value: `{b.plan_hash_value}` → `{a.plan_hash_value}` "
                   + ("(**동일한 실행계획** — 차이는 데이터/캐시 영향일 수 있습니다)" if same else "(실행계획 변경됨)"))

    def _root(an: Analysis, attr: str) -> Optional[float]:
        return getattr(an.plan.root, attr) if an.plan.root else None

    def _sum(an: Analysis, attr: str) -> Optional[float]:
        vals = [getattr(n, attr) for n in an.plan.nodes if getattr(n, attr) is not None]
        return max(vals) if vals else None

    metrics = [
        ("수행시간(초)", before.total_time, after.total_time),
        ("Buffers (논리 I/O)", before.total_buffers, after.total_buffers),
        ("Reads (물리 I/O)", _sum(before, "reads"), _sum(after, "reads")),
        ("최종 A-Rows", _root(before, "a_rows"), _root(after, "a_rows")),
        ("Cost (추정)", before.total_cost, after.total_cost),
        ("단계 수", float(len(b.nodes)), float(len(a.nodes))),
    ]
    out += ["", "| 지표 | Before | After | 변화 |", "|---|---:|---:|---|"]
    for label, x, y in metrics:
        if x is None and y is None:
            continue
        chg = _change(x, y) if label not in ("최종 A-Rows", "단계 수") else ("동일" if x == y else "다름")
        out.append(f"| {label} | {_fmt(x)} | {_fmt(y)} | {chg} |")

    if before.total_time is None or after.total_time is None:
        out.append("\n> 한쪽 이상에 실행 통계(A-Rows/A-Time)가 없어 Cost 등 추정치 위주로 비교합니다. "
                   "ALLSTATS LAST 출력끼리 비교하는 것을 권장합니다.")
    ra, rb = _root(before, "a_rows"), _root(after, "a_rows")
    if ra is not None and rb is not None and ra != rb:
        out.append(f"\n> ⚠️ 최종 결과 건수가 다릅니다({_fmt(ra)} → {_fmt(rb)}). 같은 조건·바인드 값으로 실행했는지, SQL 결과가 동일한지 확인하세요.")

    # 접근 경로 변화
    pb, pa = _access_paths(before), _access_paths(after)
    changes = []
    for obj in sorted(set(pb) | set(pa)):
        ob, oa = pb.get(obj, []), pa.get(obj, [])
        if sorted(ob) != sorted(oa):
            changes.append(f"| `{obj}` | {', '.join(ob) or '(없음)'} | {', '.join(oa) or '(없음)'} |")
    if changes:
        out += ["", "#### 접근 경로 변화", "| 오브젝트 | Before | After |", "|---|---|---|"] + changes

    jb, ja = _joins(before), _joins(after)
    if jb != ja:
        out += ["", "#### 조인 방식 변화", f"- Before: {' → '.join(jb) or '(조인 없음)'}", f"- After: {' → '.join(ja) or '(조인 없음)'}"]

    # 진단 변화
    fb, fa = _finding_keys(before), _finding_keys(after)
    resolved = [fb[k].split("|", 1)[1] for k in fb if k not in fa and not fb[k].startswith("info")]
    new = [fa[k] for k in fa if k not in fb and not fa[k].startswith("info")]
    new.sort(key=lambda s: SEVERITY_ORDER.get(s.split("|")[0], 9))
    out += ["", "#### 진단 변화"]
    if resolved:
        out += ["**해결된 문제**"] + [f"- ✅ {t}" for t in resolved]
    if new:
        out += ["**새로 생긴 문제**"] + [f"- {'🔴' if s.startswith('critical') else '🟠'} {s.split('|', 1)[1]}" for s in new]
    if not resolved and not new:
        out.append("- 규칙 진단 결과에는 변화가 없습니다.")
    return "\n".join(out)
