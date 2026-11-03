"""Gradio 앱 핸들러 스모크 테스트 (gradio 미설치 환경에서는 건너뜀)."""
from pathlib import Path

import pytest

gr = pytest.importorskip("gradio")
import app  # noqa: E402


def test_samples_registered():
    assert len(app.SAMPLES) == 4
    for label in app.SAMPLES:
        assert "Plan hash value" in app.load_sample(label)


def test_run_analysis_outputs():
    text = app.load_sample(next(iter(app.SAMPLES)))
    summary, tree, table, js, ai = app.run_analysis(text)
    assert "실행계획 요약" in summary
    assert "<svg" in tree and "<table" in table
    assert js["plan"]["nodes"]
    assert ai == ""


def test_run_analysis_empty_raises():
    with pytest.raises(gr.Error):
        app.run_analysis("   ")


def test_ai_without_key_does_not_call_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM 이 호출되면 안 됩니다")

    monkeypatch.setattr(app, "call_llm", boom)
    out = app.run_ai("x", "", "", "Anthropic (Claude)", "", "claude-opus-5-5", "", "한국어")
    assert "API Key" in out
    out = app.run_compare_ai("x", "y", "OpenAI", None, "gpt-5-mini", "", "한국어")
    assert "API Key" in out


def test_ai_with_key_calls_llm(monkeypatch):
    seen = {}

    def fake(req, system, user):
        seen["req"] = req
        return "AI 결과"

    monkeypatch.setattr(app, "call_llm", fake)
    text = list(app.SAMPLES.values())[1].read_text(encoding="utf-8")
    out = app.run_ai(text, "", "", "Anthropic (Claude)", "sk-ant-xxxxxxxx", "claude-opus-5-5", "", "한국어",
                     progress=lambda *a, **k: None)
    assert out == "AI 결과"
    assert seen["req"].api_key == "sk-ant-xxxxxxxx"


def test_export_report_writes_html():
    text = list(app.SAMPLES.values())[1].read_text(encoding="utf-8")
    path = Path(app.export_report(text, "AI 메모"))
    assert path.suffix == ".html" and "8fk2zq1m3x7cd" in path.name
    assert "AI 메모" in path.read_text(encoding="utf-8")


def test_compare_handler():
    texts = [p.read_text(encoding="utf-8") for p in app.SAMPLES.values()]
    md, t1, t2, ai = app.run_compare(texts[1], texts[2])
    assert "비교" in md and "<svg" in t1 and "<svg" in t2


def test_provider_change():
    model_upd, url_upd = app.on_provider_change("OpenAI 호환 (Base URL 지정)")
    assert url_upd["visible"] is True
    model_upd, url_upd = app.on_provider_change("OpenAI")
    assert model_upd["value"] == "gpt-5-mini" and url_upd["visible"] is False
