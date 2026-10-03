"""scripts/grounding_probe.py의 순수 함수 테스트 — 네트워크·API 키 없이 돈다."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import grounding_probe as GP


def _resp(chunks, queries=("q",)):
    gm = NS(web_search_queries=list(queries),
            grounding_chunks=[NS(web=NS(title=t, uri=u)) for t, u in chunks])
    return NS(candidates=[NS(grounding_metadata=gm)])


class ClassifyDomainTests(unittest.TestCase):
    def test_academic_news_other(self):
        self.assertEqual(GP.classify_domain("www.jstage.jst.go.jp"), "academic")
        self.assertEqual(GP.classify_domain("cir.nii.ac.jp"), "academic")
        self.assertEqual(GP.classify_domain("u-tokyo.ac.jp"), "academic")
        self.assertEqual(GP.classify_domain("www.asahi.com"), "news")
        self.assertEqual(GP.classify_domain("nihongokyoshi-net.com"), "other")
        self.assertEqual(GP.classify_domain(""), "other")

    def test_lookalike_domain_is_not_academic(self):
        # 접미사 비교가 부분 문자열이면 이런 도메인이 통과한다
        self.assertEqual(GP.classify_domain("evil-asahi.com"), "other")
        self.assertEqual(GP.classify_domain("jstage.jst.go.jp.evil.com"), "other")


class ExtractSourcesTests(unittest.TestCase):
    def test_extracts_title_as_host(self):
        r = _resp([("www.jstage.jst.go.jp", "https://x/redirect/1"),
                   ("blog.example.com", "https://x/redirect/2")])
        out = GP.extract_sources(r)
        self.assertEqual([s["class"] for s in out["sources"]], ["academic", "other"])
        self.assertEqual(out["queries"], ["q"])

    def test_missing_grounding_metadata_returns_empty(self):
        out = GP.extract_sources(NS(candidates=[NS(grounding_metadata=None)]))
        self.assertEqual(out, {"queries": [], "sources": []})

    def test_broken_response_does_not_raise(self):
        self.assertEqual(GP.extract_sources(NS(candidates=[])), {"queries": [], "sources": []})
        self.assertEqual(GP.extract_sources(None), {"queries": [], "sources": []})


class ParseVerdictTests(unittest.TestCase):
    def test_parses_three_lines(self):
        t = "判定: 反証\n根拠: 後件は否定的結果\n出典種別: 論文"
        self.assertEqual(GP.parse_verdict(t),
                         {"verdict": "反証", "basis": "後件は否定的結果", "src_type": "論文"})

    def test_fullwidth_colon_and_missing_lines(self):
        self.assertEqual(GP.parse_verdict("判定:不明")["verdict"], "不明")
        self.assertEqual(GP.parse_verdict("")["verdict"], "")


if __name__ == "__main__":
    unittest.main()
