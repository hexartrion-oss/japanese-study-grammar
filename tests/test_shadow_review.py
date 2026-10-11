"""shadow 리뷰 개선(M1·M2·M3·M6) — 문장·이유·규칙 버전 저장, 부재 문형 제외, 빈 응답 사유 분리."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_grammar as G
import grammar_bank as B
import monthly_report as MR

_PATS = {p.id: p for c in B.CATEGORIES for p in c["patterns"]}


class AbsentPatternExcludedTests(unittest.TestCase):
    def test_absent_pattern_is_not_compared(self):
        # あげく가 지문에 없으면 구조 검사 결과 자체를 내지 않는다(M2)
        res = G.check_extra_structural("今日は晴れていた。散歩に出かけた。", [_PATS["あげく"]])
        self.assertEqual(res, {})

    def test_present_pattern_still_checked(self):
        res = G.check_extra_structural("さんざん悩んだあげく、見事に成功した。", [_PATS["あげく"]])
        self.assertEqual(res, {"あげく": False})


class AppendShadowReviewTests(unittest.TestCase):
    def test_new_fields_are_stored(self):
        class S:
            def __init__(self): self.w = None
            def write(self, name, data): self.w = data
        class D: pass
        d = D(); d.store = S()
        import datetime
        G.append_shadow_review(d, {"entries": []}, "부사", [{
            "pattern": "あげく", "code_ok": False, "judge_ok": True, "snippet": "x",
            "sentence": "悩んだあげく成功した。", "judge_note": "자연", "rules_version": G.RULES_VERSION,
        }], datetime.date(2026, 10, 11))
        e = d.store.w["entries"][0]
        self.assertEqual((e["sentence"], e["judge_note"], e["rules_version"]),
                         ("悩んだあげく成功した。", "자연", G.RULES_VERSION))


class EmptyResponseReasonTests(unittest.TestCase):
    def test_reason_type_separates_empty_response(self):
        self.assertEqual(MR._reason_type("Gemini 빈 응답(생성 호출 실패)"), "생성 호출 실패(빈 응답)")
        self.assertEqual(MR._reason_type("Gemini 출력 파싱 실패(--- 구분자 없음)"), "출력 파싱 실패")


if __name__ == "__main__":
    unittest.main()
