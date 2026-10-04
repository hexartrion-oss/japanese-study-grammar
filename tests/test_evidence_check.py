"""evidence_check.py — 네트워크 없이 도는 순수 함수 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evidence_check as EC
import ports


def _res(text="判定: 自然\n根拠: x\n出典種別: 論文", hosts=("nii.ac.jp",), status="ok"):
    return ports.EvidenceResult(
        status=status, text=text, latency_s=3.0, model="m",
        sources=tuple({"host": h, "title": h, "uri": "u"} for h in hosts))


class ClassifyDomainTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(EC.classify_domain("www.jstage.jst.go.jp"), "academic")
        self.assertEqual(EC.classify_domain("u-tokyo.ac.jp"), "academic")
        self.assertEqual(EC.classify_domain("www.asahi.com"), "news")
        self.assertEqual(EC.classify_domain("blognihongo.com"), "other")
        self.assertEqual(EC.classify_domain(""), "other")

    def test_lookalike_domains_are_not_trusted(self):
        self.assertEqual(EC.classify_domain("evil-asahi.com"), "other")
        self.assertEqual(EC.classify_domain("jstage.jst.go.jp.evil.com"), "other")
        self.assertEqual(EC.classify_domain("notac.jp"), "other")


class ParseVerdictTests(unittest.TestCase):
    def test_natural_unnatural_unknown(self):
        self.assertEqual(EC.parse_verdict("判定: 自然\n根拠: a")["verdict"], "natural")
        self.assertEqual(EC.parse_verdict("判定: 不自然\n根拠: a")["verdict"], "unnatural")
        self.assertEqual(EC.parse_verdict("判定: 不明")["verdict"], "unknown")

    def test_unnatural_is_not_mistaken_for_natural(self):
        # 「不自然」 안에 「自然」이 들어 있다 — 부분 문자열 검사가 뒤집으면 안 된다
        self.assertEqual(EC.parse_verdict("判定：不自然")["verdict"], "unnatural")

    def test_garbage_is_unknown(self):
        self.assertEqual(EC.parse_verdict("")["verdict"], "unknown")
        self.assertEqual(EC.parse_verdict("よくわかりません")["verdict"], "unknown")


class SummarizeTests(unittest.TestCase):
    def test_counts_qualified_sources_by_code_not_by_model_claim(self):
        s = EC.summarize(_res("判定: 自然\n出典種別: 論文", hosts=("blognihongo.com", "weblio.jp")))
        self.assertEqual(s["n_sources"], 2)
        self.assertEqual(s["n_qualified"], 0)      # 모델이 「論文」이라 해도 도메인이 부적격

    def test_failed_call_is_unavailable_not_unknown(self):
        s = EC.summarize(ports.EvidenceResult(status="quota"))
        self.assertEqual(s["verdict"], "unavailable")
        self.assertEqual(s["status"], "quota")

    def test_usable_for_overturn_requires_natural_and_qualified_source(self):
        ok = EC.summarize(_res(hosts=("nii.ac.jp",)))
        self.assertTrue(EC.usable_for_overturn(ok, min_qualified=1))
        self.assertFalse(EC.usable_for_overturn(ok, min_qualified=2))
        none = EC.summarize(_res(hosts=("weblio.jp",)))
        self.assertFalse(EC.usable_for_overturn(none, min_qualified=1))
        bad = EC.summarize(_res("判定: 不自然", hosts=("nii.ac.jp",)))
        self.assertFalse(EC.usable_for_overturn(bad, min_qualified=1))


class SentenceTests(unittest.TestCase):
    def test_extract_sentence_picks_the_one_with_the_pattern(self):
        passage = "朝になった。彼は怒ったあげく帰ってしまった。夜が来た。"
        got = EC.extract_sentence(passage, lambda s: "あげく" in s)
        self.assertEqual(got, "彼は怒ったあげく帰ってしまった。")

    def test_extract_sentence_falls_back_to_head(self):
        got = EC.extract_sentence("一文目。二文目。", lambda s: False)
        self.assertEqual(got, "一文目。")

    def test_prompt_contains_pattern_and_sentence_and_source_restriction(self):
        p = EC.build_prompt("あげく", "彼は怒ったあげく帰った。")
        self.assertIn("あげく", p)
        self.assertIn("彼は怒ったあげく帰った。", p)
        self.assertIn("学位論文", p)


if __name__ == "__main__":
    unittest.main()
