"""파싱 결과를 담는 데이터 모델."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Predicate:
    kind: str  # "access" | "filter"
    text: str


@dataclass
class PlanNode:
    id: int
    depth: int
    operation: str
    name: str = ""
    has_predicate: bool = False  # Id 앞의 '*'
    inactive: bool = False  # 12c adaptive plan 의 '-' (11g 미지원 항목)
    parent_id: Optional[int] = None
    children: list[int] = field(default_factory=list)

    # 원본 컬럼 값 (정규화 키 -> 원문 문자열)
    raw: dict[str, str] = field(default_factory=dict)

    # 숫자 지표
    e_rows: Optional[float] = None  # Rows 또는 E-Rows (11g: Starts 1회당 추정치)
    e_bytes: Optional[float] = None
    cost: Optional[float] = None
    cpu_pct: Optional[float] = None
    e_time: Optional[float] = None  # 초
    starts: Optional[float] = None
    a_rows: Optional[float] = None
    a_time: Optional[float] = None  # 초, 하위 단계 포함 누적
    buffers: Optional[float] = None  # 누적
    reads: Optional[float] = None
    writes: Optional[float] = None
    omem: Optional[float] = None  # bytes
    one_mem: Optional[float] = None
    used_mem: Optional[float] = None
    used_mem_passes: Optional[int] = None  # Used-Mem 괄호 안: 0=optimal, 1=one-pass, 2+=multi-pass
    used_tmp: Optional[float] = None
    pstart: str = ""
    pstop: str = ""

    predicates: list[Predicate] = field(default_factory=list)

    # 파생 지표 (analyzer 에서 채움)
    est_total_rows: Optional[float] = None  # E-Rows x Starts
    misestimate_ratio: Optional[float] = None  # >=1, 실제/추정 괴리 배수
    underestimated: Optional[bool] = None
    self_time: Optional[float] = None
    self_buffers: Optional[float] = None

    @property
    def label(self) -> str:
        return f"{self.operation} {self.name}".strip()


@dataclass
class PeekedBind:
    position: int
    name: str
    datatype: str
    value: str


@dataclass
class Plan:
    sql_id: str = ""
    child_number: Optional[int] = None
    sql_text: str = ""
    plan_hash_value: str = ""
    columns: list[str] = field(default_factory=list)  # 원본 헤더 순서
    nodes: list[PlanNode] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    peeked_binds: list[PeekedBind] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # 해석하지 못한 항목 (에러 대신 기록)

    @property
    def has_runtime_stats(self) -> bool:
        return any(n.a_rows is not None for n in self.nodes)

    @property
    def root(self) -> Optional[PlanNode]:
        return self.nodes[0] if self.nodes else None

    def node(self, node_id: int) -> PlanNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_runtime_stats"] = self.has_runtime_stats
        return d
