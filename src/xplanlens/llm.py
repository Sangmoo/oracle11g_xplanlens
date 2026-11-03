"""LLM 개선안 생성 (사용자가 직접 입력한 API Key 로만 호출).

- API Key 는 요청 1회에만 사용하고 저장·로깅하지 않는다.
- 규칙 엔진 결과(구조화된 지표 + 진단)를 함께 넘겨 LLM 이 숫자를 잘못 읽는 것을 줄인다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .rules import Analysis, _fmt

PROVIDERS = {
    "Anthropic (Claude)": {
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
        "default": "claude-opus-5-5",
    },
    "OpenAI": {
        "models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"],
        "default": "gpt-5-mini",
    },
    "OpenAI 호환 (Base URL 지정)": {
        "models": [],
        "default": "",
    },
}

SYSTEM_PROMPT = """당신은 Oracle Database 11g Release 2 SQL 튜닝 전문가입니다.
사용자가 DBMS_XPLAN 실행계획과 규칙 기반 사전 진단 결과를 제공합니다. 이를 바탕으로 실무에서 바로 적용할 수 있는 개선안을 제시하세요.

반드시 지킬 것
- Oracle 11gR2 에서 동작하는 문법·기능만 제안합니다. 12c 이상 기능(FETCH FIRST, Adaptive Plan, SQL Plan Directive, 인메모리,
  IDENTITY 컬럼, 12c 이상 전용 힌트 등)은 쓰지 마세요. 페이징은 ROWNUM 을 사용합니다.
- 11g 의 E-Rows 는 Starts 1회당 추정치이므로 A-Rows 와 비교할 때 E-Rows×Starts 를 기준으로 판단합니다.
- 제공된 숫자에 근거해 설명하고, 실행계획만으로 알 수 없는 정보(테이블 건수, 인덱스 구성, 데이터 분포)는 추측이라고 명시하고 확인할 SQL 을 제시합니다.
- 힌트를 제안할 때는 쿼리 블록/별칭을 포함한 정확한 형태로 쓰고, 힌트보다 근본 해결(통계, 인덱스, SQL 재작성)을 우선합니다.
- 운영 환경 변경(인덱스 생성, 통계 수집, 파라미터 변경)은 부작용과 검증 방법을 함께 적습니다.

출력 형식 (Markdown)
1. **핵심 진단** - 가장 큰 병목 1~3개와 근거 수치
2. **개선안 (우선순위 순)** - 각 항목마다: 무엇을 / 왜 / 예상 효과 / 적용 SQL 또는 DDL
3. **개선된 SQL 예시** - SQL 원문이 있을 때만, 11g 문법으로
4. **검증 방법** - 적용 후 확인할 DBMS_XPLAN 명령과 비교할 지표
5. **추가로 필요한 정보** - 더 정확한 분석을 위해 확인할 딕셔너리 조회 SQL
"""

COMPARE_SYSTEM_PROMPT = """당신은 Oracle Database 11g Release 2 SQL 튜닝 전문가입니다.
튜닝 전(Before)과 튜닝 후(After) 두 실행계획과 규칙 기반 비교 결과가 주어집니다.
- 무엇이 바뀌었는지(접근 경로, 조인 방식·순서, 추정치), 그 변화가 성능 지표에 어떤 영향을 주었는지 수치로 설명하세요.
- 개선이 데이터·바인드 값이 달라져도 유지될지 위험 요소를 평가하세요.
- 남은 병목과 추가 개선안을 11gR2 에서 가능한 방법으로만 제시하세요.
Markdown 으로 답하세요."""


@dataclass
class LLMRequest:
    provider: str
    api_key: str
    model: str
    base_url: str = ""
    language: str = "한국어"


class LLMError(Exception):
    pass


def build_context(a: Analysis, sql_text: str = "", extra: str = "", max_plan_chars: int = 60000) -> str:
    """LLM 에 넘길 사용자 메시지 본문."""
    p = a.plan
    rows = []
    for n in p.nodes:
        rows.append({
            "id": n.id,
            "parent": n.parent_id,
            "depth": n.depth,
            "operation": n.operation,
            "name": n.name,
            "starts": n.starts,
            "e_rows_per_start": n.e_rows,
            "e_rows_total": n.est_total_rows,
            "a_rows": n.a_rows,
            "misestimate_x": round(n.misestimate_ratio, 1) if n.misestimate_ratio else None,
            "self_time_s": round(n.self_time, 3) if n.self_time is not None else None,
            "self_buffers": n.self_buffers,
            "reads": n.reads,
            "cost": n.cost,
            "used_mem": n.raw.get("used_mem"),
            "used_tmp": n.raw.get("used_tmp"),
            "pstart": n.pstart or None,
            "pstop": n.pstop or None,
            "predicates": [f"{pr.kind}: {pr.text}" for pr in n.predicates],
        })
    rows = [{k: v for k, v in r.items() if v not in (None, [], "")} for r in rows]
    findings = [
        {"severity": f.severity, "rule": f.rule, "title": f.title, "detail": f.detail, "node_ids": f.node_ids}
        for f in a.findings
    ]
    parts = [
        "## 실행계획 메타정보",
        f"- SQL_ID: {p.sql_id or '-'} / Plan hash value: {p.plan_hash_value or '-'}",
        f"- 실행 통계(A-Rows) 유무: {'있음' if p.has_runtime_stats else '없음 (EXPLAIN PLAN 추정치)'}",
        f"- 총 수행시간: {a.total_time if a.total_time is not None else '-'}초, 총 Buffers: {_fmt(a.total_buffers)}, Cost: {_fmt(a.total_cost)}",
    ]
    if p.peeked_binds:
        parts.append("- Peeked Binds: " + ", ".join(f"{b.name}({b.datatype})={b.value}" for b in p.peeked_binds))
    if p.notes:
        parts.append("- Note: " + " / ".join(p.notes))
    sql = sql_text.strip() or p.sql_text
    if sql:
        parts += ["", "## SQL 원문", "```sql", sql[:20000], "```"]
    parts += [
        "", "## 단계별 지표 (JSON, 자체 시간/버퍼는 하위 단계 제외 값)",
        "```json", json.dumps(rows, ensure_ascii=False, indent=1), "```",
        "", "## 규칙 기반 사전 진단",
        "```json", json.dumps(findings, ensure_ascii=False, indent=1), "```",
    ]
    if extra.strip():
        parts += ["", "## 사용자가 제공한 추가 정보 (테이블 건수, 인덱스 구성 등)", extra.strip()[:20000]]
    return "\n".join(parts)


def build_messages(a: Analysis, raw_plan: str, sql_text: str = "", extra: str = "", language: str = "한국어") -> tuple[str, str]:
    body = build_context(a, sql_text, extra)
    user = (
        f"{body}\n\n## DBMS_XPLAN 원문\n```\n{raw_plan.strip()[:60000]}\n```\n\n"
        f"위 실행계획을 분석해 개선안을 {language}로 작성해 주세요."
    )
    return SYSTEM_PROMPT, user


def build_compare_messages(before: Analysis, after: Analysis, diff_md: str, raw_before: str, raw_after: str,
                           language: str = "한국어") -> tuple[str, str]:
    user = (
        f"## 규칙 기반 비교 결과\n{diff_md}\n\n"
        f"## Before 단계별 지표\n{build_context(before)}\n\n"
        f"## After 단계별 지표\n{build_context(after)}\n\n"
        f"## Before 원문\n```\n{raw_before.strip()[:30000]}\n```\n\n"
        f"## After 원문\n```\n{raw_after.strip()[:30000]}\n```\n\n"
        f"두 실행계획의 차이와 남은 개선점을 {language}로 설명해 주세요."
    )
    return COMPARE_SYSTEM_PROMPT, user


def _mask(text: str, secret: str) -> str:
    if secret and len(secret) >= 8:
        text = text.replace(secret, secret[:4] + "…(masked)")
    return text


def call_llm(req: LLMRequest, system: str, user: str, timeout: float = 300.0) -> str:
    """선택한 공급자로 호출하고 Markdown 텍스트를 반환한다. 실패 시 LLMError."""
    key = (req.api_key or "").strip()
    if not key:
        raise LLMError("API Key 를 입력한 사용자만 AI 개선안을 사용할 수 있습니다.")
    model = (req.model or "").strip() or PROVIDERS.get(req.provider, {}).get("default", "")
    if not model:
        raise LLMError("모델 이름을 입력해 주세요.")
    try:
        if req.provider.startswith("Anthropic"):
            return _call_anthropic(key, model, system, user, timeout)
        base_url = req.base_url.strip() or None
        if req.provider.startswith("OpenAI 호환") and not base_url:
            raise LLMError("OpenAI 호환 공급자는 Base URL 이 필요합니다. (예: https://openrouter.ai/api/v1)")
        return _call_openai(key, model, system, user, base_url, timeout)
    except LLMError:
        raise
    except Exception as e:  # 공급자 SDK 예외를 사용자 메시지로 변환 (키는 마스킹)
        raise LLMError(_mask(_describe_error(e), key)) from None


def _describe_error(e: Exception) -> str:
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    msg = getattr(e, "message", None) or str(e)
    hints = {
        401: "API Key 가 올바르지 않습니다.",
        403: "이 API Key 에 해당 모델 권한이 없습니다.",
        404: "모델 이름 또는 Base URL 을 확인해 주세요.",
        429: "요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.",
    }
    hint = hints.get(status, "")
    if status and status >= 500:
        hint = "공급자 서버 오류입니다. 잠시 후 다시 시도해 주세요."
    return f"LLM 호출 실패 ({name}{', HTTP ' + str(status) if status else ''}): {hint} {msg}".strip()


def _call_anthropic(key: str, model: str, system: str, user: str, timeout: float) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    try:
        # 안전 분류기가 거절하면 서버가 권장 모델로 재시도하도록 fallbacks 를 켠다.
        with client.messages.stream(
            **kwargs,
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        with client.messages.stream(**kwargs) as stream:  # fallbacks 미지원 모델이면 일반 호출
            msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise LLMError("모델이 요청을 거절했습니다. 입력 내용을 확인하거나 다른 모델을 선택해 주세요.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if msg.stop_reason == "max_tokens":
        text += "\n\n_(응답이 길이 제한으로 잘렸습니다.)_"
    return text or "_(빈 응답)_"


def _call_openai(key: str, model: str, system: str, user: str, base_url: Optional[str], timeout: float) -> str:
    import openai

    client = openai.OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=1)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()
    if choice.finish_reason == "length":
        text += "\n\n_(응답이 길이 제한으로 잘렸습니다.)_"
    return text or "_(빈 응답)_"
