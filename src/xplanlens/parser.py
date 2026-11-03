"""Oracle 11gR2 DBMS_XPLAN 텍스트 출력 파서.

지원 대상
- DBMS_XPLAN.DISPLAY        (EXPLAIN PLAN 결과: Rows/Bytes/Cost/Time)
- DBMS_XPLAN.DISPLAY_CURSOR (ALLSTATS LAST, +PEEKED_BINDS 등)
- DBMS_XPLAN.DISPLAY_AWR    (형식이 DISPLAY_CURSOR 와 동일)

해석할 수 없는 행·섹션은 예외를 던지지 않고 Plan.unsupported 에 기록한다.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import PeekedBind, Plan, PlanNode, Predicate

# 헤더 이름 -> 정규화 키
_COLUMN_KEYS = {
    "id": "id",
    "operation": "operation",
    "name": "name",
    "rows": "e_rows",
    "e-rows": "e_rows",
    "bytes": "e_bytes",
    "e-bytes": "e_bytes",
    "cost (%cpu)": "cost",
    "cost": "cost",
    "time": "e_time",
    "e-time": "e_time",
    "starts": "starts",
    "a-rows": "a_rows",
    "a-time": "a_time",
    "buffers": "buffers",
    "reads": "reads",
    "writes": "writes",
    "omem": "omem",
    "1mem": "one_mem",
    "used-mem": "used_mem",
    "used-tmp": "used_tmp",
    "tempspc": "temp_spc",
    "pstart": "pstart",
    "pstop": "pstop",
    "tq": "tq",
    "in-out": "in_out",
    "pq distrib": "pq_distrib",
    "max-tmp": "max_tmp",
    "o/1/m": "o1m",
    "inst": "inst",
}

_KNOWN_SECTIONS = {
    "predicate information": "predicates",
    "peeked binds": "binds",
    "note": "note",
    "column projection information": "skip",
    "query block name / object alias": "skip",
    "outline data": "skip",
    "remote sql information": "skip",
}

_DASH_RE = re.compile(r"^\s*-{3,}\s*$")
_SQL_ID_RE = re.compile(r"SQL_ID\s*:?\s*([0-9a-z]{13})(?:\s*,\s*child number\s*(\d+))?", re.I)
_HASH_RE = re.compile(r"Plan hash value:\s*(\d+)", re.I)
_PRED_RE = re.compile(r"^\s*(\d+)\s*-\s*(access|filter)\((.*)$", re.I)
_PRED_CONT_RE = re.compile(r"^\s+(access|filter)\((.*)$", re.I)
_BIND_RE = re.compile(r"^\s*(\d+)\s*-\s*(:\S+)\s*\((.*?)\):\s*(.*)$")
_NUM_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTPE]?)\s*$", re.I)
_MEM_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTPE]?)\s*(?:\((\d+)\))?\s*$", re.I)
_COST_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTPE]?)\s*(?:\((\d+)\))?\s*$", re.I)

_UNIT_1000 = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
_UNIT_1024 = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5, "E": 1024**6}


def parse_count(text: str) -> Optional[float]:
    """'48210', '193K', '1502K', '2M' -> 숫자 (K=1000). 빈 값이면 None."""
    if text is None:
        return None
    m = _NUM_RE.match(text)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_1000[m.group(2).upper()]


def parse_bytes(text: str) -> Optional[float]:
    """'1850', '11M', '9216K' -> 바이트 (K=1024)."""
    if text is None:
        return None
    m = _NUM_RE.match(text)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_1024[m.group(2).upper()]


def parse_mem(text: str) -> tuple[Optional[float], Optional[int]]:
    """Used-Mem '9876K (0)' -> (바이트, pass 수)."""
    if not text or not text.strip():
        return None, None
    m = _MEM_RE.match(text)
    if not m:
        return None, None
    value = float(m.group(1)) * _UNIT_1024[m.group(2).upper()]
    passes = int(m.group(3)) if m.group(3) is not None else None
    return value, passes


def parse_cost(text: str) -> tuple[Optional[float], Optional[float]]:
    """'  2186K  (1)' -> (2186000, 1)."""
    if not text or not text.strip():
        return None, None
    m = _COST_RE.match(text)
    if not m:
        return None, None
    cost = float(m.group(1)) * _UNIT_1000[m.group(2).upper()]
    cpu = float(m.group(3)) if m.group(3) is not None else None
    return cost, cpu


def parse_duration(text: str) -> Optional[float]:
    """'00:00:41.37' 또는 '07:17:21' -> 초."""
    if not text or not text.strip():
        return None
    parts = text.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _split_row(line: str) -> list[str]:
    s = line.rstrip()
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _is_table_header(line: str) -> bool:
    if "|" not in line:
        return False
    cells = [c.strip().lower() for c in _split_row(line)]
    return "id" in cells and "operation" in cells


def _strip_outer_paren(expr: str) -> str:
    """'"A"=1)' 처럼 마지막 닫는 괄호가 access( / filter( 에 대응하면 제거."""
    expr = expr.strip()
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    if depth < 0 and expr.endswith(")"):
        return expr[:-1].rstrip()
    return expr


def _section_of(line: str, next_line: str) -> Optional[str]:
    """섹션 제목 줄이면 섹션 키를 반환. 미지원 섹션은 'unknown:<제목>'."""
    if not _DASH_RE.match(next_line or ""):
        return None
    title = line.strip()
    if not title or "|" in title:
        return None
    lowered = title.lower()
    for prefix, key in _KNOWN_SECTIONS.items():
        if lowered.startswith(prefix):
            return key
    if lowered.startswith("sql_id") or lowered.startswith("plan hash value") or lowered.startswith("plan_table_output"):
        return None
    return "unknown:" + title


def parse_plan(text: str) -> Plan:
    plan = Plan()
    if not text or not text.strip():
        plan.unsupported.append("입력이 비어 있습니다.")
        return plan

    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").split("\n")
    n = len(lines)

    # 1) 헤더: SQL_ID, SQL 텍스트, Plan hash value
    header_idx = next((i for i, ln in enumerate(lines) if _is_table_header(ln)), None)
    head_lines = lines[: header_idx if header_idx is not None else n]
    sql_lines: list[str] = []
    in_sql = False
    for ln in head_lines:
        m = _SQL_ID_RE.search(ln)
        if m and not plan.sql_id:
            plan.sql_id = m.group(1)
            if m.group(2) is not None:
                plan.child_number = int(m.group(2))
            in_sql = True
            continue
        hm = _HASH_RE.search(ln)
        if hm:
            plan.plan_hash_value = hm.group(1)
            in_sql = False
            continue
        if in_sql:
            if _DASH_RE.match(ln):
                continue
            if ln.strip():
                sql_lines.append(ln.strip())
    plan.sql_text = "\n".join(sql_lines)

    if header_idx is None:
        plan.unsupported.append("실행계획 표(| Id | Operation | ...)를 찾지 못했습니다. DBMS_XPLAN 텍스트 출력 전체를 붙여넣어 주세요.")
        return plan

    # 2) 실행계획 표
    raw_headers = [c.strip() for c in _split_row(lines[header_idx])]
    keys = []
    for h in raw_headers:
        key = _COLUMN_KEYS.get(h.lower())
        if key is None:
            plan.unsupported.append(f"알 수 없는 컬럼 '{h}' 은(는) 원문 값만 보존합니다.")
            key = "x_" + re.sub(r"\W+", "_", h.lower()).strip("_")
        keys.append(key)
    plan.columns = raw_headers

    i = header_idx + 1
    rows: list[tuple[list[str], str]] = []
    while i < n:
        ln = lines[i]
        if _DASH_RE.match(ln):
            if rows:
                i += 1
                break
            i += 1
            continue
        if not ln.strip():
            if rows:
                break
            i += 1
            continue
        if "|" not in ln:
            break
        rows.append((_split_row(ln), ln))
        i += 1
    table_end = i

    op_idx = keys.index("operation")
    id_idx = keys.index("id")
    indents: list[int] = []
    parsed_rows = []
    for cells, original in rows:
        if len(cells) != len(keys):
            plan.unsupported.append(f"컬럼 수가 맞지 않는 행을 건너뜀: {original.strip()[:80]}")
            continue
        id_cell = cells[id_idx]
        m = re.match(r"^\s*([*\-]*)\s*(\d+)\s*$", id_cell)
        if not m:
            plan.unsupported.append(f"Id 를 해석할 수 없는 행을 건너뜀: {original.strip()[:80]}")
            continue
        op_cell = cells[op_idx]
        indent = len(op_cell) - len(op_cell.lstrip(" "))
        indents.append(indent)
        parsed_rows.append((m.group(1), int(m.group(2)), indent, cells))

    base_indent = min(indents) if indents else 0
    stack: list[PlanNode] = []
    for flags, node_id, indent, cells in parsed_rows:
        values = {k: v for k, v in zip(keys, cells)}
        node = PlanNode(
            id=node_id,
            depth=indent - base_indent,
            operation=values["operation"].strip(),
            name=values.get("name", "").strip(),
            has_predicate="*" in flags,
            inactive="-" in flags,
            raw={k: v.strip() for k, v in values.items() if k not in ("id", "operation", "name") and v.strip()},
        )
        if node.inactive:
            msg = "'-' 표시된 비활성 행(12c Adaptive Plan)은 11g 분석 대상이 아닙니다."
            if msg not in plan.unsupported:
                plan.unsupported.append(msg)
        _fill_metrics(node, values)

        while stack and stack[-1].depth >= node.depth:
            stack.pop()
        if stack:
            node.parent_id = stack[-1].id
            stack[-1].children.append(node.id)
        stack.append(node)
        plan.nodes.append(node)

    # 3) 하단 섹션들
    section: Optional[str] = None
    last_pred: Optional[tuple[PlanNode, Predicate]] = None
    note_buf: list[str] = []
    i = table_end
    while i < n:
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < n else ""
        sec = _section_of(ln, nxt)
        if sec:
            section = sec
            last_pred = None
            if sec.startswith("unknown:"):
                plan.unsupported.append(f"미지원 섹션 '{sec[8:]}' 은(는) 건너뜁니다.")
            i += 2
            continue
        if _DASH_RE.match(ln) or not ln.strip():
            i += 1
            continue

        if section == "predicates":
            m = _PRED_RE.match(ln)
            mc = _PRED_CONT_RE.match(ln) if not m else None
            if m:
                node = _find(plan, int(m.group(1)))
                pred = Predicate(kind=m.group(2).lower(), text=m.group(3).strip())
                if node is not None:
                    node.predicates.append(pred)
                    last_pred = (node, pred)
                else:
                    last_pred = None
            elif mc and last_pred is not None:
                pred = Predicate(kind=mc.group(1).lower(), text=mc.group(2).strip())
                last_pred[0].predicates.append(pred)
                last_pred = (last_pred[0], pred)
            elif last_pred is not None and ln[:1].isspace():
                last_pred[1].text += " " + ln.strip()
            else:
                last_pred = None
        elif section == "binds":
            m = _BIND_RE.match(ln)
            if m:
                plan.peeked_binds.append(PeekedBind(int(m.group(1)), m.group(2), m.group(3).strip(), m.group(4).strip()))
        elif section == "note":
            s = ln.strip()
            if s.startswith("- "):
                note_buf.append(s[2:].strip())
            elif note_buf:
                note_buf[-1] += " " + s
            else:
                note_buf.append(s)
        i += 1

    plan.notes = note_buf
    for node in plan.nodes:
        for p in node.predicates:
            p.text = _strip_outer_paren(p.text)
    return plan


def _find(plan: Plan, node_id: int) -> Optional[PlanNode]:
    for nd in plan.nodes:
        if nd.id == node_id:
            return nd
    return None


def _fill_metrics(node: PlanNode, values: dict[str, str]) -> None:
    g = values.get
    node.e_rows = parse_count(g("e_rows", ""))
    node.e_bytes = parse_bytes(g("e_bytes", ""))
    node.cost, node.cpu_pct = parse_cost(g("cost", ""))
    node.e_time = parse_duration(g("e_time", ""))
    node.starts = parse_count(g("starts", ""))
    node.a_rows = parse_count(g("a_rows", ""))
    node.a_time = parse_duration(g("a_time", ""))
    node.buffers = parse_count(g("buffers", ""))
    node.reads = parse_count(g("reads", ""))
    node.writes = parse_count(g("writes", ""))
    node.omem = parse_bytes(g("omem", ""))
    node.one_mem = parse_bytes(g("one_mem", ""))
    node.used_mem, node.used_mem_passes = parse_mem(g("used_mem", ""))
    node.used_tmp = parse_bytes(g("used_tmp", ""))
    node.pstart = (g("pstart") or "").strip()
    node.pstop = (g("pstop") or "").strip()
