"""LLM 모듈 테스트 - 실제 API 를 호출하지 않고 SDK 를 가짜 객체로 대체한다."""
import sys
import types

import pytest

from xplanlens import analyze
from xplanlens import llm
from xplanlens.llm import LLMError, LLMRequest, build_compare_messages, build_messages, call_llm


def test_missing_key_is_rejected():
    with pytest.raises(LLMError, match="API Key"):
        call_llm(LLMRequest("Anthropic (Claude)", "  ", "claude-opus-5-5"), "s", "u")


def test_openai_compatible_requires_base_url():
    with pytest.raises(LLMError, match="Base URL"):
        call_llm(LLMRequest("OpenAI 호환 (Base URL 지정)", "sk-test-123456", "some-model"), "s", "u")


def test_prompt_contains_metrics_and_11g_constraints(sample):
    raw = sample("02_nested_loops_misestimate.txt")
    a = analyze(raw)
    system, user = build_messages(a, raw, sql_text="", extra="ORDERS 2천만 건", language="한국어")
    assert "11g" in system and "FETCH FIRST" in system and "ROWNUM" in system
    assert '"e_rows_total": 48210' in user
    assert "CARDINALITY_MISESTIMATE" in user
    assert "ORDERS 2천만 건" in user
    assert "Plan hash value: 2817364519" in user
    assert ":B1(VARCHAR2(30), CSID=846)='SHIPPED'" in user


def test_compare_prompt(sample):
    b = analyze(sample("02_nested_loops_misestimate.txt"))
    a = analyze(sample("03_hash_join_temp_spill.txt"))
    system, user = build_compare_messages(b, a, "diff", "raw-b", "raw-a")
    assert "Before" in user and "After" in user and "raw-b" in user


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMsg:
    def __init__(self, text="개선안", stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


def _install_fake_anthropic(monkeypatch, msg, captured):
    class BadRequestError(Exception):
        pass

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return msg

    class _Messages:
        def stream(self, **kwargs):
            captured.append(kwargs)
            return _Stream()

    class Anthropic:
        def __init__(self, api_key, timeout, max_retries):
            captured.append({"api_key": api_key})
            self.messages = _Messages()

    fake = types.SimpleNamespace(Anthropic=Anthropic, BadRequestError=BadRequestError)
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_anthropic_call_uses_key_model_and_fallbacks(monkeypatch):
    captured = []
    _install_fake_anthropic(monkeypatch, _FakeMsg("## 결과"), captured)
    out = call_llm(LLMRequest("Anthropic (Claude)", "sk-ant-abcdefgh", ""), "SYS", "USER")
    assert out == "## 결과"
    assert captured[0]["api_key"] == "sk-ant-abcdefgh"
    req = captured[1]
    assert req["model"] == "claude-opus-5-5"  # 모델 미지정 시 기본값
    assert req["system"] == "SYS"
    assert req["extra_body"] == {"fallbacks": "default"}
    assert "temperature" not in req


def test_anthropic_refusal(monkeypatch):
    _install_fake_anthropic(monkeypatch, _FakeMsg("", stop_reason="refusal"), [])
    with pytest.raises(LLMError, match="거절"):
        call_llm(LLMRequest("Anthropic (Claude)", "sk-ant-abcdefgh", "claude-opus-5-5"), "s", "u")


def test_error_message_masks_key(monkeypatch):
    secret = "sk-secret-0123456789"

    class Boom(Exception):
        status_code = 401

    class Anthropic:
        def __init__(self, **kw):
            raise Boom(f"bad key {secret}")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic, BadRequestError=Boom))
    with pytest.raises(LLMError) as ei:
        call_llm(LLMRequest("Anthropic (Claude)", secret, "claude-opus-5-5"), "s", "u")
    assert secret not in str(ei.value)
    assert "API Key 가 올바르지 않습니다" in str(ei.value)


def test_openai_call(monkeypatch):
    captured = {}

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            msg = types.SimpleNamespace(content="openai 결과")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    class OpenAI:
        def __init__(self, api_key, base_url, timeout, max_retries):
            captured["base_url"] = base_url
            self.chat = types.SimpleNamespace(completions=_Completions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=OpenAI))
    out = call_llm(LLMRequest("OpenAI 호환 (Base URL 지정)", "sk-test-123456", "m", base_url="https://x.example/v1"), "S", "U")
    assert out == "openai 결과"
    assert captured["base_url"] == "https://x.example/v1"
    assert captured["messages"][0] == {"role": "system", "content": "S"}


def test_provider_table_has_defaults():
    for name, info in llm.PROVIDERS.items():
        if not name.startswith("OpenAI 호환"):
            assert info["default"] in info["models"]
