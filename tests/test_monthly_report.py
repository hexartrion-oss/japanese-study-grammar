"""monthly_report.py 테스트.

get_grammar.py의 파이프라인 테스트(test_pipeline.py)와 관심사가 달라서
파일을 분리했다 — 여기는 "이력 dict를 숫자로 집계하고 메일로 보내는지"만
본다, 지문 생성·검증은 전혀 건드리지 않는다.

실행: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monthly_report as MR
import ports
from tests.fakes import make_deps


class AggregateStatsTests(unittest.TestCase):
    """집계 로직만 본다 — 파일 입출력도 메일도 전혀 안 거친다."""

    def test_counts_categories_and_filters_weekday_by_real_match(self):
        # 2026-09-07(월)은 부사의 배정 요일(0=월)과 일치 → 요일 통계에 포함.
        # 2026-09-08(화)은 부사인데 화요일 — FORCE_CATEGORY류 수동 실행 재현,
        # 요일 통계에서는 빠져야 한다(카테고리 통계에는 그대로 포함).
        used_history = {"runs": [
            {"date": "2026-09-07", "category": "부사", "patterns": ["a"]},
            {"date": "2026-09-08", "category": "부사", "patterns": ["a"]},
        ]}
        # 2026-09-09(수)는 강조·역접의 배정 요일(2=수)과 일치.
        failure_history = {"runs": [
            {"date": "2026-09-09", "category": "강조·역접", "failures": [
                {"pattern": "あげく", "reason": "문형 누락: あげく"},
                {"pattern": "さえ", "reason": "문형 누락: あげく"},
            ]},
        ]}

        stats = MR.aggregate_stats(failure_history, used_history)

        self.assertEqual(stats.category_totals,
                         {"부사": {"fail": 0, "total": 2},
                          "강조·역접": {"fail": 1, "total": 1}})
        self.assertEqual(stats.weekday_totals["월"], {"fail": 0, "total": 1})
        self.assertEqual(stats.weekday_totals["화"], {"fail": 0, "total": 0},
                         "화요일에 강제 실행된 부사는 요일 통계에서 빠져야 한다")
        self.assertEqual(stats.weekday_totals["수"], {"fail": 1, "total": 1})
        self.assertEqual(stats.total_runs, 3)
        self.assertEqual(stats.total_failures, 1)

    def test_reason_breakdown_dedupes_by_day_not_by_attempt(self):
        """한 번 실패한 날 같은 사유가 여러 시도(문형 수만큼)에 걸쳐 중복
        기록돼 있어도, reason_breakdown은 그 날을 한 번만 센다."""
        failure_history = {"runs": [
            {"date": "2026-09-09", "category": "강조·역접", "failures": [
                {"pattern": "あげく", "reason": "문형 누락: さえ"},
                {"pattern": "こそ", "reason": "문형 누락: さえ"},
                {"pattern": "すら", "reason": "문장 수 17개 — 범위(10~15) 벗어남"},
            ]},
        ]}
        stats = MR.aggregate_stats(failure_history, {"runs": []})
        self.assertEqual(stats.reason_breakdown,
                         {"문형 누락": 1, "문장 수 범위 이탈": 1})

    def test_top_patterns_dedupes_per_day(self):
        """같은 문형이 하루 안에서 4번 시도 내내 실패에 관여해도 그 날은
        1회로만 센다(get_grammar.find_repeat_offenders와 같은 규칙)."""
        failure_history = {"runs": [
            {"date": "2026-09-09", "category": "강조·역접", "failures": [
                {"pattern": "あげく", "reason": "r1"},
                {"pattern": "あげく", "reason": "r2"},
            ]},
            {"date": "2026-09-16", "category": "강조·역접", "failures": [
                {"pattern": "あげく", "reason": "r3"},
            ]},
        ]}
        stats = MR.aggregate_stats(failure_history, {"runs": []})
        self.assertEqual(dict(stats.top_patterns), {"あげく": 2})

    def test_empty_history_produces_zeroed_stats(self):
        stats = MR.aggregate_stats({"runs": []}, {"runs": []})
        self.assertEqual(stats.total_runs, 0)
        self.assertEqual(stats.total_failures, 0)
        self.assertEqual(stats.category_totals, {})
        self.assertEqual(stats.top_patterns, [])


class ReasonTypeTests(unittest.TestCase):
    def test_known_prefixes_classified(self):
        self.assertEqual(MR._reason_type("문형 누락: あげく"), "문형 누락")
        self.assertEqual(MR._reason_type("구조 조건 미충족: あげく"), "구조 조건 미충족")
        self.assertEqual(MR._reason_type("자연스러움 판정 실패: までも"), "자연스러움 판정 실패")
        self.assertEqual(MR._reason_type("문장 수 21개 — 범위(10~20) 벗어남"),
                         "문장 수 범위 이탈")
        self.assertEqual(MR._reason_type("Gemini 출력 파싱 실패(--- 구분자 없음)"),
                         "출력 파싱 실패")

    def test_unknown_reason_falls_back_to_etc(self):
        self.assertEqual(MR._reason_type("알 수 없는 새 사유"), "기타")


class SendMonthlyReportTests(unittest.TestCase):
    """조립부(send_monthly_report) — deps를 가짜로 채워 메일 발송까지
    네트워크 없이 끝까지 돌린다."""

    def test_sends_mail_with_chart_attachment(self):
        store_initial = {
            "used_history": {"runs": [
                {"date": "2026-09-07", "category": "부사", "patterns": ["a"]},
            ]},
            "failure_history": {"runs": [
                {"date": "2026-09-09", "category": "강조·역접", "failures": [
                    {"pattern": "あげく", "reason": "문형 누락: あげく"},
                ]},
            ]},
        }
        deps, fakes = make_deps(store_initial=store_initial)

        with tempfile.TemporaryDirectory() as tmp:
            MR.send_monthly_report(deps, datetime.date(2026, 10, 1), tmp)

            self.assertEqual(len(fakes["mailer"].sent), 1)
            sent = fakes["mailer"].sent[0]
            self.assertTrue(sent["subject"].startswith("[운영 알림] 월간 실패 통계 리포트"))
            self.assertTrue(sent["attachment_path"])
            chart_file = Path(sent["attachment_path"])
            self.assertTrue(chart_file.exists())
            self.assertGreater(chart_file.stat().st_size, 0)
        self.assertTrue(fakes["log"].has("발송 완료"))

    def test_skips_without_mail_credentials(self):
        deps, fakes = make_deps(
            secrets=ports.Secrets(gmail_address="", gmail_app_password="",
                                  gemini_api_key="", email_recipients=""))
        with tempfile.TemporaryDirectory() as tmp:
            MR.send_monthly_report(deps, datetime.date(2026, 10, 1), tmp)
        self.assertEqual(fakes["mailer"].sent, [])
        self.assertTrue(fakes["log"].has("인증 정보 없음"))

    def test_skips_with_no_accumulated_history(self):
        deps, fakes = make_deps(store_initial={
            "used_history": {"runs": []}, "failure_history": {"runs": []},
        })
        with tempfile.TemporaryDirectory() as tmp:
            MR.send_monthly_report(deps, datetime.date(2026, 10, 1), tmp)
        self.assertEqual(fakes["mailer"].sent, [])
        self.assertTrue(fakes["log"].has("누적 데이터 없음"))



# ── 용법·판정·증거 통계 (2026-10-03 계획 P3) ──────────────────
class AggregateUsageTests(unittest.TestCase):
    RUNS = [
        {"date": "2026-10-05", "result": "passed", "patterns": ["a", "b"], "rules_version": "v1",
         "attempts": [{"judge_status": "call_failed", "outcome": "failed"},
                      {"judge_status": "call_failed", "outcome": "passed"}],
         "final_judge_status": "call_failed", "judge_summary": {"call_failed": 2}},
        {"date": "2026-10-06", "result": "passed", "patterns": ["a", "c"], "rules_version": "v2",
         "attempts": [{"judge_status": "ran", "outcome": "passed", "evidence": [
             {"verdict": "natural", "status": "ok", "n_qualified": 1},
             {"verdict": "unavailable", "status": "quota", "n_qualified": 0}]}],
         "final_judge_status": "ran", "judge_summary": {"ran": 1}},
        {"date": "2026-10-07", "result": "failed", "patterns": ["d"], "rules_version": "v2",
         "attempts": [{"judge_status": None, "outcome": "failed"}],
         "final_judge_status": None, "judge_summary": {}},
    ]
    CASES = [{"status": "candidate", "direction": "judge_ng→web_ok", "pattern": "a",
              "sentence": "s", "hosts": ["nii.ac.jp"], "date": "2026-10-06"},
             {"status": "confirmed", "direction": "code_ng→web_ok", "pattern": "b",
              "sentence": "s", "hosts": [], "date": "2026-10-06"}]

    def test_counts(self):
        u = MR.aggregate_usage_stats(self.RUNS, self.CASES)
        self.assertEqual((u.runs_total, u.runs_passed, u.runs_failed), (3, 2, 1))
        self.assertEqual(u.final_judge, {"call_failed": 1, "ran": 1})
        self.assertEqual(u.judge_attempts, {"call_failed": 2, "ran": 1})
        self.assertEqual(u.evidence_verdicts, {"natural": 1, "unavailable": 1})
        self.assertEqual(u.evidence_status, {"ok": 1, "quota": 1})
        self.assertEqual(dict(u.top_patterns)["a"], 2)
        self.assertEqual(u.overturned_status, {"candidate": 1, "confirmed": 1})
        self.assertEqual(u.rules_versions, {"v1": 1, "v2": 2})

    def test_empty(self):
        u = MR.aggregate_usage_stats([], [])
        self.assertEqual(u.runs_total, 0)
        self.assertEqual(u.final_judge, {})


class UsageEmailTests(unittest.TestCase):
    def test_email_unchanged_when_no_usage_stats(self):
        stats = MR.aggregate_stats({"runs": []}, {"runs": []})
        subj1, html1 = MR.build_report_email(stats, datetime.date(2026, 11, 1))
        subj2, html2 = MR.build_report_email(stats, datetime.date(2026, 11, 1), usage_stats=None)
        self.assertEqual((subj1, html1), (subj2, html2))
        self.assertNotIn("판정 가동률", html1)

    def test_email_has_judge_uptime_and_review_list(self):
        stats = MR.aggregate_stats({"runs": []}, {"runs": []})
        u = MR.aggregate_usage_stats(AggregateUsageTests.RUNS, AggregateUsageTests.CASES)
        _, html = MR.build_report_email(stats, datetime.date(2026, 11, 1), usage_stats=u)
        self.assertIn("판정 가동률", html)
        self.assertIn("1/2", html)                 # 통과한 실행 2건 중 판정이 실제로 돈 것 1건
        self.assertIn("규칙 버전", html)
        self.assertIn("검토 대기", html)           # 사람 확인용 후보 목록


class SendWithUsageTests(unittest.TestCase):
    def test_usage_section_and_chart_when_reader_present(self):
        from usage_repository import JsonUsageRepository
        deps, fakes = make_deps(store_initial={
            "used_history": {"runs": [{"date": "2026-10-06", "category": "부사", "patterns": ["a"]}]},
            "failure_history": {"runs": []},
            "usage_log_2026-10": {"runs": AggregateUsageTests.RUNS,
                                  "overturned": AggregateUsageTests.CASES}})
        repo = JsonUsageRepository(fakes["store"], first_month="2026-10",
                                   today_fn=lambda: datetime.date(2026, 11, 1))
        import dataclasses
        deps = dataclasses.replace(deps, usage_reader=repo)
        with tempfile.TemporaryDirectory() as tmp:
            MR.send_monthly_report(deps, datetime.date(2026, 11, 1), tmp)
            sent = fakes["mailer"].sent[0]
            self.assertIn("판정 가동률", sent["html"])
            self.assertGreater(Path(sent["attachment_path"]).stat().st_size, 0)

    def test_without_reader_report_is_the_old_one(self):
        deps, fakes = make_deps(store_initial={
            "used_history": {"runs": [{"date": "2026-10-06", "category": "부사", "patterns": ["a"]}]},
            "failure_history": {"runs": []}})
        with tempfile.TemporaryDirectory() as tmp:
            MR.send_monthly_report(deps, datetime.date(2026, 11, 1), tmp)
        self.assertNotIn("판정 가동률", fakes["mailer"].sent[0]["html"])


if __name__ == "__main__":
    unittest.main()
