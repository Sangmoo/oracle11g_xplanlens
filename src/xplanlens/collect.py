"""SQL_ID 기반 수집: 사용자가 자기 DB 에서 실행할 조회 스크립트를 만들고, 그 출력을 해석한다.

XPlanLens(Hugging Face)는 사용자 DB 에 직접 접속하지 않는다. 대신
1) SQL_ID 를 받아 SELECT 한 문장짜리 수집 스크립트를 만들어 주고,
2) 사용자가 SQL*Plus / SQL Developer / Toad 등에서 실행한 출력(텍스트 또는 그리드 복사)을 받아
3) 실행계획 + 커서 통계 + 테이블/인덱스/컬럼 통계 + 바인드 캡처를 함께 분석한다.

출력 각 행은 '@@태그|필드|필드…' 형식이라 도구별 머리글·여백·줄번호가 섞여도 행 단위로 골라낼 수 있다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from .rules import SEVERITY_ORDER, Analysis, Finding, _fmt, run_rules

SCRIPT_VERSION = "xplanlens-collect-1"
SQL_ID_RE = re.compile(r"^[0-9a-z]{13}$")
_DATE_FMT = "YYYY-MM-DD HH24:MI:SS"
_SQL_CHUNK = 500  # SQL 원문을 나눠 담는 글자 수 (멀티바이트·줄바꿈 치환 후에도 4000 bytes 미만)
_NL = "{{NL}}"

LARGE_TABLE_ROWS = 100_000
OLD_STATS_DAYS = 180


# ---------------------------------------------------------------------------
# 수집 스크립트 생성
# ---------------------------------------------------------------------------

def normalize_sql_id(sql_id: str) -> str:
    s = (sql_id or "").strip().lower()
    if not SQL_ID_RE.match(s):
        raise ValueError("SQL_ID 는 영문 소문자와 숫자로 된 13자리입니다. (예: 8fk2zq1m3x7cd)")
    return s


def build_collect_script(sql_id: str, child_number: Optional[int] = None, include_binds: bool = True) -> str:
    """SQL_ID 하나에 대한 수집용 SELECT 문 (Oracle 11gR2, 읽기 전용)."""
    sid = normalize_sql_id(sql_id)
    child = "NULL" if child_number is None else str(int(child_number))
    q = f"'{sid}'"
    d = f"'{_DATE_FMT}'"

    def clean(expr: str, n: int = 200) -> str:
        return f"REPLACE(REPLACE(REPLACE(SUBSTR({expr}, 1, {n}), CHR(10), ' '), CHR(13), ' '), '|', '/')"

    branches = [
        # 0. 메타
        f"SELECT 0 sec, 0 seq, '@@META|' || {q} || '|' || TO_CHAR(SYSDATE, {d}) || '|' ||\n"
        f"         (SELECT MAX(version) FROM product_component_version WHERE product LIKE 'Oracle%') || '|{child}|{SCRIPT_VERSION}' xplanlens\n"
        f"    FROM dual",
        # 1. 실행계획
        f"SELECT 1, ROWNUM, '@@PLAN|' || plan_table_output\n"
        f"    FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR({q}, {child}, 'ALLSTATS LAST +PEEKED_BINDS'))",
        # 2. 자식 커서별 누적 통계
        "SELECT 2, child_number, '@@CUR|' || child_number || '|' || plan_hash_value || '|' || executions || '|' ||\n"
        "         elapsed_time || '|' || cpu_time || '|' || buffer_gets || '|' || disk_reads || '|' || rows_processed || '|' ||\n"
        "         fetches || '|' || parse_calls || '|' || loads || '|' || invalidations || '|' || user_io_wait_time || '|' ||\n"
        f"         TO_CHAR(last_active_time, {d}) || '|' || first_load_time || '|' || is_bind_sensitive || '|' ||\n"
        "         is_bind_aware || '|' || is_shareable || '|' || optimizer_mode || '|' || optimizer_cost || '|' ||\n"
        f"         sql_profile || '|' || sql_plan_baseline || '|' || {clean('module', 64)}\n"
        f"    FROM v$sql WHERE sql_id = {q}",
        # 3. SQL 원문 (CLOB 을 조각내어 담는다)
        f"SELECT 3, LEVEL, '@@SQL|' || REPLACE(REPLACE(REPLACE(DBMS_LOB.SUBSTR(t, {_SQL_CHUNK}, (LEVEL - 1) * {_SQL_CHUNK} + 1),\n"
        f"         CHR(13), ''), CHR(10), '{_NL}'), CHR(9), ' ')\n"
        f"    FROM (SELECT sql_fulltext t FROM v$sql WHERE sql_id = {q} AND ROWNUM = 1)\n"
        f"    CONNECT BY LEVEL <= LEAST(CEIL(DBMS_LOB.GETLENGTH(t) / {_SQL_CHUNK}), 100)",
        # 4. 테이블 통계
        "SELECT 4, ROWNUM, '@@TAB|' || t.owner || '|' || t.table_name || '|' || t.num_rows || '|' || t.blocks || '|' ||\n"
        f"         t.avg_row_len || '|' || TO_CHAR(t.last_analyzed, {d}) || '|' || s.stale_stats || '|' ||\n"
        "         s.stattype_locked || '|' || t.partitioned || '|' || TRIM(t.degree) || '|' || t.temporary\n"
        "    FROM all_tables t JOIN tabs x ON x.owner = t.owner AND x.table_name = t.table_name\n"
        "    LEFT JOIN all_tab_statistics s\n"
        "      ON s.owner = t.owner AND s.table_name = t.table_name AND s.object_type = 'TABLE'",
        # 5. 해당 테이블의 모든 인덱스
        "SELECT 5, ROWNUM, '@@IDX|' || i.owner || '|' || i.index_name || '|' || i.table_owner || '|' || i.table_name || '|' ||\n"
        "         i.uniqueness || '|' || i.index_type || '|' || i.status || '|' || i.visibility || '|' || i.blevel || '|' ||\n"
        "         i.leaf_blocks || '|' || i.distinct_keys || '|' || i.clustering_factor || '|' || i.num_rows || '|' ||\n"
        f"         TO_CHAR(i.last_analyzed, {d}) || '|' || i.partitioned || '|' || TRIM(i.degree) || '|' ||\n"
        "         (SELECT LISTAGG(c.column_name, ',') WITHIN GROUP (ORDER BY c.column_position)\n"
        "            FROM all_ind_columns c WHERE c.index_owner = i.owner AND c.index_name = i.index_name)\n"
        "    FROM all_indexes i JOIN tabs x ON x.owner = i.table_owner AND x.table_name = i.table_name",
        # 6. 조건절에 쓰인 컬럼 통계
        "SELECT 6, ROWNUM, '@@COL|' || c.owner || '|' || c.table_name || '|' || c.column_name || '|' || c.data_type || '|' ||\n"
        "         c.nullable || '|' || c.num_distinct || '|' || c.num_nulls || '|' || c.density || '|' || c.histogram || '|' ||\n"
        f"         c.num_buckets || '|' || TO_CHAR(c.last_analyzed, {d})\n"
        "    FROM all_tab_columns c JOIN tabs x ON x.owner = c.owner AND x.table_name = c.table_name\n"
        "   WHERE EXISTS (SELECT 1 FROM v$sql_plan p\n"
        f"                  WHERE p.sql_id = {q}\n"
        "                    AND (INSTR(p.access_predicates, '\"' || c.column_name || '\"') > 0\n"
        "                      OR INSTR(p.filter_predicates, '\"' || c.column_name || '\"') > 0))",
    ]
    if include_binds:
        branches.append(
            # 7. 바인드 캡처 (15분 간격 샘플링 값)
            "SELECT 7, child_number * 1000 + position, '@@BIND|' || child_number || '|' || position || '|' || name || '|' ||\n"
            f"         datatype_string || '|' || TO_CHAR(last_captured, {d}) || '|' || {clean('value_string')}\n"
            f"    FROM v$sql_bind_capture WHERE sql_id = {q}"
        )

    union = "\n  UNION ALL\n  ".join(branches)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""-- XPlanLens SQL_ID 수집 스크립트 (Oracle 11gR2) · SQL_ID {sid} · 생성 {generated}
-- * 읽기 전용 SELECT 한 문장입니다. 데이터를 변경하지 않습니다.
-- * 필요 권한: V$SQL, V$SQL_PLAN, V$SQL_PLAN_STATISTICS_ALL, V$SQL_BIND_CAPTURE 조회
--   (보통 SELECT_CATALOG_ROLE 또는 SELECT ANY DICTIONARY) + 대상 테이블의 ALL_* 딕셔너리 조회
-- * SQL*Plus / SQL Developer(F5 스크립트 실행) / Toad 에서 실행한 뒤 출력 전체를 복사해 붙여넣으세요.
--   그리드로 실행했다면 결과 그리드에서 Ctrl+A → Ctrl+C 로 전체 행을 복사하면 됩니다.
-- * 아래 SET 줄은 SQL*Plus 용입니다. 다른 도구에서 오류가 나면 SET 줄은 지우고 SELECT 만 실행하세요.
SET PAGESIZE 0 LINESIZE 4000 LONG 100000 TRIMSPOOL ON TRIMOUT ON HEADING OFF FEEDBACK OFF VERIFY OFF TAB OFF
WITH objs AS (
  SELECT DISTINCT object_owner owner, object_name name, object_type
    FROM v$sql_plan WHERE sql_id = {q} AND object_owner IS NOT NULL
), tabs AS (
  SELECT owner, name table_name FROM objs WHERE object_type LIKE 'TABLE%'
  UNION
  SELECT i.table_owner, i.table_name
    FROM objs o JOIN all_indexes i ON i.owner = o.owner AND i.index_name = o.name
   WHERE o.object_type LIKE 'INDEX%'
)
SELECT xplanlens FROM (
  {union}
) ORDER BY sec, seq;
"""


# ---------------------------------------------------------------------------
# 출력 해석
# ---------------------------------------------------------------------------

def _num(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(s: str) -> Optional[int]:
    v = _num(s)
    return int(v) if v is not None else None


def _date(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d/%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class CursorStat:
    child: int
    plan_hash: str
    executions: Optional[float] = None
    elapsed_s: Optional[float] = None  # 누적, 초
    cpu_s: Optional[float] = None
    buffer_gets: Optional[float] = None
    disk_reads: Optional[float] = None
    rows_processed: Optional[float] = None
    fetches: Optional[float] = None
    parse_calls: Optional[float] = None
    loads: Optional[float] = None
    invalidations: Optional[float] = None
    user_io_wait_s: Optional[float] = None
    last_active: str = ""
    first_load: str = ""
    bind_sensitive: str = ""
    bind_aware: str = ""
    shareable: str = ""
    optimizer_mode: str = ""
    optimizer_cost: Optional[float] = None
    sql_profile: str = ""
    sql_plan_baseline: str = ""
    module: str = ""

    def per_exec(self, attr: str) -> Optional[float]:
        v = getattr(self, attr)
        if v is None or not self.executions:
            return None
        return v / self.executions


@dataclass
class TableStat:
    owner: str
    name: str
    num_rows: Optional[float] = None
    blocks: Optional[float] = None
    avg_row_len: Optional[float] = None
    last_analyzed: str = ""
    stale_stats: str = ""
    locked: str = ""
    partitioned: str = ""
    degree: str = ""
    temporary: str = ""


@dataclass
class IndexStat:
    owner: str
    name: str
    table_owner: str
    table_name: str
    uniqueness: str = ""
    index_type: str = ""
    status: str = ""
    visibility: str = ""
    blevel: Optional[float] = None
    leaf_blocks: Optional[float] = None
    distinct_keys: Optional[float] = None
    clustering_factor: Optional[float] = None
    num_rows: Optional[float] = None
    last_analyzed: str = ""
    partitioned: str = ""
    degree: str = ""
    columns: list[str] = field(default_factory=list)


@dataclass
class ColumnStat:
    owner: str
    table_name: str
    column_name: str
    data_type: str = ""
    nullable: str = ""
    num_distinct: Optional[float] = None
    num_nulls: Optional[float] = None
    density: Optional[float] = None
    histogram: str = ""
    num_buckets: Optional[float] = None
    last_analyzed: str = ""


@dataclass
class BindCapture:
    child: int
    position: int
    name: str
    datatype: str = ""
    last_captured: str = ""
    value: str = ""


@dataclass
class Collected:
    sql_id: str = ""
    collected_at: str = ""
    db_version: str = ""
    requested_child: str = ""
    sql_text: str = ""
    cursors: list[CursorStat] = field(default_factory=list)
    tables: list[TableStat] = field(default_factory=list)
    indexes: list[IndexStat] = field(default_factory=list)
    columns: list[ColumnStat] = field(default_factory=list)
    binds: list[BindCapture] = field(default_factory=list)
    plans: dict[int, str] = field(default_factory=dict)  # child_number -> DBMS_XPLAN 텍스트
    plan_messages: list[str] = field(default_factory=list)  # 실행계획 대신 나온 안내/오류 문구
    selected_child: Optional[int] = None

    @property
    def plan_text(self) -> str:
        if self.selected_child is not None and self.selected_child in self.plans:
            return self.plans[self.selected_child]
        return next(iter(self.plans.values()), "")

    def table(self, name: str) -> Optional[TableStat]:
        name = name.upper()
        return next((t for t in self.tables if t.name.upper() == name), None)

    def index(self, name: str) -> Optional[IndexStat]:
        name = name.upper()
        return next((i for i in self.indexes if i.name.upper() == name), None)

    def table_of(self, object_name: str) -> Optional[TableStat]:
        """실행계획 Name(테이블 또는 인덱스) -> 테이블 통계."""
        if not object_name:
            return None
        t = self.table(object_name)
        if t:
            return t
        i = self.index(object_name)
        return self.table(i.table_name) if i else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["plans"] = {str(k): v for k, v in self.plans.items()}
        return d


_TAG_RE = re.compile(r"@@(META|PLAN|CUR|SQL|TAB|IDX|COL|BIND)\|")
_CHILD_HEADER_RE = re.compile(r"^\s*SQL_ID\s*:?\s*[0-9a-z]{13}\s*,\s*child number\s*:?\s*(\d+)", re.I)


def is_collected(text: str) -> bool:
    return bool(text) and "@@PLAN|" in text and ("@@META|" in text or "@@CUR|" in text)


def _tagged_lines(text: str):
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _TAG_RE.search(raw)
        if not m:
            continue
        body = raw[m.end():]
        # 그리드 복사 시 값 전체가 따옴표로 감싸지는 경우
        if raw[: m.start()].rstrip().endswith('"') and body.rstrip().endswith('"'):
            body = body.rstrip()[:-1]
        yield m.group(1), body


def parse_collected(text: str) -> Collected:
    c = Collected()
    plan_lines: list[str] = []
    sql_parts: list[str] = []
    for tag, body in _tagged_lines(text):
        f = body.split("|")
        f += [""] * 30  # 비어 있는 꼬리 필드 대비
        if tag == "PLAN":
            plan_lines.append(body.rstrip())
        elif tag == "SQL":
            sql_parts.append(body.rstrip("\n"))
        elif tag == "META":
            c.sql_id, c.collected_at, c.db_version, c.requested_child = f[0], f[1], f[2], f[3]
        elif tag == "CUR":
            module = "|".join(body.split("|")[22:])
            c.cursors.append(CursorStat(
                child=_int(f[0]) or 0, plan_hash=f[1].strip(), executions=_num(f[2]),
                elapsed_s=(_num(f[3]) or 0) / 1e6 if f[3].strip() else None,
                cpu_s=(_num(f[4]) or 0) / 1e6 if f[4].strip() else None,
                buffer_gets=_num(f[5]), disk_reads=_num(f[6]), rows_processed=_num(f[7]), fetches=_num(f[8]),
                parse_calls=_num(f[9]), loads=_num(f[10]), invalidations=_num(f[11]),
                user_io_wait_s=(_num(f[12]) or 0) / 1e6 if f[12].strip() else None,
                last_active=f[13].strip(), first_load=f[14].strip(), bind_sensitive=f[15].strip(),
                bind_aware=f[16].strip(), shareable=f[17].strip(), optimizer_mode=f[18].strip(),
                optimizer_cost=_num(f[19]), sql_profile=f[20].strip(), sql_plan_baseline=f[21].strip(),
                module=module.strip(),
            ))
        elif tag == "TAB":
            c.tables.append(TableStat(
                owner=f[0], name=f[1], num_rows=_num(f[2]), blocks=_num(f[3]), avg_row_len=_num(f[4]),
                last_analyzed=f[5].strip(), stale_stats=f[6].strip(), locked=f[7].strip(),
                partitioned=f[8].strip(), degree=f[9].strip(), temporary=f[10].strip(),
            ))
        elif tag == "IDX":
            c.indexes.append(IndexStat(
                owner=f[0], name=f[1], table_owner=f[2], table_name=f[3], uniqueness=f[4].strip(),
                index_type=f[5].strip(), status=f[6].strip(), visibility=f[7].strip(), blevel=_num(f[8]),
                leaf_blocks=_num(f[9]), distinct_keys=_num(f[10]), clustering_factor=_num(f[11]),
                num_rows=_num(f[12]), last_analyzed=f[13].strip(), partitioned=f[14].strip(), degree=f[15].strip(),
                columns=[x.strip() for x in f[16].split(",") if x.strip()],
            ))
        elif tag == "COL":
            c.columns.append(ColumnStat(
                owner=f[0], table_name=f[1], column_name=f[2], data_type=f[3].strip(), nullable=f[4].strip(),
                num_distinct=_num(f[5]), num_nulls=_num(f[6]), density=_num(f[7]), histogram=f[8].strip(),
                num_buckets=_num(f[9]), last_analyzed=f[10].strip(),
            ))
        elif tag == "BIND":
            c.binds.append(BindCapture(
                child=_int(f[0]) or 0, position=_int(f[1]) or 0, name=f[2].strip(), datatype=f[3].strip(),
                last_captured=f[4].strip(), value="|".join(body.split("|")[5:]).strip(),
            ))

    c.sql_text = "".join(sql_parts).replace(_NL, "\n").strip()
    _split_plans(c, plan_lines)
    _choose_child(c)
    return c


def _split_plans(c: Collected, lines: list[str]) -> None:
    """DISPLAY_CURSOR(child=NULL) 출력은 자식 커서별 실행계획이 이어져 나온다."""
    current: Optional[int] = None
    buf: list[str] = []
    for ln in lines:
        m = _CHILD_HEADER_RE.match(ln)
        if m:
            if current is not None:
                c.plans[current] = "\n".join(buf).strip("\n")
            current, buf = int(m.group(1)), [ln]
            continue
        if current is None:
            if ln.strip():
                c.plan_messages.append(ln.strip())
            continue
        buf.append(ln)
    if current is not None:
        c.plans[current] = "\n".join(buf).strip("\n")
    # 커서가 공유 풀에서 밀려난 경우 등: 실행계획 표 없이 안내 문구만 있다
    for child, text in list(c.plans.items()):
        if "Plan hash value" not in text:
            c.plan_messages.extend(x.strip() for x in text.splitlines() if x.strip() and not set(x.strip()) <= {"-"})
            del c.plans[child]


def _choose_child(c: Collected) -> None:
    """가장 최근에 실행된 자식 커서의 실행계획을 분석 대상으로 고른다."""
    if not c.plans:
        return
    active = sorted(
        (cur for cur in c.cursors if cur.child in c.plans),
        key=lambda cur: (_date(cur.last_active) or datetime.min, cur.executions or 0),
        reverse=True,
    )
    c.selected_child = active[0].child if active else next(iter(c.plans))


# ---------------------------------------------------------------------------
# 통계 기반 규칙
# ---------------------------------------------------------------------------

_QUOTED_RE = re.compile(r'"([A-Za-z0-9_$#]+)"')


def _nodes_on_table(a: Analysis, c: Collected, table: TableStat) -> list[int]:
    out = []
    for n in a.plan.nodes:
        t = c.table_of(n.name)
        if t is not None and t.name == table.name and t.owner == table.owner:
            out.append(n.id)
    return out


def _pred_columns(c: Collected, node, table: TableStat, kind: Optional[str] = None) -> list[ColumnStat]:
    text = " ".join(p.text for p in node.predicates if kind is None or p.kind == kind)
    names = {m.group(1).upper() for m in _QUOTED_RE.finditer(text)}
    return [col for col in c.columns
            if col.table_name == table.name and col.owner == table.owner and col.column_name.upper() in names]


def stats_findings(a: Analysis, c: Collected) -> list[Finding]:
    out: list[Finding] = []
    now = _date(c.collected_at) or datetime.now()

    if c.db_version and not c.db_version.startswith("11."):
        out.append(Finding(
            "DB_VERSION", "info", f"Oracle {c.db_version} 에서 수집된 결과입니다",
            "XPlanLens 의 규칙과 AI 프롬프트는 11gR2 기준입니다. 12c 이상 기능(Adaptive Plan, SQL Plan Directive 등)은 해석하지 않습니다.",
        ))

    if not c.plans:
        msg = " / ".join(c.plan_messages[:3]) or "DBMS_XPLAN 출력이 비어 있습니다."
        out.append(Finding(
            "CURSOR_NOT_FOUND", "critical", "공유 풀에서 실행계획을 찾지 못했습니다",
            f"원문: {msg}",
            "커서가 공유 풀에서 밀려났거나 SQL_ID/child 번호가 다릅니다. SQL 을 다시 실행한 직후 스크립트를 실행하세요. "
            "RAC 라면 SQL 이 실행된 인스턴스에 접속해야 합니다.",
        ))

    # 자식 커서 / 실행계획 다중화
    hashes: dict[str, list[CursorStat]] = {}
    for cur in c.cursors:
        hashes.setdefault(cur.plan_hash, []).append(cur)
    if len(hashes) > 1:
        lines = []
        for ph, curs in hashes.items():
            execs = sum(x.executions or 0 for x in curs)
            ela = sum(x.elapsed_s or 0 for x in curs)
            bg = sum(x.buffer_gets or 0 for x in curs)
            per = f"{ela / execs:.3f}초, Buffers {_fmt(bg / execs)}" if execs else "실행 0회"
            lines.append(f"plan hash {ph} (child {', '.join(str(x.child) for x in curs)}): 실행 {_fmt(execs)}회, 1회 평균 {per}")
        out.append(Finding(
            "MULTIPLE_PLANS", "warning", f"같은 SQL 에 실행계획이 {len(hashes)}개 존재합니다",
            "바인드 피킹, 적응형 커서 공유(ACS), 통계 변경, 옵티마이저 환경 차이로 실행계획이 갈라졌습니다. " + " / ".join(lines),
            "1회 평균 수행시간이 가장 좋은 plan hash 를 확인하고, 11g SQL Plan Management 로 고정하세요: "
            "DBMS_SPM.LOAD_PLANS_FROM_CURSOR_CACHE(sql_id => '…', plan_hash_value => …). "
            "값 분포가 치우친 컬럼이라면 히스토그램과 바인드 값별 실행계획을 함께 검토하세요.",
        ))
    if any(cur.bind_aware == "Y" for cur in c.cursors):
        out.append(Finding(
            "BIND_AWARE", "info", "적응형 커서 공유(ACS)가 동작 중입니다",
            "IS_BIND_AWARE=Y 인 자식 커서가 있어 바인드 값 범위에 따라 다른 실행계획을 쓸 수 있습니다.",
        ))
    if any(cur.invalidations and cur.invalidations >= 10 for cur in c.cursors):
        out.append(Finding(
            "CURSOR_INVALIDATIONS", "info", "커서 무효화가 잦습니다",
            "INVALIDATIONS 가 10회 이상입니다. 통계 재수집·DDL 이 실행계획 변동 원인일 수 있습니다.",
        ))

    # 테이블 통계
    for t in c.tables:
        nodes = _nodes_on_table(a, c, t)
        label = f"{t.owner}.{t.name}"
        if not t.last_analyzed and t.temporary != "Y":
            out.append(Finding(
                "NO_TABLE_STATS", "critical", f"{label} 에 옵티마이저 통계가 없습니다",
                "통계가 없으면 동적 샘플링이나 기본값으로 추정해 카디널리티가 크게 틀어질 수 있습니다.",
                f"EXEC DBMS_STATS.GATHER_TABLE_STATS('{t.owner}', '{t.name}', cascade => TRUE);",
                nodes,
            ))
            continue
        if t.stale_stats == "YES":
            out.append(Finding(
                "STALE_STATS", "warning", f"{label} 통계가 오래되었습니다 (STALE_STATS=YES)",
                f"마지막 수집 {t.last_analyzed}, NUM_ROWS {_fmt(t.num_rows)}. 이후 10% 이상 변경되어 통계가 실제와 다를 수 있습니다.",
                f"EXEC DBMS_STATS.GATHER_TABLE_STATS('{t.owner}', '{t.name}', cascade => TRUE);"
                + (" 통계가 잠겨 있으므로(STATTYPE_LOCKED) 잠금 의도를 먼저 확인하세요." if t.locked else ""),
                nodes,
            ))
        else:
            analyzed = _date(t.last_analyzed)
            if analyzed and (now - analyzed).days >= OLD_STATS_DAYS:
                out.append(Finding(
                    "OLD_STATS", "info", f"{label} 통계가 {(now - analyzed).days}일 전에 수집되었습니다",
                    "변경량이 적어 STALE 로 표시되지 않았더라도, 날짜·순번 컬럼의 최대값이 통계보다 커지면 범위 조건 추정이 틀어집니다.",
                    "최근 값 범위를 조회하는 SQL 이라면 통계를 다시 수집하세요.", nodes,
                ))
        if t.degree and t.degree not in ("1", "0"):
            out.append(Finding(
                "TABLE_DEGREE", "warning", f"{label} 에 병렬도(DEGREE={t.degree})가 설정되어 있습니다",
                "테이블 속성의 병렬도 때문에 의도하지 않은 병렬 실행이나 FULL SCAN 선호가 생길 수 있습니다.",
                f"의도한 설정이 아니면 ALTER TABLE {t.owner}.{t.name} NOPARALLEL; 로 되돌리고 필요한 SQL 에만 PARALLEL 힌트를 쓰세요.",
                nodes,
            ))

    # 실제 건수와 NUM_ROWS 비교 (조건 없는 FULL SCAN)
    for n in a.plan.nodes:
        if n.operation != "TABLE ACCESS FULL" or n.predicates or n.a_rows is None:
            continue
        t = c.table(n.name)
        if not t or not t.num_rows or t.partitioned == "YES":
            continue
        actual = n.a_rows / n.starts if n.starts else n.a_rows
        ratio = max(actual, 1) / max(t.num_rows, 1)
        if max(actual, t.num_rows) >= 1000 and (ratio >= 10 or ratio <= 0.1):
            out.append(Finding(
                "NUM_ROWS_MISMATCH", "warning", f"{t.name} 실제 건수와 통계 NUM_ROWS 가 크게 다릅니다",
                f"조건 없는 FULL SCAN 결과 {_fmt(actual)}건, 통계 NUM_ROWS {_fmt(t.num_rows)}건 (마지막 수집 {t.last_analyzed}).",
                f"EXEC DBMS_STATS.GATHER_TABLE_STATS('{t.owner}', '{t.name}', cascade => TRUE);", [n.id],
            ))

    # 인덱스 상태
    for i in c.indexes:
        label = f"{i.owner}.{i.name}"
        if i.status == "UNUSABLE":
            out.append(Finding(
                "UNUSABLE_INDEX", "critical", f"{label} 인덱스가 UNUSABLE 상태입니다",
                f"{i.table_name}({', '.join(i.columns)}) 인덱스를 옵티마이저가 사용할 수 없습니다.",
                f"ALTER INDEX {label} REBUILD ONLINE;",
            ))
        elif i.visibility == "INVISIBLE":
            out.append(Finding(
                "INVISIBLE_INDEX", "info", f"{label} 인덱스가 INVISIBLE 입니다",
                f"{i.table_name}({', '.join(i.columns)}) 인덱스는 OPTIMIZER_USE_INVISIBLE_INDEXES=TRUE 가 아니면 사용되지 않습니다.",
            ))

    # 노드별 규칙: 히스토그램 후보, 인덱스 후보, 클러스터링 팩터
    by_id = {n.id: n for n in a.plan.nodes}
    root_causes = {f.node_ids[0] for f in a.findings if f.rule == "CARDINALITY_MISESTIMATE" and f.node_ids}
    for nid in sorted(root_causes):
        n = by_id.get(nid)
        t = c.table_of(n.name) if n else None
        if not t:
            continue
        cols = [col for col in _pred_columns(c, n, t) if col.histogram in ("", "NONE") and (col.num_distinct or 0) > 1]
        if cols:
            names = ", ".join(col.column_name for col in cols)
            out.append(Finding(
                "HISTOGRAM_CANDIDATE", "warning", f"Id {nid}: 조건 컬럼에 히스토그램이 없습니다 ({t.name}.{names})",
                "추정/실제 건수 괴리가 시작된 단계의 조건 컬럼에 히스토그램이 없어 값 분포를 균등하다고 가정합니다. "
                + " / ".join(f"{col.column_name}: NDV {_fmt(col.num_distinct)}, NULL {_fmt(col.num_nulls)}" for col in cols),
                f"값 분포가 치우쳐 있다면 EXEC DBMS_STATS.GATHER_TABLE_STATS('{t.owner}', '{t.name}', "
                f"method_opt => 'FOR COLUMNS SIZE 254 {' '.join(col.column_name for col in cols)}'); "
                "단, 바인드 변수 SQL 은 히스토그램과 바인드 피킹이 겹쳐 실행계획이 흔들릴 수 있으니 함께 검토하세요.",
                [nid],
            ))

    for n in a.plan.nodes:
        if n.operation == "TABLE ACCESS FULL" and any(p.kind == "filter" for p in n.predicates):
            t = c.table(n.name)
            if not t or not t.num_rows or t.num_rows < LARGE_TABLE_ROWS:
                continue
            rows = (n.a_rows / n.starts if n.starts else n.a_rows) if n.a_rows is not None else n.e_rows
            if rows is None:
                continue
            sel = rows / t.num_rows
            if sel > 0.05:
                continue
            cols = _pred_columns(c, n, t, "filter")
            leading = {i.columns[0].upper() for i in c.indexes
                       if i.table_name == t.name and i.table_owner == t.owner and i.columns and i.status != "UNUSABLE"}
            missing = [col.column_name for col in cols if col.column_name.upper() not in leading]
            existing = [col.column_name for col in cols if col.column_name.upper() in leading]
            if missing:
                why = ("필터 컬럼으로 시작하는 인덱스가 없습니다." if not existing else
                       f"{', '.join(existing)} 로 시작하는 인덱스는 있지만 선택되지 않았고, {', '.join(missing)} 로 시작하는 인덱스는 없습니다.")
                out.append(Finding(
                    "INDEX_CANDIDATE", "warning" if sel <= 0.01 else "info",
                    f"Id {n.id}: {t.name} FULL SCAN 결과가 {sel:.2%} 뿐입니다 — 인덱스 후보 ({', '.join(missing + existing)})",
                    f"NUM_ROWS {_fmt(t.num_rows)}건 중 {_fmt(rows)}건만 남기는데, {why}",
                    f"선택도가 좋은 컬럼부터 CREATE INDEX … ON {t.owner}.{t.name}({', '.join(missing + existing)}) 를 검토하세요. "
                    "등치(=) 조건 컬럼을 앞에, 범위 조건 컬럼을 뒤에 두는 것이 일반적입니다. DML 부하와 기존 인덱스 중복도 함께 확인하세요.",
                    [n.id],
                ))

        if n.operation.startswith("INDEX RANGE SCAN") and n.name:
            i = c.index(n.name)
            t = c.table(i.table_name) if i else None
            parent = by_id.get(n.parent_id) if n.parent_id is not None else None
            if not (i and t and parent and parent.operation.startswith("TABLE ACCESS BY")):
                continue
            rows = n.a_rows if n.a_rows is not None else n.est_total_rows
            if (i.clustering_factor and t.num_rows and t.blocks and rows and rows >= 1000
                    and i.clustering_factor >= 0.5 * t.num_rows and i.clustering_factor > 5 * t.blocks):
                out.append(Finding(
                    "POOR_CLUSTERING", "info", f"Id {n.id}: {i.name} 의 클러스터링 팩터가 높습니다",
                    f"CLUSTERING_FACTOR {_fmt(i.clustering_factor)} (테이블 NUM_ROWS {_fmt(t.num_rows)}, BLOCKS {_fmt(t.blocks)}). "
                    f"인덱스로 {_fmt(rows)}건을 읽으면 테이블 블록을 거의 건마다 랜덤 I/O 합니다.",
                    "조회 컬럼까지 포함한 커버링 인덱스로 테이블 액세스를 없애거나, 건수가 많다면 FULL SCAN + 해시 조인이 나은지 비교하세요.",
                    [n.id, parent.id],
                ))
    return out


def analyze_collected(text: str) -> tuple[Analysis, Collected]:
    from .parser import parse_plan

    c = parse_collected(text)
    plan = parse_plan(c.plan_text) if c.plan_text else parse_plan("")
    if not plan.sql_text and c.sql_text:
        plan.sql_text = c.sql_text
    if not plan.sql_id:
        plan.sql_id = c.sql_id
    a = run_rules(plan)
    a.findings.extend(stats_findings(a, c))
    a.findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.node_ids[0] if f.node_ids else -1))
    return a, c


# ---------------------------------------------------------------------------
# DB 통계 Markdown
# ---------------------------------------------------------------------------

def _cell(v) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, float):
        return _fmt(v)
    return str(v).replace("|", "/")


def _md_table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
    return out


def render_collected(c: Collected, used_objects: Optional[set[str]] = None) -> str:
    used = {x.upper() for x in (used_objects or set())}
    out = ["### 🗄️ SQL_ID 수집 정보",
           f"**SQL_ID** `{c.sql_id or '-'}` · **수집 시각** {c.collected_at or '-'} · **DB 버전** {c.db_version or '-'}"
           + (f" · **분석한 child** {c.selected_child}" if c.selected_child is not None else "")]
    if len(c.plans) > 1:
        out.append(f"\n실행계획이 있는 자식 커서 {len(c.plans)}개 중 가장 최근에 실행된 child {c.selected_child} 를 분석했습니다. "
                   "다른 child 를 보려면 스크립트 생성 시 child 번호를 지정하세요.")
    if c.plan_messages:
        out.append("\n> " + " / ".join(c.plan_messages[:3]))

    if c.cursors:
        out.append("\n#### 자식 커서별 누적 통계 (V$SQL, 1회 평균)")
        rows = []
        for cur in sorted(c.cursors, key=lambda x: x.child):
            ela = cur.per_exec("elapsed_s")
            cpu = cur.per_exec("cpu_s")
            extra = " ".join(x for x in (
                "bind-aware" if cur.bind_aware == "Y" else "",
                f"Profile:{cur.sql_profile}" if cur.sql_profile else "",
                f"Baseline:{cur.sql_plan_baseline}" if cur.sql_plan_baseline else "",
            ) if x)
            rows.append([cur.child, cur.plan_hash, cur.executions,
                         f"{ela:.4f}" if ela is not None else None, f"{cpu:.4f}" if cpu is not None else None,
                         cur.per_exec("buffer_gets"), cur.per_exec("disk_reads"), cur.per_exec("rows_processed"),
                         cur.last_active, extra])
        out += _md_table(["child", "plan hash", "실행 수", "경과(초)", "CPU(초)", "Buffer Gets", "Disk Reads", "Rows",
                          "마지막 실행", "비고"], rows)

    if c.tables:
        out.append("\n#### 테이블 통계")
        out += _md_table(
            ["테이블", "NUM_ROWS", "BLOCKS", "AVG_ROW_LEN", "LAST_ANALYZED", "STALE", "파티션", "DEGREE"],
            [[f"{t.owner}.{t.name}", t.num_rows, t.blocks, t.avg_row_len, t.last_analyzed,
              t.stale_stats + (" 🔒" if t.locked else ""), t.partitioned, t.degree] for t in c.tables],
        )
    if c.indexes:
        out.append("\n#### 인덱스 (✔ = 이 실행계획에서 사용)")
        out += _md_table(
            ["", "인덱스", "테이블", "컬럼", "UNIQUE", "상태", "BLEVEL", "LEAF", "DISTINCT", "CLUSTERING", "LAST_ANALYZED"],
            [["✔" if i.name.upper() in used else "", i.name, i.table_name, ", ".join(i.columns),
              "Y" if i.uniqueness == "UNIQUE" else "", i.status + ("" if i.visibility in ("", "VISIBLE") else f" {i.visibility}"),
              i.blevel, i.leaf_blocks, i.distinct_keys, i.clustering_factor, i.last_analyzed] for i in c.indexes],
        )
    if c.columns:
        out.append("\n#### 조건절 컬럼 통계")
        out += _md_table(
            ["테이블.컬럼", "타입", "NULL 허용", "NDV", "NULL 수", "DENSITY", "히스토그램", "버킷"],
            [[f"{col.table_name}.{col.column_name}", col.data_type, col.nullable, col.num_distinct, col.num_nulls,
              f"{col.density:.3g}" if col.density is not None else None, col.histogram, col.num_buckets]
             for col in c.columns],
        )
    if c.binds:
        out.append("\n#### 바인드 캡처 (V$SQL_BIND_CAPTURE, 샘플링 값)")
        out += _md_table(["child", "위치", "이름", "타입", "값", "캡처 시각"],
                         [[b.child, b.position, b.name, b.datatype, b.value or "(미캡처)", b.last_captured] for b in c.binds])
    if not (c.cursors or c.tables or c.indexes):
        out.append("\n수집된 통계가 없습니다. 권한(V$SQL, ALL_TABLES 등)이 있는 계정으로 실행했는지 확인하세요.")
    return "\n".join(out)
