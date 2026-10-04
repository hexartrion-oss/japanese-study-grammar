"""검증기(활용형 허용 + 어휘 목록) 양방향 회귀 세트.

기존 테스트는 "통과해야 하는 사례"를 거의 안 가졌다 — 2026-10-03 감사에서 자연스러운 문장이
활용형·목록 밖 어휘 때문에 오탈락하는 것이 확인됐다(손작업 자연문 24개 중 10개). 그래서 여기서는
(1) 자연스러워 통과해야 하는 문장 (2) 실제로 틀려서 실격이어야 하는 문장을 함께 고정한다.
문장은 사람이 읽어 판단한 예시이며 모집단 추정이 아니다.

알려진 한계는 expectedFailure로 남긴다 — 고쳐지면 "unexpected success"로 알려 준다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_grammar as G
import grammar_bank as B

_PATS = {p.id: p for c in B.CATEGORIES for p in c["patterns"]}


def verdict(pid: str, sentence: str) -> str:
    """'누락' | '구조불합격' | '통과' — validate_passage의 문형별 판정과 같은 경로."""
    n = G._normalize(sentence)
    p = _PATS[pid]
    if not G._pattern_used(p, n):
        return "누락"
    check = G._EXTRA_CHECKS.get(pid)
    return "통과" if (check is None or check(n)) else "구조불합격"


class ConjugationToleranceTests(unittest.TestCase):
    """문형 id는 사전형인데 소설체 지문은 과거·부정 활용형으로 쓴다."""

    CASES = [
        ("を禁じ得ない", "彼の死に、悲しみを禁じ得なかった。"),
        ("を禁じ得ない", "涙を禁じ得なかった。"),
        ("てやまない", "感謝してやまなかった。"),
        ("にほかならない", "それは無謀な挑戦にほかならなかった。"),
        ("にかたくない", "彼の苦労は想像にかたくなかった。"),
        ("にたえない", "その光景は見るにたえなかった。"),
        ("に違いない", "彼は来るに違いなかった。"),
        ("きらいがある", "彼には物事を悪く考えるきらいがあった。"),
        ("に決まっている", "このままでは失敗するに決まっていた。"),
        ("まんざら〜でもない", "まんざら悪い気分でもなかった。"),
        ("あながち〜とは言えない", "あながち間違いとは言えなかった。"),
        ("なんとか〜ようにする", "なんとか遅れないようにした。"),
        ("なんとか〜ようにする", "なんとか遅れないようにして出発した。"),
        # 같은 문법 항목의 표기 변형
        ("なりに", "自分なりの努力を続けた。"),
        ("と相まって", "経験とが相まって、確信が宿った。"),
        ("に伴って", "彼の努力にともなって、店は回復した。"),
    ]

    def test_conjugated_forms_are_recognised(self):
        for pid, s in self.CASES:
            with self.subTest(pid=pid, s=s):
                self.assertNotEqual(verdict(pid, s), "누락", s)

    def test_dictionary_forms_still_recognised(self):
        for pid, s in [("を禁じ得ない", "涙を禁じ得ない。"),
                       ("きらいがある", "悪く考えるきらいがある。"),
                       ("に決まっている", "失敗するに決まっている。")]:
            with self.subTest(pid=pid):
                self.assertNotEqual(verdict(pid, s), "누락")

    def test_polite_forms_stay_rejected(self):
        """정중체(です・ます)는 문체 고정(규칙 7)을 어긴 것 — 활용형 허용이 이를 가리면 안 된다."""
        for pid, s in [("を禁じ得ない", "涙を禁じ得ませんでした。"),
                       ("きらいがある", "悪く考えるきらいがありました。"),
                       ("に決まっている", "失敗するに決まっていました。")]:
            with self.subTest(pid=pid):
                self.assertEqual(verdict(pid, s), "누락", s)

    def test_unrelated_text_is_not_matched(self):
        for pid, s in [("を禁じ得ない", "その行為は法律で禁じられていない。"),
                       ("に決まっている", "試合の日程が決まっている。"),
                       ("にほかならない", "ほかの人に比べてならない話ではない。"),
                       ("きらいがある", "嫌いがある人と会うのを避けた。")]:
            with self.subTest(pid=pid):
                self.assertEqual(verdict(pid, s), "누락", s)


class StructuralBatteryTests(unittest.TestCase):
    """어휘 목록 기반 구조 검사의 양방향 시험."""

    NATURAL = [
        ("あげく", "さんざん悩んだあげく、結局会社を辞めてしまった。"),
        ("あげく", "言い争ったあげく、彼は怒って帰ってしまった。"),
        ("あげく", "長時間待たされたあげく、断られた。"),
        ("ばかりに", "彼を信じたばかりに、大損をした。"),
        ("ばかりに", "油断したばかりに、事故を起こしてしまった。"),
        ("ばかりに", "準備を怠ったばかりに、試験に落ちた。"),
        ("ばかりに", "個人的な事情を優先しすぎたばかりに、全体の予定に大きな遅れが生じた。"),
        ("を禁じ得ない", "事件の経緯に疑問を禁じ得ない。"),
        ("を禁じ得ない", "被害者に同情を禁じ得ない。"),
        ("を禁じ得ない", "達成感と安堵の念を禁じ得ない。"),
        ("を禁じ得ない", "無力感と悔しさを禁じ得ない。"),
        ("を禁じ得ない", "涙を禁じ得なかった。"),
        ("にかたくない", "彼の苦労は予想に難くない。"),
        ("にかたくない", "彼の苦労は想像にかたくない。"),
        ("いかんによって", "結果のいかんによっては、計画を変更する。"),
        ("いかんによって", "結果のいかんによって、対応が異なる。"),
        ("ないことには", "実際に会ってみないことには、彼の人柄は分からない。"),
        ("なくしては", "彼の協力なくしては、この計画は成功しなかっただろう。"),
        ("くせに", "子供のくせに、生意気なことを言う。"),
        ("きらいがある", "彼は物事を悪く考えるきらいがある。"),
    ]

    UNNATURAL = [
        ("あげく", "さんざん悩んだあげく、見事に成功した。"),
        ("ばかりに", "彼を信じたばかりに、成功を収めた。"),
        ("を禁じ得ない", "彼の決断を禁じ得ない。"),
        ("くせに", "彼は若いくせに、立派に成長した。"),
        ("ないことには", "実際に会ってみないことには、彼はとても親切な人だ。"),
        ("なくしては", "彼の協力なくしては、この計画は大成功を収めた。"),
        ("いかんによって", "結果のいかんによって、私は部屋で本を読んだ。"),
        ("にかたくない", "彼の話は面白いにかたくない。"),
        ("きらいがある", "彼は物事を悪く考えつつあるきらいがある。"),
    ]

    def test_natural_sentences_pass(self):
        for pid, s in self.NATURAL:
            with self.subTest(pid=pid, s=s):
                self.assertEqual(verdict(pid, s), "통과", s)

    def test_unnatural_sentences_are_rejected(self):
        for pid, s in self.UNNATURAL:
            with self.subTest(pid=pid, s=s):
                self.assertNotEqual(verdict(pid, s), "통과", s)

    @unittest.expectedFailure
    def test_known_false_reject_kuseni_without_critical_word(self):
        # 알려진 한계: くせに는 주변 20/30자에 비판어가 없으면 자연스러워도 탈락한다.
        self.assertEqual(verdict("くせに", "知っているくせに、教えてくれなかった。"), "통과")

    @unittest.expectedFailure
    def test_known_false_accept_ageku_with_generic_word(self):
        # 알려진 한계: 결과 어휘 「結局」가 목록에 있어 긍정적 결과도 통과한다(단어 탐색 방식의 한계).
        self.assertNotEqual(verdict("あげく", "さんざん悩んだあげく、結局、見事に成功した。"), "통과")


if __name__ == "__main__":
    unittest.main()
