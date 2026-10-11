"""어휘 혼동 규칙(B)·프롬프트 주의문(C)·교정 패스 shadow(A)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_grammar as G
import proofread_check as PC
import ports
from tests.fakes import make_deps  # noqa: E402


class VocabConfusionTests(unittest.TestCase):
    BAD = ["暖かいコーヒーを飲んだ。", "暖かいお茶で一息ついた。", "暖かいスープが身にしみた。",
           "暖かな料理が並んだ。"]
    GOOD = ["温かいコーヒーを飲んだ。", "暖かい部屋でコーヒーを飲んだ。", "暖かい日差しの中で料理をした。",
            "今日は暖かい。お茶を入れた。", "暖かい服を着て鍋を囲んだ。", "暖かく迎えた食事の時間。"]

    def test_wrong_pairs_detected(self):
        for s in self.BAD:
            with self.subTest(s=s):
                self.assertTrue(G.check_vocab_confusions(s), s)

    def test_correct_usage_not_flagged(self):
        for s in self.GOOD:
            with self.subTest(s=s):
                self.assertEqual(G.check_vocab_confusions(s), [], s)


class PromptNoteTests(unittest.TestCase):
    def test_generation_prompt_has_vocab_rule(self):
        deps, _ = make_deps()
        p = G.build_prompt(deps, [], "N3")
        self.assertIn("温かい", p)
        self.assertIn("暖かい", p)


class ProofreadParseTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(PC.parse("なし"), [])
        self.assertEqual(PC.parse(""), [])

    def test_parses_lines_and_drops_garbage(self):
        raw = "暖かい|温かい|語彙|飲み物には温かい\n説明文だけの行\n同じ|同じ|語彙|x"
        self.assertEqual(PC.parse(raw), [{"wrong": "暖かい", "right": "温かい", "kind": "語彙",
                                          "reason": "飲み物には温かい"}])


class ProofreadShadowTests(unittest.TestCase):
    def _run(self, mode):
        from tests.test_pipeline import _full_passage
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")],
                                settings=ports.Settings(proofread_mode=mode))
        sink = []
        G.generate_passage(deps, G.GB.CATEGORY_BY_KEY["부사"], {"patterns": {}}, attempt_sink=sink)
        return fakes, sink

    def test_off_makes_no_extra_call(self):
        fakes, sink = self._run("off")
        self.assertFalse(any("校正" in c["prompt"] for c in fakes["llm"].calls))
        self.assertIsNone(sink[0]["proofread"])

    def test_shadow_records_without_changing_outcome(self):
        fakes, sink = self._run("shadow")
        calls = [c for c in fakes["llm"].calls if "校正" in c["prompt"]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], ports.Settings().proofread_model)
        self.assertEqual(sink[0]["outcome"], "passed")
        self.assertEqual(sink[0]["proofread"], [])


if __name__ == "__main__":
    unittest.main()
