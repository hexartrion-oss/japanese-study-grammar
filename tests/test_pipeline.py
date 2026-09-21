"""주입 리팩터링이 실제로 하는 일을 증명하는 테스트.

새 기능 검증이 아니라, 지금까지 정규식으로 함수 본문을 exec해서 손으로
확인했던 세 가지 실제 사고를 표준 테스트로 재현한다:

1. 2026-09-15 인용 회차 — 쿨다운 우회가 exclude_ids까지 버려서 재선정이
   직전 실패 조합과 3/5 겹친 것 (PR #16)
2. 2026-09-14 회차 5시간41분 지연 — KST 자정을 넘겨 월요일 회차가
   화요일로 밀린 것 (PR #16)
3. 2026-09-15 인용 4차 지문 — 「素晴らしい」가 문형 らしい로 오탐된 것
   (らしい 오탐 차단 커밋)

이 파일이 실행 가능하다는 사실 자체가 리팩터링의 결과물이다 — 이전에는
이 세 가지를 모두 손으로 정규식을 짜서 함수를 떼어내 확인했다.

실행: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_grammar as G
import grammar_bank as GB
import ports
from tests.fakes import make_deps, passage_text


class SendDateTests(unittest.TestCase):
    """PR #16의 핵심 수정 — 정규 실행은 UTC 날짜, 수동 실행은 KST 날짜."""

    def test_on_time_run_keeps_scheduled_date(self):
        deps, _ = make_deps(
            utc=datetime.datetime(2026, 9, 14, 11, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(G._send_date(deps), datetime.date(2026, 9, 14))

    def test_actual_incident_delay_no_longer_rolls_the_date(self):
        """실측: 2026-09-14 회차가 5시간41분 밀려 16:41 UTC에 떴다. 그 결과
        KST로는 이미 9/15 화요일 새벽이었다(01:41 KST). 수정 전에는 이 시각의
        KST 날짜를 그대로 썼기 때문에 회차가 화요일로 밀렸다."""
        deps, _ = make_deps(
            utc=datetime.datetime(2026, 9, 14, 16, 41, tzinfo=datetime.timezone.utc))
        # 밀렸다면 이 값이 될 뻔했다 — 켜두는 이유는 회귀가 생기면 바로 보이게 하려는 것.
        would_have_rolled = deps.clock.now_kst().date()
        self.assertEqual(would_have_rolled, datetime.date(2026, 9, 15))
        self.assertEqual(G._send_date(deps), datetime.date(2026, 9, 14))

    def test_friday_survives_delay_up_to_thirteen_hours(self):
        """금요일에 지연이 KST 자정을 넘기면 주말 가드에 걸려 발송이 사라진다.
        UTC 기준이면 13시간까지는 안전하다(11:00 UTC 예정 + 13:00 = 자정 UTC)."""
        deps, _ = make_deps(
            utc=datetime.datetime(2026, 9, 18, 11, 0, tzinfo=datetime.timezone.utc))
        deps.clock.advance(hours=12, minutes=59)
        self.assertEqual(G._send_date(deps), datetime.date(2026, 9, 18))
        self.assertEqual(G._send_date(deps).weekday(), 4)  # 금

    def test_manual_run_uses_kst_not_utc(self):
        """수동 실행은 사람이 누른 시각이 의도다. UTC 09-14 23:30은 KST로
        09-15 08:30 — UTC를 썼다면 전날로 어긋났을 구간이다."""
        deps, _ = make_deps(
            utc=datetime.datetime(2026, 9, 14, 23, 30, tzinfo=datetime.timezone.utc),
            manual=True)
        self.assertEqual(G._send_date(deps), datetime.date(2026, 9, 15))


class WeekendGuardTests(unittest.TestCase):
    def test_automatic_run_blocked_on_saturday(self):
        deps, fakes = make_deps(
            utc=datetime.datetime(2026, 9, 19, 11, 0, tzinfo=datetime.timezone.utc))
        result = G.main(deps)
        self.assertTrue(result)  # 주말 스킵은 실패가 아니다
        self.assertTrue(fakes["log"].has("주말"))
        self.assertEqual(fakes["mailer"].sent, [])

    def test_manual_run_allowed_on_saturday(self):
        deps, fakes = make_deps(
            utc=datetime.datetime(2026, 9, 19, 11, 0, tzinfo=datetime.timezone.utc),
            manual=True,
            llm_responses=[_full_passage("부사")],
        )
        G.main(deps)
        self.assertFalse(fakes["log"].has("주말"))
        self.assertEqual(len(fakes["mailer"].sent), 1)


class SelectPatternsExcludeIdsTests(unittest.TestCase):
    """2026-09-15 인용 회차 사고 재현. 쿨다운 15개 제외 + 직전 실패 5개
    제외로 후보가 2개까지 좁아지는 실측 상황에서, 재선정이 직전 조합과
    겹치지 않는지 확인한다."""

    def test_cooldown_bypass_still_excludes_failed_combo(self):
        deps, fakes = make_deps()
        category = GB.CATEGORY_BY_KEY["인용"]
        pool_ids = [p.id for p in category["patterns"]]
        self.assertEqual(len(pool_ids), 17)  # 실측 풀 크기 전제가 깨지면 이 테스트도 재검토해야 함

        history = {"runs": [
            {"date": "2026-09-12", "category": "인용", "patterns": pool_ids[0:5]},
            {"date": "2026-09-13", "category": "인용", "patterns": pool_ids[5:10]},
            {"date": "2026-09-14", "category": "인용", "patterns": pool_ids[10:15]},
        ]}
        failed_combo = {"らしい", "そうだ(様態)", "ということだ", "っぽい", "んじゃないか"}

        picked = G.select_patterns(deps, category, history, exclude_ids=failed_combo)
        picked_ids = {p.id for p in picked}

        self.assertEqual(len(picked), 5)
        self.assertEqual(picked_ids & failed_combo, set(),
                         "재선정이 직전 실패 조합과 겹치면 안 된다 — "
                         "실측 사고에서는 3/5가 겹쳐 そうだ(様態)가 유임됐다")
        self.assertTrue(fakes["log"].has("직전 조합"))

    def test_relaxing_cooldown_without_exclude_ids_still_uses_full_pool(self):
        """exclude_ids 없이 쿨다운만 좁아진 평상시 우회 경로는 그대로
        pool 전체를 쓴다 — 회귀 확인."""
        deps, _ = make_deps()
        category = GB.CATEGORY_BY_KEY["인용"]
        pool_ids = [p.id for p in category["patterns"]]
        history = {"runs": [
            {"date": "2026-09-14", "category": "인용", "patterns": pool_ids[:15]},
        ]}
        picked = G.select_patterns(deps, category, history)
        self.assertEqual(len(picked), 5)


class RashiiPresenceCheckTests(unittest.TestCase):
    """2026-09-15 4차 지문 오탐 재현 — 「素晴らしい」가 문형 らしい로 잡혔다."""

    def test_keiyoushi_tail_is_not_counted_as_the_pattern(self):
        rashii = next(p for p in GB.CATEGORY_BY_KEY["인용"]["patterns"] if p.id == "らしい")
        text = "そう思わせるような素晴らしい経験だった。"
        self.assertTrue(rashii.found_in(text), "리터럴 매칭은 여전히 오탐한다(전제 확인)")
        self.assertFalse(G._pattern_used(rashii, text),
                         "형태소 분석 보정이 素晴らしい를 걸러내야 한다")

    def test_real_suiryou_usage_still_counts(self):
        rashii = next(p for p in GB.CATEGORY_BY_KEY["인용"]["patterns"] if p.id == "らしい")
        text = "とても可愛らしい猫の姿だった。名前らしい。"
        self.assertTrue(G._pattern_used(rashii, text),
                        "오탐 어휘와 정상 용법이 공존해도 정상 용법을 놓치면 안 된다")


class LemmaAwareWordListTests(unittest.TestCase):
    """2026-09-17 비즈니스 회차 사고 재현 — ばかりに 뒤의 부정적 결과어가
    활용형(落ち込んだ)으로 등장하면 사전형(落ち込む) 리터럴 매칭이 놓쳤다.
    _contains_any()가 원문과 활용형 복원 텍스트를 모두 보게 고친 뒤의
    회귀·정착 확인."""

    def test_real_failing_passage_now_passes(self):
        # 실제 9/17 2차 시도 지문 일부
        text = ("私は自分の絵の才能を過信していたばかりに、現実の厳しさに直面し、"
                "ひどく落ち込んだ。友人にとっては慰めの言葉をかけるのが難しい状況"
                "だっただろう。")
        self.assertTrue(G._check_hardship_after(text, "ばかりに"),
                        "活用形(落ち込んだ)도 잡아야 한다")

    def test_vocabulary_gap_also_fixed(self):
        # 실제 9/17 1차 시도 지문 일부 — 迷惑/苦い 자체가 목록에 없던 별건
        text = ("準備を怠ったばかりに、チーム全体に多大な迷惑をかけてしまった"
                "という苦い経験だ。")
        self.assertTrue(G._check_hardship_after(text, "ばかりに"))

    def test_dictionary_form_still_matches(self):
        """회귀 — 활용형 대응을 넣기 전부터 통과하던 사전형 케이스."""
        text = "苦労したあげく、結局失敗に終わった。"
        self.assertTrue(G._check_hardship_after(text, "あげく"))

    def test_positive_outcome_still_rejected(self):
        """오탐 방지 — 부정적 결과어가 진짜 없으면 활용형 매칭을 넣어도
        여전히 실격이어야 한다."""
        text = "努力したばかりに、大きな成功を収めた。"
        self.assertFalse(G._check_hardship_after(text, "ばかりに"))

    def test_other_word_list_checks_also_gained_conjugation_matching(self):
        """같은 구조(창+어휘 목록)를 쓰는 나머지 네 체크도 함께 고쳤다 —
        결과 변화(いかんによって) 뒤에 과거형이 와도 잡혀야 한다."""
        text = "結果は本人の努力いかんによって大きく変わった。"
        self.assertTrue(G._check_result_variation(text, "いかんによって"))


class EndToEndTests(unittest.TestCase):
    """main()을 처음부터 끝까지 네트워크 없이 돌린다."""

    def test_successful_send_updates_history_and_commits(self):
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")])
        ok = G.main(deps)

        self.assertTrue(ok)
        self.assertEqual(len(fakes["mailer"].sent), 1)
        self.assertIn("used_history", fakes["store"].write_log)
        # 그림자 비교 커밋(항상) + 이력 커밋(발송 성공 시) = 2건
        committed_messages = [c["message"] for c in fakes["vcs"].commits]
        self.assertTrue(any("이력" in m and "실패" not in m for m in committed_messages))
        self.assertTrue(any("그림자비교" in m for m in committed_messages))

    def test_manual_run_sends_but_does_not_touch_cooldown_history(self):
        """MANUAL 4장 — 수동 실행으로 쿨다운 이력을 오염시키지 않는다."""
        deps, fakes = make_deps(manual=True, llm_responses=[_full_passage("부사")])
        G.main(deps)
        self.assertEqual(len(fakes["mailer"].sent), 1)
        self.assertNotIn("used_history", fakes["store"].write_log)

    def test_all_attempts_failing_sends_admin_alert_not_reader_mail(self):
        deps, fakes = make_deps(llm_responses=["망가진 응답(구분자 없음)"] * 4)
        ok = G.main(deps)

        self.assertFalse(ok)
        # 운영자 알림과 독자 발송이 같은 Mailer 포트를 쓰므로, "메일이 갔는지"가
        # 아니라 "누구에게 갔는지"로 구분해야 한다.
        self.assertEqual(len(fakes["mailer"].sent), 1)
        self.assertEqual(fakes["mailer"].sent[0]["to"], [deps.secrets.gmail_address])
        self.assertNotIn(deps.secrets.recipient_list()[0],
                         fakes["mailer"].sent[0]["to"])
        self.assertIn("failure_history", fakes["store"].write_log)

    def test_failed_send_does_not_update_history(self):
        """발송이 실패하면(SMTP 등) 이력을 건드리지 않는다 — 안 보낸 문형을
        "사용됨"으로 잘못 기록하지 않기 위해서다."""
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")], mail_ok=False)
        ok = G.main(deps)
        self.assertFalse(ok)
        self.assertNotIn("used_history", fakes["store"].write_log)


class BuildPromptRetryNoteTests(unittest.TestCase):
    """2026-09-21 부사 회차 사고 — ことができました/でもなかった처럼 정중체로
    쓰거나 활용형이 어긋나 재시도해도 같은 실수를 반복했다. 문체 고정(규칙7)과
    실패 피드백(retry_note)이 프롬프트에 실제로 들어가는지 확인한다."""

    def _deps(self):
        deps, _ = make_deps()
        return deps

    def test_rule_seven_always_locks_style(self):
        deps = self._deps()
        patterns = [GB.Pattern("pat0")]
        prompt = G.build_prompt(deps, patterns, "N3")
        self.assertIn("だ・である", prompt)
        self.assertIn("です・ます", prompt)

    def test_no_retry_note_when_missing_is_none(self):
        deps = self._deps()
        patterns = [GB.Pattern("pat0")]
        prompt = G.build_prompt(deps, patterns, "N3", missing=None)
        self.assertNotIn("前回の失敗", prompt)

    def test_retry_note_included_when_missing_given(self):
        deps = self._deps()
        patterns = [GB.Pattern("pat0")]
        prompt = G.build_prompt(deps, patterns, "N3", missing=["ばかりに", "あげく"])
        self.assertIn("前回の失敗", prompt)
        self.assertIn("ばかりに", prompt)
        self.assertIn("あげく", prompt)


class GeneratePassageRetryFeedbackTests(unittest.TestCase):
    """generate_passage()가 실패한 문형 id를 다음 시도의 build_prompt에
    실제로 넘기는지, 그리고 2회 실패 후 문형 조합이 바뀌는 시점에는 직전
    피드백을 버리는지(구 조합 얘기를 새 조합에 섞지 않기 위해) 확인한다."""

    def _category(self, n: int = 10) -> dict:
        return {
            "key": "테스트",
            "weekday": 0,
            "level_tag": "N3",
            "patterns": [GB.Pattern(f"pat{i}") for i in range(n)],
        }

    def test_retry_feedback_flows_to_next_attempt_and_resets_on_reselect(self):
        # 1차: pat0~4 조합, pat4 누락 → 실패
        resp1 = passage_text(["pat0", "pat1", "pat2", "pat3"], 12)
        # 2차: 같은 조합, 여전히 pat4 누락 → 실패 (2연패로 3차에서 조합 교체 유발)
        resp2 = passage_text(["pat0", "pat1", "pat2", "pat3"], 12)
        # 3차: 조합 교체 후 pat5~9, pat9 누락 → 실패
        resp3 = passage_text(["pat5", "pat6", "pat7", "pat8"], 12)
        # 4차: 같은(교체된) 조합, 전부 포함 → 성공
        resp4 = passage_text(["pat5", "pat6", "pat7", "pat8", "pat9"], 12)

        deps, fakes = make_deps(llm_responses=[resp1, resp2, resp3, resp4])
        topic, passage, patterns, attempts_log, shadow_log = G.generate_passage(
            deps, self._category(), history={})

        gen_calls = [c for c in fakes["llm"].calls if c["model"] is None]
        self.assertEqual(len(gen_calls), 4)

        # 1차 프롬프트: 직전 시도가 없으니 재시도 피드백이 없어야 한다
        self.assertNotIn("前回の失敗", gen_calls[0]["prompt"])

        # 2차 프롬프트: 같은 조합으로 재시도하므로 1차 실패(pat4 누락)를 알려줘야 한다
        self.assertIn("前回の失敗", gen_calls[1]["prompt"])
        self.assertIn("pat4", gen_calls[1]["prompt"])

        # 3차 프롬프트: 조합이 바뀌었으니 pat4 얘기를 그대로 들고 오면 안 된다
        self.assertNotIn("前回の失敗", gen_calls[2]["prompt"])

        # 4차 프롬프트: 새 조합(pat5~9)에서도 직전 실패(pat9 누락)를 알려줘야 한다
        self.assertIn("前回の失敗", gen_calls[3]["prompt"])
        self.assertIn("pat9", gen_calls[3]["prompt"])

        # 4차 시도가 최종적으로 성공해서 반환되어야 한다
        self.assertIn("pat9", passage)
        self.assertEqual({p.id for p in patterns}, {f"pat{i}" for i in range(5, 10)})


def _full_passage(category_key: str, sentence_count: int = 12) -> str:
    """지정 카테고리의 순환 선택(요일=월요일 기준) 5개 문형이 전부 들어간
    지문을 조립한다. FakeRng가 pool의 앞 5개를 그대로 고르므로, 그 5개의
    대표 문자열을 그대로 문장으로 쓴다."""
    category = GB.CATEGORY_BY_KEY[category_key]
    patterns = category["patterns"][:5]
    terms = [p.terms[0][0] + "だ。" if len(p.terms[0]) == 1
             else "".join(p.terms[0]) + "だ。" for p in patterns]
    return passage_text(terms, sentence_count)


if __name__ == "__main__":
    unittest.main()
