"""JsonUsageRepository — 월별 샤드·멱등 기록·사람 검토 파일 합성."""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import InMemoryStore
from usage_repository import JsonUsageRepository

D = datetime.date


def _repo(store=None, today=D(2026, 11, 5)):
    store = store or InMemoryStore()
    return JsonUsageRepository(store, first_month="2026-10", today_fn=lambda: today), store


def _run(date, run_id="1", **kw):
    return {"date": date, "run_id": run_id, "category": "부사", "result": "passed", **kw}


class ShardTests(unittest.TestCase):
    def test_shard_name_is_by_month(self):
        repo, _ = _repo()
        self.assertEqual(repo.shard_name(D(2026, 10, 31)), "usage_log_2026-10")
        self.assertEqual(repo.shard_name(D(2026, 11, 1)), "usage_log_2026-11")

    def test_records_go_to_their_own_month_and_are_read_across_months(self):
        repo, store = _repo()
        repo.record_run(_run("2026-10-30"))
        repo.record_run(_run("2026-11-02"))
        self.assertIn("usage_log_2026-10", store.write_log)
        self.assertIn("usage_log_2026-11", store.write_log)
        got = repo.runs_between(D(2026, 10, 1), D(2026, 11, 30))
        self.assertEqual([r["date"] for r in got], ["2026-10-30", "2026-11-02"])
        self.assertEqual(len(repo.runs_between(D(2026, 11, 1), D(2026, 11, 30))), 1)

    def test_missing_or_corrupt_shard_reads_as_empty(self):
        repo, _ = _repo(InMemoryStore({"usage_log_2026-10": {"unexpected": 1}}))
        self.assertEqual(repo.runs_between(D(2026, 10, 1), D(2026, 10, 31)), [])


class IdempotencyTests(unittest.TestCase):
    def test_same_date_and_run_id_overwrites(self):
        repo, _ = _repo()
        repo.record_run(_run("2026-10-05", run_id="9", result="failed"))
        repo.record_run(_run("2026-10-05", run_id="9", result="passed"))
        got = repo.runs_between(D(2026, 10, 5), D(2026, 10, 5))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["result"], "passed")

    def test_manual_rerun_same_day_keeps_both(self):
        repo, _ = _repo()
        repo.record_run(_run("2026-10-05", run_id="9"))
        repo.record_run(_run("2026-10-05", run_id="10", manual=True))
        self.assertEqual(len(repo.runs_between(D(2026, 10, 5), D(2026, 10, 5))), 2)

    def test_write_failure_returns_false_instead_of_raising(self):
        class Boom(InMemoryStore):
            def write(self, name, data):
                raise OSError("disk full")
        repo, _ = _repo(Boom())
        self.assertFalse(repo.record_run(_run("2026-10-05")))


class OverturnedAndReviewTests(unittest.TestCase):
    CASE = {"date": "2026-10-06", "pattern": "あげく", "direction": "judge_ng→web_ok",
            "sentence": "言い争ったあげく、彼は怒って帰ってしまった。"}

    def test_add_assigns_id_and_dedupes(self):
        repo, _ = _repo()
        self.assertTrue(repo.add_overturned(dict(self.CASE)))
        self.assertTrue(repo.add_overturned(dict(self.CASE)))   # 같은 사례 재기록 — 중복 없음
        got = repo.overturned_between(D(2026, 10, 1), D(2026, 10, 31))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], "candidate")

    def test_human_review_file_promotes_and_vetoes(self):
        repo, store = _repo()
        repo.add_overturned(dict(self.CASE))
        cid = repo.overturned_between(D(2026, 10, 1), D(2026, 10, 31))[0]["id"]
        self.assertEqual(repo.confirmed_cases("あげく"), [])      # 자동 승격 없음
        store.write("usage_review", {"confirmed": [cid], "vetoed": []})
        self.assertEqual([c["id"] for c in repo.confirmed_cases("あげく")], [cid])
        store.write("usage_review", {"confirmed": [cid], "vetoed": [cid]})
        self.assertEqual(repo.confirmed_cases("あげく"), [])      # veto가 우선
        self.assertEqual(repo.overturned_between(D(2026, 10, 1), D(2026, 10, 31))[0]["status"], "vetoed")

    def test_only_judge_ng_to_web_ok_direction_is_usable_as_confirmed_example(self):
        repo, store = _repo()
        other = dict(self.CASE, direction="code_ng→web_ok", pattern="ばかりに")
        repo.add_overturned(other)
        cid = repo.overturned_between(D(2026, 10, 1), D(2026, 10, 31))[0]["id"]
        store.write("usage_review", {"confirmed": [cid]})
        self.assertEqual(repo.confirmed_cases("ばかりに"), [])


if __name__ == "__main__":
    unittest.main()
