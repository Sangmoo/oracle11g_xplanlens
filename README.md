---
title: XPlanLens Oracle 11g
emoji: 🔍
colorFrom: red
colorTo: yellow
sdk: gradio
sdk_version: 5.50.0
python_version: "3.11"
app_file: app.py
pinned: false
license: mit
short_description: Oracle 11gR2 DBMS_XPLAN plan analyzer & visualizer
tags:
  - oracle
  - sql-tuning
  - execution-plan
  - dbms_xplan
---

# 🔍 XPlanLens — Oracle 11g 실행계획 분석기

> **Oracle 11gR2 `DBMS_XPLAN` 출력 전용** 실행계획 시각화·진단 도구 (Oracle 판 *explain.depesz*)
>
> 🤗 **데모**: <https://huggingface.co/spaces/Sangmoo/oracle11g_xplanlens> · 💻 **소스**: <https://github.com/Sangmoo/oracle11g_xplanlens>

`DBMS_XPLAN.DISPLAY_CURSOR(..., 'ALLSTATS LAST')` 결과를 붙여넣으면

1. 실행계획을 **트리 다이어그램**과 **단계별 히트맵 표**로 시각화하고,
2. 추정/실제 건수 괴리, NL 조인 반복 실행, TEMP 사용, 형변환 같은 병목을 **규칙 기반으로 진단**하며,
3. 사용자가 **본인의 LLM API Key 를 입력한 경우에만** LLM 을 호출해 11g 에서 적용 가능한 개선안을 제안합니다.

국내 SI·레거시 현장에 여전히 많은 11g 환경에서, DB 접속 없이 실행계획 텍스트만으로 튜닝 포인트를 빠르게 찾는 것이 목표입니다.

---

## 목차

- [주요 기능](#-주요-기능)
- [지원 범위](#-지원-범위)
- [사용 방법](#-사용-방법)
- [진단 규칙](#-진단-규칙)
- [AI 개선안과 API Key 정책](#-ai-개선안과-api-key-정책)
- [로컬 실행](#-로컬-실행)
- [테스트](#-테스트)
- [Hugging Face Space 에 등록하기](#-hugging-face-space-에-등록하기)
- [프로젝트 구조](#-프로젝트-구조)
- [라이브러리로 사용하기](#-라이브러리로-사용하기)
- [한계와 로드맵](#-한계와-로드맵)

---

## ✨ 주요 기능

| 기능 | 설명 |
|---|---|
| **실행계획 파서** | `DISPLAY` / `DISPLAY_CURSOR` / `DISPLAY_AWR` 텍스트 출력을 해석합니다. SQL_ID, SQL 텍스트, Plan hash value, 실행계획 표, Predicate Information, Peeked Binds, Note 섹션을 읽습니다. SQL\*Plus 의 `PLAN_TABLE_OUTPUT` 머리글, `n rows selected.` 꼬리말, CRLF/탭도 처리합니다. |
| **트리 다이어그램** | 부모→자식 구조를 SVG 로 그립니다. 박스 색은 그 단계 자체 부하(하위 단계 제외) 비중, 선 굵기는 전달 건수, 테두리/점 색은 진단 심각도입니다. 박스에 마우스를 올리면 조건절과 진단 내용이 보입니다. |
| **단계별 히트맵 표** | Starts, **E-Rows×Starts**, A-Rows, 괴리 배수(↑과소/↓과대 추정), 자체 시간·자체 Buffers 막대, Reads, Used-Mem/Used-Tmp, 조건절을 한 줄에 보여줍니다. 실행 통계가 없으면 Rows/Bytes/Cost/Cost 비중을 보여줍니다. |
| **규칙 기반 진단** | 16가지 규칙과 Note 해석으로 🔴심각 / 🟠경고 / 🔵참고 등급의 진단과 개선 방향을 제시합니다. ([목록](#-진단-규칙)) |
| **AI 개선안** (선택) | Anthropic Claude, OpenAI, OpenAI 호환 API(OpenRouter, 사내 게이트웨이, vLLM 등) 중 하나를 골라 본인 API Key 로 호출합니다. 규칙 진단 결과와 단계별 지표(JSON)를 함께 넘겨 LLM 이 숫자를 잘못 읽는 것을 줄였습니다. |
| **튜닝 전/후 비교** | 두 실행계획의 수행시간·Buffers·Reads·Cost 변화율, 오브젝트별 접근 경로 변화, 조인 방식 변화, 해결된 진단/새로 생긴 진단을 비교합니다. AI 비교 해설도 받을 수 있습니다. |
| **파일 업로드** | `.txt .sql .log .lst .html` 파일을 올리면 텍스트로 변환합니다. UTF-8 → CP949(국내 Windows 기본) → Latin-1 순으로 디코딩하고, HTML 로 저장된 출력은 태그를 제거합니다. |
| **HTML 보고서** | 요약·진단, 트리, 표, AI 개선안, 원문을 외부 리소스 없는 단일 HTML 파일로 내려받습니다. 메일 첨부나 산출물 보관용입니다. |
| **JSON 결과** | 파싱·분석 결과 전체를 JSON 으로 볼 수 있어 다른 도구와 연동하기 쉽습니다. |

---

## 🎯 지원 범위

**Oracle 11g Release 2 (11.2.0.x)** 의 `DBMS_XPLAN` 텍스트 출력만 공식 지원합니다.

| 항목 | 지원 |
|---|---|
| `DBMS_XPLAN.DISPLAY` (EXPLAIN PLAN) | ✅ Rows / Bytes / Cost (%CPU) / Time / Pstart / Pstop / TQ 등 |
| `DBMS_XPLAN.DISPLAY_CURSOR` `ALLSTATS LAST` | ✅ Starts / E-Rows / A-Rows / A-Time / Buffers / Reads / Writes / OMem / 1Mem / Used-Mem / Used-Tmp |
| `+PEEKED_BINDS` | ✅ |
| Predicate Information (여러 줄로 감긴 조건 포함) | ✅ |
| Note 섹션 | ✅ dynamic sampling, cardinality feedback(11.2), SQL plan baseline, SQL profile, stored outline, RBO, 구버전 PLAN_TABLE 등 |
| Column Projection / Outline Data / Query Block | ⏭️ 읽고 건너뜀 |
| 12c 이상 Adaptive Plan(`-` 표시 행), Statistics Feedback, SQL Plan Directive | ⚠️ 에러 없이 **"미지원 항목"** 으로 표시 |

> 이름에 버전을 넣지 않은 이유: 나중에 12c 이상으로 확장할 여지를 남기기 위해서입니다. 해석하지 못한 행·컬럼·Note·섹션은 예외를 던지지 않고 `Plan.unsupported` 에 기록한 뒤 넘어갑니다.

### 11g 에서 꼭 알아야 할 점: E-Rows 는 Starts 1회당 값

11g 의 `ALLSTATS LAST` 에서 **E-Rows 는 한 번 실행될 때의 추정치**이고, **A-Rows 는 모든 반복 실행의 합계**입니다. 그래서 NESTED LOOPS 안쪽처럼 `Starts` 가 큰 단계에서 E-Rows 와 A-Rows 를 그대로 비교하면 엉뚱한 결론이 나옵니다.
XPlanLens 는 **E-Rows × Starts** 로 비교합니다. 단, `PARTITION ... ` / `PX ...` 반복자 아래의 E-Rows 는 이미 전체 추정치이므로 곱하지 않습니다.

---

## 🚀 사용 방법

### 1) 실행계획 뽑기 (권장: 실제 실행 통계 포함)

```sql
-- SQL*Plus 라면 잘리지 않게
SET LINESIZE 300 PAGESIZE 0 TRIMSPOOL ON

-- 1. 힌트를 넣어 SQL 을 한 번 실행 (또는 ALTER SESSION SET STATISTICS_LEVEL = ALL;)
SELECT /*+ GATHER_PLAN_STATISTICS */ ...;

-- 2. 같은 세션에서 직전 커서의 실행계획 조회
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(NULL, NULL, 'ALLSTATS LAST +PEEKED_BINDS'));

-- 이미 수행된 SQL 이라면 SQL_ID 로 조회 (statistics_level=ALL 로 수행된 경우에만 A-Rows 가 나옴)
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('&sql_id', NULL, 'ALLSTATS LAST +PEEKED_BINDS'));

-- AWR 에 남은 과거 계획
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_AWR('&sql_id'));
```

`EXPLAIN PLAN FOR ...` + `DBMS_XPLAN.DISPLAY` 결과도 분석하지만, 추정치만 있고 바인드 피킹도 하지 않아 실제 실행계획과 다를 수 있습니다.

### 2) 화면 사용

1. **📊 실행계획 분석** 탭에 출력 전체를 붙여넣거나 파일을 올리거나 샘플을 고릅니다.
2. **🔍 분석** → `요약·진단`, `트리 다이어그램`, `단계별 표`, `JSON` 탭에서 결과를 확인합니다.
3. (선택) 상단 **🤖 AI 설정**에 공급자·모델·API Key 를 넣고 **AI 개선안 받기**를 누릅니다. SQL 원문과 테이블 건수·인덱스 구성 같은 추가 정보를 넣으면 더 정확해집니다.
4. **📥 HTML 보고서 만들기**로 결과를 내려받습니다.
5. 튜닝 후에는 **⚖️ 튜닝 전/후 비교** 탭에 두 실행계획을 넣어 개선 효과를 확인합니다.

### 내장 샘플

| 샘플 | 내용 |
|---|---|
| ① EXPLAIN PLAN 기본 | 실제 11g 서버에서 받은 출력 (`'PLAN_TABLE' is old version` Note 포함) |
| ② NL 조인 + 카디널리티 오류 | E-Rows 1 vs A-Rows 48K, 인덱스로 121만 건 읽고 4.8만 건만 남기는 패턴, NL 내부 4.8만 회 반복, Peeked Binds, Cardinality Feedback |
| ③ 해시 조인 TEMP 사용 | Build 쪽 509배 과소 추정 → one-pass TEMP 306M, PARTITION RANGE ALL, dynamic sampling |
| ④ FILTER 서브쿼리 + 카티션 | Unnesting 실패, MERGE JOIN CARTESIAN, `TO_NUMBER(컬럼)` 형변환 |

---

## 🩺 진단 규칙

| 규칙 ID | 등급 | 탐지 조건 | 개선 방향 |
|---|---|---|---|
| `NO_RUNTIME_STATS` | 참고 | A-Rows 컬럼 없음 | `GATHER_PLAN_STATISTICS` 힌트 / `STATISTICS_LEVEL=ALL` 안내 |
| `CARDINALITY_MISESTIMATE` | 경고(≥10배) / 심각(≥100배) | (E-Rows×Starts) 와 A-Rows 괴리. **괴리가 처음 생긴 단계만** 보고하고 전파된 상위 단계는 함께 표시 | 통계 갱신, 히스토그램, 확장 통계, DYNAMIC_SAMPLING/CARDINALITY 힌트 |
| `HIGH_STARTS` | 경고 | NESTED LOOPS / FILTER 의 자식이 1,000회 이상 반복 | USE_HASH, LEADING, UNNEST, 커버링 인덱스 |
| `INDEX_FILTER_WASTE` | 경고 | 인덱스가 넘긴 ROWID 의 90% 이상을 테이블 필터가 버림 | 필터 컬럼을 포함한 결합 인덱스 |
| `TIME_HOTSPOT` | 경고 | 자체 A-Time 이 전체의 30% 이상 | 튜닝 1순위 단계 |
| `BUFFER_HOTSPOT` | 참고 | 자체 Buffers 가 전체의 30% 이상 | 블록 접근량 감소 |
| `FULL_SCAN_WITH_FILTER` | 경고 | 필터 조건이 있는 FULL SCAN 이 Buffers 20% 이상(또는 Cost 50% 이상) | 인덱스 검토, 인덱스 미사용 원인 확인 |
| `CARTESIAN_JOIN` | 심각 | MERGE JOIN CARTESIAN | 조인 조건 누락 / 1건 추정 오류 확인 |
| `IMPLICIT_CONVERSION` | 경고 | 조건절에서 컬럼에 `INTERNAL_FUNCTION/TO_NUMBER/TO_CHAR/TO_DATE/SYS_OP_C2C` 적용 | 바인드·리터럴 타입 일치, 함수 기반 인덱스 |
| `FILTER_SUBQUERY` | 경고 | 자식이 2개 이상인 FILTER (서브쿼리 Unnesting 실패) | UNNEST 힌트, 세미/안티 조인 재작성 |
| `TEMP_SPILL` | 경고(one-pass) / 심각(multi-pass) | Used-Tmp 존재 또는 Used-Mem 의 pass ≥ 1 | 대상 집합 축소, Build 입력 교체, PGA 조정 |
| `NO_PARTITION_PRUNING` | 경고 | PARTITION RANGE/LIST/HASH ALL | 파티션 키 조건 추가, 키 컬럼 가공 금지 |
| `INDEX_SKIP_SCAN` | 참고 | INDEX SKIP SCAN | 선두 컬럼 조건 / 인덱스 컬럼 순서 |
| `INDEX_FULL_SCAN` | 참고 | 정렬 회피 목적이 아닌 INDEX FULL SCAN | INDEX_FFS / FULL 과 비교 |
| `PEEKED_BINDS` | 참고 | Peeked Binds 존재 | 바인드 값 분포·Adaptive Cursor Sharing 확인 |
| Note 해석 | 참고/경고 | dynamic sampling, cardinality feedback, SPM baseline, SQL profile, outline, RBO, CPU costing off, 구버전 PLAN_TABLE, 12c 이상 Note | 항목별 안내. 모르는 Note 는 "미지원 항목" |

임계값은 [`src/xplanlens/rules.py`](src/xplanlens/rules.py) 상단 상수(`MISESTIMATE_WARN`, `HIGH_STARTS`, `HOTSPOT_SHARE` 등)로 조정할 수 있습니다.

---

## 🤖 AI 개선안과 API Key 정책

- **API Key 를 입력한 사용자만** LLM 을 호출합니다. Key 가 비어 있으면 LLM 호출 코드에 도달하지 않고 안내 문구만 보여줍니다(테스트로 보장).
- Key 는 `type="password"` 입력란으로 받아 **해당 요청을 처리하는 동안 메모리에서만** 사용합니다. 파일·로그·브라우저 저장소에 남기지 않고, 오류 메시지에 Key 가 섞이면 마스킹합니다.
- AI 관련 이벤트는 `api_name=False` 로 Gradio 공개 API 목록에서 숨겼습니다.
- 실행계획과 SQL 은 **선택한 LLM 공급자로 전송**됩니다. 고객 데이터가 담긴 리터럴/바인드 값은 붙여넣기 전에 가려 주세요.
- 운영자 Key 를 Space Secret 으로 넣는 방식은 **사용하지 않습니다.** 공개 Space 에서 다른 사람이 운영자 비용으로 호출하는 것을 막기 위해서입니다.

| 공급자 | 기본 모델 | 비고 |
|---|---|---|
| Anthropic (Claude) | `claude-opus-5-5` | `claude-sonnet-5-5`, `claude-haiku-5-5` 선택 가능. 공식 `anthropic` SDK, 스트리밍 호출, 안전 분류기 거절 시 서버 측 대체 모델(`fallbacks: "default"`) 사용 |
| OpenAI | `gpt-5-mini` | 공식 `openai` SDK |
| OpenAI 호환 | 직접 입력 | Base URL 필수 (예: `https://openrouter.ai/api/v1`, 사내 LLM 게이트웨이, vLLM/Ollama 의 `/v1`) |

모델 목록은 드롭다운이지만 **직접 입력**도 됩니다. 시스템 프롬프트는 11gR2 문법만 쓰도록(예: `FETCH FIRST` 대신 `ROWNUM`), E-Rows×Starts 기준으로 판단하도록, 실행계획에 없는 정보는 추측임을 밝히고 확인 SQL 을 제시하도록 지시합니다. 프롬프트는 [`src/xplanlens/llm.py`](src/xplanlens/llm.py) 에서 수정할 수 있습니다.

---

## 💻 로컬 실행

Python 3.10 이상이 필요합니다.

```bash
git clone https://github.com/Sangmoo/oracle11g_xplanlens.git
cd oracle11g_xplanlens
python -m venv .venv
# Windows: .venv\Scripts\activate   /   macOS·Linux: source .venv/bin/activate
pip install "gradio==5.50.0" -r requirements.txt
python app.py
```

브라우저에서 <http://127.0.0.1:7860> 을 엽니다. 다른 PC 에서 접속하려면 `GRADIO_SERVER_NAME=0.0.0.0`, 포트를 바꾸려면 `GRADIO_SERVER_PORT` 환경 변수를 지정합니다.

---

## 🧪 테스트

```bash
pip install pytest
pytest -q
```

59개 테스트가 다음을 검증합니다. 실제 LLM API 는 호출하지 않고 SDK 를 가짜 객체로 바꿔 테스트합니다.

| 파일 | 검증 내용 |
|---|---|
| `tests/test_parser.py` | 단위(K/M/G) 변환, Cost/시간/메모리 파싱, 들여쓰기 기반 트리 구성, 여러 줄 조건절, 같은 Id 의 두 번째 조건, Peeked Binds, SQL\*Plus 머리글/꼬리말, CRLF, 12c Adaptive 행, 알 수 없는 컬럼, 잘못된 입력에서 예외가 나지 않는지 |
| `tests/test_rules.py` | E-Rows×Starts, 자체 시간·버퍼 계산, 파티션 반복자 예외, 근본 원인 단계만 보고하는지, 각 규칙의 탐지 여부와 대상 단계, 작은 쿼리에서 오탐이 없는지, JSON 직렬화 |
| `tests/test_render_report_compare.py` | 모든 샘플의 SVG 가 올바른 XML 인지, 트리 레이아웃, HTML 이스케이프(XSS 방지), 보고서 구성, CP949/HTML 파일 읽기, 튜닝 전/후 비교 |
| `tests/test_llm.py` | Key 없을 때 거부, Base URL 필수, 프롬프트에 지표·진단·11g 제약 포함, Anthropic/OpenAI 호출 인자, 거절 응답 처리, 오류 메시지의 Key 마스킹 |
| `tests/test_app.py` | Gradio 핸들러: 샘플 로딩, 분석 출력, Key 없을 때 LLM 미호출, 보고서 파일 생성, 비교, 공급자 변경 |

GitHub 에 push 하면 [`.github/workflows/tests.yml`](.github/workflows/tests.yml) 이 Python 3.10 / 3.12 에서 테스트를 실행합니다.

---

## 🤗 Hugging Face Space 에 등록하기

이 저장소는 **그대로 Space 저장소로 쓸 수 있게** 구성되어 있습니다.

- `README.md` 맨 위의 YAML 블록이 Space 설정입니다 (`sdk: gradio`, `sdk_version: 5.50.0`, `app_file: app.py`).
- `requirements.txt` 는 Space 가 설치할 추가 패키지입니다. Gradio 는 `sdk_version` 으로 설치되므로 넣지 않았습니다.
- `app.py` 가 `src/` 를 import 경로에 추가하므로 패키지 설치 과정이 필요 없습니다.

### 방법 A. 웹에서 Space 를 만들고 git 으로 올리기 (가장 간단)

1. <https://huggingface.co/join> 에서 가입 후 로그인합니다.
2. <https://huggingface.co/settings/tokens> → **Create new token** → 종류 **Write** 로 토큰을 만듭니다. (git push 비밀번호로 사용)
3. <https://huggingface.co/new-space> 에서
   - **Owner**: 본인 계정, **Space name**: `oracle11g_xplanlens`
   - **License**: MIT
   - **Select the Space SDK**: **Gradio** → 템플릿 **Blank**
   - **Space hardware**: **CPU basic (Free)** — 규칙 분석은 가볍고 LLM 은 외부 API 라 무료 CPU 로 충분합니다.
   - **Public** 선택 후 **Create Space**
4. 로컬 저장소에서 Space 를 원격으로 추가하고 push 합니다.

```bash
git remote add space https://huggingface.co/spaces/Sangmoo/oracle11g_xplanlens
git push --force space main
```

   - 사용자명/비밀번호를 물으면 비밀번호 자리에 2번의 **Write 토큰**을 입력합니다.
   - `--force` 는 Space 생성 시 만들어진 기본 README 커밋을 이 저장소 내용으로 덮어쓰기 위해 처음 한 번만 필요합니다.
5. Space 페이지의 **Logs** 탭에서 빌드가 끝나면(1~3분) **App** 탭에서 바로 사용할 수 있습니다.

### 방법 B. `huggingface_hub` CLI 로 올리기

```bash
pip install -U huggingface_hub
huggingface-cli login                       # Write 토큰 입력
huggingface-cli repo create oracle11g_xplanlens --type space --space_sdk gradio
huggingface-cli upload Sangmoo/oracle11g_xplanlens . . --repo-type space \
  --exclude ".git/*" ".venv/*" "__pycache__/*" ".pytest_cache/*"
```

### 방법 C. GitHub 에 push 하면 Space 에 자동 반영 (GitHub Actions)

1. GitHub 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서 `HF_TOKEN` 이름으로 HF Write 토큰을 등록합니다.
2. 아래 파일을 `.github/workflows/sync-to-hf.yml` 로 추가합니다. (다른 계정이라면 `Sangmoo` 를 본인 HF 사용자명으로 바꾸세요)

```yaml
name: Sync to Hugging Face Space
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          lfs: true
      - name: Push to Space
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
        run: git push --force https://Sangmoo:$HF_TOKEN@huggingface.co/spaces/Sangmoo/oracle11g_xplanlens main
```

### 등록 후 확인할 것

- **Settings → Variables and secrets** 에 LLM API Key 를 **넣지 마세요.** 이 앱은 사용자 각자의 Key 만 사용합니다.
- Space 가 48시간 동안 접속이 없으면 무료 CPU 는 잠듭니다(Sleeping). 첫 접속 때 30초 정도 깨어나는 시간이 걸립니다.
- 사내 전용으로 쓰려면 Space 를 **Private** 으로 만들거나, 로컬/사내 서버에서 `python app.py` 로 실행하세요.
- `sdk_version` 을 올릴 때는 로컬에서 같은 버전으로 `pytest` 를 먼저 돌려 보세요. (Gradio 6 에서는 `css`/`theme` 인자 위치와 `api_name=False` 표기가 바뀝니다.)

---

## 🗂 프로젝트 구조

```
oracle11g_xplanlens/
├── app.py                         # Gradio UI (Hugging Face Space 진입점)
├── requirements.txt               # Space 런타임 의존성
├── pyproject.toml                 # 패키지 메타데이터, pytest 설정
├── src/xplanlens/
│   ├── models.py                  # Plan / PlanNode / Predicate / PeekedBind 데이터 모델
│   ├── parser.py                  # DBMS_XPLAN 텍스트 파서
│   ├── rules.py                   # 파생 지표(E-Rows×Starts, 자체 시간/버퍼) + 진단 규칙
│   ├── render.py                  # 요약 Markdown, 히트맵 HTML 표, SVG 트리
│   ├── compare.py                 # 튜닝 전/후 비교
│   ├── llm.py                     # 프롬프트 구성, Anthropic/OpenAI 호출
│   ├── report.py                  # 파일 읽기(CP949/HTML), HTML 보고서
│   └── samples/                   # 내장 샘플 실행계획
├── tests/                         # pytest (fixtures/ 에 추가 입력 샘플)
└── .github/workflows/tests.yml    # CI
```

처리 흐름:

```
DBMS_XPLAN 텍스트 ─▶ parser.parse_plan ─▶ Plan(트리)
                                         │
                                         ▼
                         rules.run_rules (파생 지표 + 진단) ─▶ Analysis
                                         │
             ┌───────────────┬───────────┼──────────────┬──────────────┐
             ▼               ▼           ▼              ▼              ▼
      render_summary   render_tree_svg  render_table  llm.build_messages  compare_plans
         (Markdown)        (SVG)         (HTML)       → call_llm (Key 있을 때만)
```

---

## 📦 라이브러리로 사용하기

UI 없이 파이썬 코드에서 바로 쓸 수 있습니다. 핵심 분석은 표준 라이브러리만 사용합니다.

```python
import sys; sys.path.insert(0, "src")
from xplanlens import analyze

a = analyze(open("plan.txt", encoding="utf-8").read())
for f in a.findings:
    print(f.severity, f.title)
print(a.plan.node(5).misestimate_ratio)
```

---

## 🛣 한계와 로드맵

**현재 한계**

- 텍스트 출력만 해석합니다. `V$SQL_PLAN_STATISTICS_ALL` 직접 조회, SQL Monitor 보고서, 10046 트레이스는 지원하지 않습니다.
- 병렬 실행(PX) 계획은 표시는 되지만 TQ/IN-OUT 기반 분석은 하지 않습니다.
- 11g 의 A-Time 은 일부 단계(예: 해시 조인 Build, COUNT STOPKEY)에서 정확히 누적되지 않을 수 있어 자체 시간은 근사치입니다.
- 진단 규칙은 실행계획만 보고 판단하므로 테이블 크기·인덱스 구성을 알 수 없습니다. 추가 정보는 AI 분석 입력란에 적어 주세요.

**로드맵**

- [ ] `DBMS_SQLTUNE.REPORT_SQL_MONITOR` 텍스트 지원
- [ ] Gradio MCP 서버 모드로 AI 에이전트에서 분석 도구로 호출
- [ ] PyPI 패키지 배포 (`pip install xplanlens`)
- [ ] 12c 이상 출력 정식 지원 (Adaptive Plan 활성 행만 분석)

---

## 📄 라이선스

[MIT](LICENSE)

XPlanLens 는 Oracle Corporation 과 무관한 오픈소스 프로젝트입니다. Oracle 과 Java 는 Oracle 및 그 계열사의 등록상표입니다. AI 개선안은 참고용이며, 운영 환경 적용 전 반드시 검증하세요.
