"""GeminiLlm 폴백 체인 — 순서, 빈 응답 전환, 대기 상한, 응답 모델 기록."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters


class FakeClient:
    """script: {모델id: [응답 또는 예외, ...]} — 호출 순서대로 소비."""

    def __init__(self, script, calls):
        self.script, self.calls = script, calls
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append((model, config))
        item = self.script[model].pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, model_version=model + "-001")


def run(script, **kw):
    calls, slept, logs = [], [], []
    llm = adapters.GeminiLlm("k", logs.append, sleep=slept.append, **kw)
    with mock.patch.object(adapters, "GEMINI_AVAILABLE", True), \
         mock.patch.object(adapters, "google_genai", create=True) as g, \
         mock.patch.object(adapters, "genai_types", create=True) as t:
        g.Client.side_effect = lambda api_key: FakeClient(script, calls)
        t.ThinkingConfig.side_effect = lambda **k: ("think", k)
        t.GenerateContentConfig.side_effect = lambda **k: k
        t.HttpOptions.side_effect = lambda **k: k
        out = llm.generate("p", 0.5)
    return out, llm, calls, slept, logs


class ChainTests(unittest.TestCase):
    def test_order(self):
        self.assertEqual(adapters.GEMINI_MODELS, [
            "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3-flash-preview",
            "gemini-3.5-flash", "gemini-3.6-flash"])

    def test_first_model_used_when_ok(self):
        out, llm, calls, _, _ = run({"gemini-2.5-flash": ["본문"]})
        self.assertEqual((out, [c[0] for c in calls], llm.last_model),
                         ("본문", ["gemini-2.5-flash"], "gemini-2.5-flash-001"))

    def test_empty_response_moves_to_next_model(self):
        out, llm, calls, _, logs = run({"gemini-2.5-flash": [""], "gemini-3.5-flash-lite": ["본문"]})
        self.assertEqual(out, "본문")
        self.assertEqual([c[0] for c in calls], ["gemini-2.5-flash", "gemini-3.5-flash-lite"])
        self.assertTrue(any("빈 응답" in m for m in logs))

    def test_other_error_moves_on_and_404_does_not_stop_chain(self):
        script = {"gemini-2.5-flash": [Exception("500 boom")],
                  "gemini-3.5-flash-lite": [Exception("404 not found")],
                  "gemini-3-flash-preview": ["본문"]}
        out, llm, calls, _, _ = run(script)
        self.assertEqual((out, llm.last_model), ("본문", "gemini-3-flash-preview-001"))

    def test_thinking_config_per_family(self):
        _, _, calls, _, _ = run({"gemini-2.5-flash": [""], "gemini-3.5-flash-lite": [""],
                                 "gemini-3-flash-preview": ["x"]})
        by = {m: cfg.get("thinking_config") for m, cfg in calls}
        self.assertEqual(by["gemini-2.5-flash"], ("think", {"thinking_budget": 0}))
        self.assertIn("thinking_level", by["gemini-3-flash-preview"][1])

    def test_all_fail_returns_empty_and_last_model_none(self):
        script = {m: [Exception("500 x")] for m in adapters.GEMINI_MODELS}
        out, llm, _, _, _ = run(script)
        self.assertEqual((out, llm.last_model), ("", None))

    def test_wait_budget_caps_total_sleep(self):
        quota = lambda: Exception("429 quota. retry in 100s")
        script = {m: [quota(), quota()] for m in adapters.GEMINI_MODELS}
        out, _, calls, slept, logs = run(script)
        self.assertEqual(out, "")
        self.assertLessEqual(sum(slept), adapters.GENERATE_WAIT_BUDGET_S)
        self.assertTrue(any("대기 상한" in m for m in logs))


class EnvModelsTests(unittest.TestCase):
    def test_default_when_empty_or_invalid(self):
        self.assertEqual(adapters.models_from_env({}), adapters.GEMINI_MODELS)
        self.assertEqual(adapters.models_from_env({"GEMINI_MODELS": "rm -rf"}), adapters.GEMINI_MODELS)

    def test_override(self):
        self.assertEqual(adapters.models_from_env({"GEMINI_MODELS": "gemini-3-flash-preview, gemini-2.5-flash"}),
                         ["gemini-3-flash-preview", "gemini-2.5-flash"])


if __name__ == "__main__":
    unittest.main()
