"""파생 지표 계산과 Oracle 11g 튜닝 탐지 규칙."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .models import Plan, PlanNode

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

# 추정/실제 괴리 판정 기준
MISESTIMATE_WARN = 10.0
MISESTIMATE_CRIT = 100.0
MISESTIMATE_MIN_ROWS = 100  # 둘 다 작으면 무시
HIGH_STARTS = 1000
HOTSPOT_SHARE = 0.30
FULLSCAN_BUFFER_SHARE = 0.20


@dataclass
class Finding:
    rule: str
    severity: str  # critical | warning | info
    title: str
    detail: str
    suggestion: str = ""
    node_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    plan: Plan
    findings: list[Finding]
    total_time: Optional[float]
    total_buffers: Optional[float]
    total_cost: Optional[float]

    def findings_for(self, node_id: int) -> list[Finding]:
        return [f for f in self.findings if node_id in f.node_ids]

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
            "total_time": self.total_time,
            "total_buffers": self.total_buffers,
            "total_cost": self.total_cost,
        }


# ---------------------------------------------------------------------------
# 파생 지표
# ---------------------------------------------------------------------------

def _is_partition_iterator(node: Optional[PlanNode]) -> bool:
    return node is not None and (node.operation.startswith("PARTITION ") or node.operation.startswith("PX "))


def compute_derived(plan: Plan) -> None:
    by_id = {n.id: n for n in plan.nodes}
    for node in plan.nodes:
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        if node.e_rows is not None:
            # 11g 의 E-Rows 는 Starts 1회당 추정치. 단, 파티션/PX 반복자 하위는 이미 전체 추정치.
            if node.starts and node.starts > 1 and not _is_partition_iterator(parent):
                node.est_total_rows = node.e_rows * node.starts
            else:
                node.est_total_rows = node.e_rows
        if node.a_rows is not None and node.est_total_rows is not None:
            a = max(node.a_rows, 1.0)
            e = max(node.est_total_rows, 1.0)
            node.misestimate_ratio = max(a / e, e / a)
            node.underestimated = a > e

        kids = [by_id[c] for c in node.children if c in by_id]
        if node.a_time is not None:
            child_t = sum(k.a_time or 0.0 for k in kids)
            node.self_time = max(node.a_time - child_t, 0.0)
        if node.buffers is not None:
            child_b = sum(k.buffers or 0.0 for k in kids)
            node.self_buffers = max(node.buffers - child_b, 0.0)


# ---------------------------------------------------------------------------
# 규칙
# ---------------------------------------------------------------------------

def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "-"
    for unit, div in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div * 10:
            return f"{v / div:.0f}{unit}"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _pct(part: float, total: Optional[float]) -> float:
    return part / total if total else 0.0


def _preds(node: PlanNode, kind: Optional[str] = None) -> str:
    return " ".join(p.text for p in node.predicates if kind is None or p.kind == kind)


_CONVERSION_RE = re.compile(r"(INTERNAL_FUNCTION|TO_NUMBER|TO_CHAR|TO_DATE|SYS_OP_C2C)\s*\(\s*\"", re.I)


def run_rules(plan: Plan) -> Analysis:
    compute_derived(plan)
    findings: list[Finding] = []
    nodes = [n for n in plan.nodes if not n.inactive]
    by_id = {n.id: n for n in plan.nodes}
    root = plan.root
    runtime = plan.has_runtime_stats

    total_time = root.a_time if root and root.a_time is not None else None
    if total_time is None and nodes:
        times = [n.a_time for n in nodes if n.a_time is not None]
        total_time = max(times) if times else None
    total_buffers = root.buffers if root and root.buffers is not None else None
    total_cost = root.cost if root and root.cost is not None else None

    if not nodes:
        return Analysis(plan, findings, total_time, total_buffers, total_cost)

    # R01 실행 통계 없음
    if not runtime:
        findings.append(Finding(
            "NO_RUNTIME_STATS", "info",
            "실제 실행 통계(A-Rows)가 없습니다",
            "EXPLAIN PLAN 또는 통계 수집 없이 실행된 커서입니다. 옵티마이저의 추정치(Rows/Cost)만으로 분석하므로 "
            "실제 병목과 다를 수 있습니다. EXPLAIN PLAN 은 바인드 변수 피킹을 하지 않아 실제 실행계획과 다를 수도 있습니다.",
            "SQL 에 /*+ GATHER_PLAN_STATISTICS */ 힌트를 넣거나 세션에서 ALTER SESSION SET STATISTICS_LEVEL = ALL; "
            "후 실행하고, SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST +PEEKED_BINDS')); "
            "결과를 붙여넣으세요.",
        ))

    # R02 카디널리티 추정 오류 - 괴리가 처음 생긴 단계(자식에 같은 방향 괴리가 없는 단계)만 보고
    def _misestimated(n: PlanNode) -> bool:
        return (
            n.misestimate_ratio is not None
            and n.misestimate_ratio >= MISESTIMATE_WARN
            and max(n.a_rows or 0, n.est_total_rows or 0) >= MISESTIMATE_MIN_ROWS
        )

    def _inherited_by(n: PlanNode) -> list[int]:
        """n 의 괴리가 전파된 조상 Id 목록."""
        out = []
        cur = n
        while cur.parent_id is not None:
            parent = by_id[cur.parent_id]
            if not (_misestimated(parent) and parent.underestimated == n.underestimated):
                break
            out.append(parent.id)
            cur = parent
        return out

    for n in nodes:
        if not _misestimated(n):
            continue
        kids = [by_id[c] for c in n.children if c in by_id]
        if any(_misestimated(k) and k.underestimated == n.underestimated for k in kids):
            continue
        r = n.misestimate_ratio
        sev = "critical" if r >= MISESTIMATE_CRIT else "warning"
        upward = _inherited_by(n)
        propagated = f" 이 괴리가 상위 단계 Id {', '.join(map(str, upward))} 로 전파되었습니다." if upward else ""
        direction = "과소" if n.underestimated else "과대"
        starts_note = f" (E-Rows {_fmt(n.e_rows)} × Starts {_fmt(n.starts)})" if n.starts and n.starts > 1 and n.est_total_rows != n.e_rows else ""
        findings.append(Finding(
            "CARDINALITY_MISESTIMATE", sev,
            f"Id {n.id} {n.label}: 카디널리티 {direction} 추정 {r:,.0f}배",
            f"추정 {_fmt(n.est_total_rows)}건{starts_note} vs 실제 {_fmt(n.a_rows)}건. "
            "추정이 틀리면 조인 순서·조인 방식·메모리 할당이 잘못 결정됩니다. 괴리가 처음 시작된 이 단계가 근본 원인입니다." + propagated,
            "DBMS_STATS.GATHER_TABLE_STATS 로 통계를 최신화하고, 값 분포가 치우친 컬럼에는 히스토그램(METHOD_OPT => 'FOR COLUMNS SIZE 254 col'), "
            "상관관계가 있는 다중 컬럼 조건에는 확장 통계(DBMS_STATS.CREATE_EXTENDED_STATS)를 검토하세요. "
            "임시 조치로 /*+ DYNAMIC_SAMPLING(alias 4) */ 또는 /*+ CARDINALITY(alias n) */ 힌트를 사용할 수 있습니다.",
            [n.id] + upward,
        ))

    # R03 NL/FILTER 내부의 과도한 반복 실행
    for n in nodes:
        if not n.starts or n.starts < HIGH_STARTS:
            continue
        parent = by_id.get(n.parent_id) if n.parent_id is not None else None
        if parent is None or _is_partition_iterator(parent):
            continue
        if parent.starts and n.starts <= parent.starts * 1.5:
            continue  # 부모와 같은 횟수로 반복되는 하위 단계는 부모 쪽에서 보고
        if parent.operation.startswith("NESTED LOOPS") or parent.operation == "FILTER":
            ctx = "NESTED LOOPS 내부" if parent.operation.startswith("NESTED LOOPS") else "FILTER 서브쿼리"
            findings.append(Finding(
                "HIGH_STARTS", "warning",
                f"Id {n.id} {n.label}: {ctx}에서 {_fmt(n.starts)}회 반복 실행",
                f"선행 집합의 건수만큼 이 단계가 반복됩니다. 1회당 비용이 작아도 누적 Buffers {_fmt(n.buffers)}, "
                f"A-Time {n.a_time if n.a_time is not None else '-'}초가 발생했습니다.",
                "선행 집합이 크다면 /*+ USE_HASH(alias) */ 로 해시 조인을 검토하거나, 조인 순서(/*+ LEADING */)를 바꿔 반복 횟수를 줄이세요. "
                "FILTER 서브쿼리라면 /*+ UNNEST */ 로 조인 변환을 유도하거나 EXISTS/IN 을 조인으로 재작성하세요. "
                "반복이 불가피하면 조인 컬럼 인덱스에 SELECT 컬럼까지 포함시켜 테이블 랜덤 액세스를 없애는 방법도 있습니다.",
                [n.id],
            ))

    # R04 인덱스 스캔 후 테이블 필터로 대부분 버려짐
    for n in nodes:
        if not n.operation.startswith("TABLE ACCESS BY") or not n.children:
            continue
        child = by_id.get(n.children[0])
        if child is None or not child.operation.startswith("INDEX"):
            continue
        if child.a_rows is None or n.a_rows is None or not _preds(n, "filter"):
            continue
        if child.a_rows >= 1000 and child.a_rows >= max(n.a_rows, 1) * 10:
            findings.append(Finding(
                "INDEX_FILTER_WASTE", "warning",
                f"Id {n.id} {n.label}: 인덱스로 {_fmt(child.a_rows)}건 읽고 {_fmt(n.a_rows)}건만 남김",
                f"인덱스 {child.name} 로 찾은 ROWID 대부분이 테이블 필터 조건({_preds(n, 'filter')})에서 버려집니다. "
                "버려지는 행만큼 불필요한 테이블 랜덤 액세스(single block read)가 발생합니다.",
                f"필터 조건 컬럼을 인덱스 {child.name} 에 추가한 결합 인덱스(선두 컬럼은 '=' 조건 컬럼)를 검토하세요.",
                [n.id, child.id],
            ))

    # R05 시간 / 버퍼 핫스팟
    if total_time:
        for n in nodes:
            if n.self_time is not None and n.self_time >= total_time * HOTSPOT_SHARE and total_time >= 0.1:
                findings.append(Finding(
                    "TIME_HOTSPOT", "warning",
                    f"Id {n.id} {n.label}: 전체 수행시간의 {_pct(n.self_time, total_time):.0%} 차지",
                    f"이 단계 자체 시간(하위 단계 제외) {n.self_time:.2f}초 / 전체 {total_time:.2f}초.",
                    "튜닝 우선순위가 가장 높은 단계입니다. 이 단계의 Starts, Buffers, Reads 와 조건절을 함께 확인하세요.",
                    [n.id],
                ))
    if total_buffers:
        for n in nodes:
            if n.self_buffers is not None and n.self_buffers >= total_buffers * HOTSPOT_SHARE and total_buffers >= 1000:
                findings.append(Finding(
                    "BUFFER_HOTSPOT", "info",
                    f"Id {n.id} {n.label}: 전체 논리 I/O의 {_pct(n.self_buffers, total_buffers):.0%} 차지",
                    f"자체 Buffers {_fmt(n.self_buffers)} / 전체 {_fmt(total_buffers)}.",
                    "블록 접근량을 줄이는 것이 핵심입니다(인덱스 선택도 개선, 불필요한 테이블 액세스 제거, 파티션 프루닝).",
                    [n.id],
                ))

    # R06 조건이 있는 FULL SCAN
    for n in nodes:
        if n.operation not in ("TABLE ACCESS FULL", "INDEX FAST FULL SCAN") or not _preds(n, "filter"):
            continue
        heavy = False
        if runtime and total_buffers and n.self_buffers is not None:
            heavy = n.self_buffers >= total_buffers * FULLSCAN_BUFFER_SHARE and n.self_buffers >= 1000
            if heavy and n.a_rows is not None and n.self_buffers and n.a_rows > n.self_buffers:
                heavy = False  # 블록당 1건 이상 반환하면 FULL SCAN 이 합리적일 수 있음
        elif not runtime and total_cost and n.cost is not None:
            heavy = n.cost >= total_cost * 0.5 and n.cost >= 100
        if heavy:
            findings.append(Finding(
                "FULL_SCAN_WITH_FILTER", "warning",
                f"Id {n.id} {n.label}: 조건이 있는데 전체 스캔",
                f"필터 조건: {_preds(n, 'filter')}",
                "조건의 선택도가 좋다면(전체의 수 % 이하) 해당 컬럼 인덱스를 검토하세요. 이미 인덱스가 있다면 컬럼 가공·형변환·"
                "NULL 비교·부정 조건 때문에 인덱스를 쓰지 못하는지 확인하세요.",
                [n.id],
            ))

    # R07 카티션 조인
    for n in nodes:
        if "CARTESIAN" in n.operation:
            findings.append(Finding(
                "CARTESIAN_JOIN", "critical",
                f"Id {n.id} {n.label}: 카티션 곱 조인",
                "조인 조건 없이 두 집합의 모든 조합을 만듭니다. 조인 조건 누락이거나, 한쪽이 1건이라고 잘못 추정해 선택된 경우가 많습니다.",
                "WHERE 절 조인 조건 누락 여부를 먼저 확인하세요. 누락이 아니라면 선행 테이블의 카디널리티 추정 오류(통계)를 확인하세요.",
                [n.id],
            ))

    # R08 묵시적/명시적 형변환
    for n in nodes:
        text = _preds(n)
        m = _CONVERSION_RE.search(text)
        if m:
            fn = m.group(1).upper()
            findings.append(Finding(
                "IMPLICIT_CONVERSION", "warning",
                f"Id {n.id} {n.label}: 컬럼에 {fn}() 적용",
                f"조건절: {text[:300]}",
                "컬럼 쪽에 함수/형변환이 적용되면 해당 컬럼 인덱스를 사용할 수 없습니다. 바인드 변수·리터럴 타입을 컬럼 타입과 맞추세요"
                "(예: VARCHAR2 컬럼에 숫자 비교 금지). 불가피하면 함수 기반 인덱스를 검토하세요.",
                [n.id],
            ))

    # R09 FILTER 서브쿼리 (Unnesting 실패)
    for n in nodes:
        if n.operation == "FILTER" and len(n.children) >= 2:
            findings.append(Finding(
                "FILTER_SUBQUERY", "warning",
                f"Id {n.id} FILTER: 서브쿼리가 조인으로 변환되지 않음",
                "두 번째 이후 자식 단계가 메인 쿼리 행마다(캐싱 제외) 반복 실행될 수 있습니다.",
                "/*+ UNNEST */ 힌트(서브쿼리 안)나 세미/안티 조인으로의 재작성을 검토하세요. 서브쿼리 상관 컬럼에 인덱스가 있는지도 확인하세요.",
                [n.id] + n.children[1:],
            ))

    # R10 TEMP 사용 (디스크 정렬/해시)
    for n in nodes:
        passes = n.used_mem_passes
        if (n.used_tmp and n.used_tmp > 0) or (passes is not None and passes >= 1):
            sev = "critical" if passes is not None and passes >= 2 else "warning"
            mode = "multi-pass" if passes is not None and passes >= 2 else "one-pass"
            findings.append(Finding(
                "TEMP_SPILL", sev,
                f"Id {n.id} {n.label}: PGA 부족으로 TEMP 사용({mode})",
                f"Used-Mem {n.raw.get('used_mem', '-')}, Used-Tmp {n.raw.get('used_tmp', '-')}. 디스크 I/O 가 추가로 발생합니다.",
                "정렬/해시 대상 집합을 줄이는 것(조건 선적용, 필요한 컬럼만 SELECT)이 우선입니다. 해시 조인이라면 작은 집합이 Build(첫 번째 자식)가 "
                "되도록 /*+ LEADING */ 또는 /*+ SWAP_JOIN_INPUTS */ 를 검토하고, PGA_AGGREGATE_TARGET 또는 세션 단위 WORKAREA_SIZE_POLICY=MANUAL + "
                "HASH_AREA_SIZE/SORT_AREA_SIZE 조정을 검토하세요.",
                [n.id],
            ))

    # R11 파티션 프루닝 없음
    for n in nodes:
        if n.operation in ("PARTITION RANGE ALL", "PARTITION LIST ALL", "PARTITION HASH ALL"):
            findings.append(Finding(
                "NO_PARTITION_PRUNING", "warning",
                f"Id {n.id} {n.label}: 모든 파티션 스캔",
                f"Pstart {n.pstart or '-'} ~ Pstop {n.pstop or '-'}. 파티션 키 조건이 없거나 가공되어 프루닝이 되지 않았습니다.",
                "파티션 키 컬럼에 대한 조건을 추가하고, 키 컬럼을 함수로 가공하지 않았는지 확인하세요.",
                [n.id],
            ))

    # R12 주의가 필요한 인덱스 스캔 유형
    for n in nodes:
        if n.operation == "INDEX SKIP SCAN":
            findings.append(Finding(
                "INDEX_SKIP_SCAN", "info",
                f"Id {n.id} {n.label}: INDEX SKIP SCAN",
                "결합 인덱스의 선두 컬럼 조건 없이 사용되었습니다. 선두 컬럼의 값 종류가 많으면 비효율적입니다.",
                "선두 컬럼 조건을 추가하거나, 조건 컬럼이 선두인 인덱스를 검토하세요.",
                [n.id],
            ))
        elif n.operation == "INDEX FULL SCAN" and n.parent_id is not None:
            parent = by_id.get(n.parent_id)
            if parent and not parent.operation.startswith("SORT"):
                findings.append(Finding(
                    "INDEX_FULL_SCAN", "info",
                    f"Id {n.id} {n.label}: INDEX FULL SCAN",
                    "인덱스 전체를 single block 으로 순차 읽습니다. 정렬 회피 목적이 아니라면 비효율적일 수 있습니다.",
                    "정렬 회피가 목적이 아니라면 INDEX FAST FULL SCAN(/*+ INDEX_FFS */) 또는 FULL SCAN 과 비교해 보세요.",
                    [n.id],
                ))

    # R13 Note 섹션 해석
    findings.extend(_note_findings(plan))

    # R14 바인드 피킹
    if plan.peeked_binds:
        binds = ", ".join(f"{b.name}={b.value}" for b in plan.peeked_binds)
        findings.append(Finding(
            "PEEKED_BINDS", "info",
            f"바인드 피킹 값 {len(plan.peeked_binds)}개",
            f"하드 파싱 시점에 엿본 값: {binds}. 이 실행계획은 이 값 기준으로 최적화되었습니다.",
            "실제 자주 쓰이는 값과 분포가 크게 다르면(예: 소수 값/다수 값) 다른 값에서 성능이 급변할 수 있습니다. "
            "11g 의 Adaptive Cursor Sharing(V$SQL.IS_BIND_SENSITIVE / IS_BIND_AWARE)과 히스토그램 유무를 확인하세요.",
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.node_ids[0] if f.node_ids else -1))
    return Analysis(plan, findings, total_time, total_buffers, total_cost)


_NOTE_RULES = [
    (re.compile(r"dynamic sampling used", re.I), "info", "DYNAMIC_SAMPLING",
     "동적 샘플링 사용", "통계가 없는 테이블(또는 OPTIMIZER_DYNAMIC_SAMPLING 설정)이 있어 실행 시점에 샘플링했습니다.",
     "통계가 없는 테이블이면 DBMS_STATS 로 통계를 수집하세요. 임시 테이블(GTT)이라면 정상일 수 있습니다."),
    (re.compile(r"cardinality feedback used", re.I), "warning", "CARDINALITY_FEEDBACK",
     "Cardinality Feedback 사용 (11.2)", "첫 실행의 추정 오류를 보고 두 번째 실행부터 실행계획이 바뀌었습니다. 실행 때마다 성능이 달라질 수 있습니다.",
     "근본 원인인 추정 오류(통계/히스토그램/확장 통계)를 해결하세요. 계획 고정이 필요하면 SQL Plan Baseline 을 검토하세요."),
    (re.compile(r"SQL plan baseline .* used", re.I), "info", "SPM_BASELINE",
     "SQL Plan Baseline 적용", "SPM 으로 고정된 실행계획입니다. 통계·인덱스를 바꿔도 계획이 바뀌지 않을 수 있습니다.",
     "개선안을 적용할 때는 DBMS_SPM 으로 새 계획을 진화(evolve)시키거나 Baseline 을 수정해야 합니다."),
    (re.compile(r"SQL profile .* used", re.I), "info", "SQL_PROFILE",
     "SQL Profile 적용", "SQL Tuning Advisor 등으로 만든 Profile 이 적용되어 있습니다.",
     "Profile 이 오래되었다면 현재 데이터 기준으로 유효한지 재검토하세요."),
    (re.compile(r"outline .* used", re.I), "info", "STORED_OUTLINE",
     "Stored Outline 적용", "Stored Outline 으로 고정된 계획입니다. 11g 에서는 SQL Plan Baseline 으로 이관을 권장합니다.", ""),
    (re.compile(r"rule based optimizer used", re.I), "warning", "RBO",
     "RBO(Rule Based Optimizer) 사용", "/*+ RULE */ 힌트나 통계 부재로 RBO 가 사용되었습니다. 11g 에서 RBO 는 지원 중단 기능입니다.",
     "RULE 힌트를 제거하고 통계를 수집해 CBO 로 전환하세요."),
    (re.compile(r"'PLAN_TABLE' is old version", re.I), "info", "OLD_PLAN_TABLE",
     "구버전 PLAN_TABLE", "구버전 PLAN_TABLE 이라 일부 정보가 표시되지 않을 수 있습니다.",
     "@?/rdbms/admin/utlxplan.sql 로 PLAN_TABLE 을 재생성하거나 SYS.PLAN_TABLE$(공용 시노님)를 사용하세요."),
    (re.compile(r"cpu costing is off", re.I), "info", "CPU_COSTING_OFF",
     "CPU Costing 꺼짐", "시스템 통계가 없거나 비활성 상태라 I/O 비용만으로 계산되었습니다.", "DBMS_STATS.GATHER_SYSTEM_STATS 를 검토하세요."),
    (re.compile(r"Warning: basic plan statistics not available", re.I), "info", "NO_PLAN_STATS",
     "실행 통계 미수집", "STATISTICS_LEVEL=ALL 또는 GATHER_PLAN_STATISTICS 힌트 없이 실행되었습니다.",
     "/*+ GATHER_PLAN_STATISTICS */ 힌트를 추가해 다시 실행하세요."),
    (re.compile(r"automatic DOP|Degree of Parallelism", re.I), "info", "PARALLEL",
     "병렬 처리 정보", "병렬도 관련 Note 입니다. 이 도구는 PX 단계의 TQ/IN-OUT 정보는 참고용으로만 보여줍니다.", ""),
    (re.compile(r"this is an adaptive plan|statistics feedback|SQL plan directive|performance feedback", re.I), "info", "UNSUPPORTED_12C",
     "12c 이상 기능 (미지원)", "Oracle 12c 이상에서만 나타나는 Note 입니다. 이 도구는 11gR2 출력만 해석합니다.", ""),
]


def _note_findings(plan: Plan) -> list[Finding]:
    out: list[Finding] = []
    for note in plan.notes:
        for rx, sev, rule, title, detail, sugg in _NOTE_RULES:
            if rx.search(note):
                out.append(Finding(rule, sev, f"Note: {title}", f"{detail} (원문: {note})", sugg))
                break
        else:
            plan.unsupported.append(f"해석하지 못한 Note: {note}")
    return out


def analyze(text: str) -> Analysis:
    """텍스트 -> 파싱 -> 규칙 분석."""
    from .parser import parse_plan

    return run_rules(parse_plan(text))
