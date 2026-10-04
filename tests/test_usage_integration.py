"""판정 상태 기록(P0)·증거 shadow(P2)·용법 기록 조립 — generate_passage/main 수준 통합 테스트."""

from __future__ import annotations

import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_grammar as G
import grammar_bank as GB
import ports
import usage_record as UR
from tests.fakes import FakeEvidence, make_deps, passage_text
from tests.test_pipeline import _full_passage

ALL_OK = "\n".join(f"pat{i}|true|" for i in range(10))


def _category(patterns=None) -> dict:
    return {"key": "테스트", "weekday": 0, "level_tag": "N3",
            "patterns": patterns or [GB.Pattern(f"pat{i}") for i in range(10)]}


def _good(n_from=0):
    return passage_text([f"pat{i}" for i in range(n_from, n_from + 5)], 12)


class JudgeStatusTests(unittest.TestCase):
    def _run(self, judge_pass):
        deps, _ = make_deps(llm_responses=[_good()], judge_pass=judge_pass)
        sink: list = []
        G.generate_passage(deps, _category(), {}, attempt_sink=sink)
        return sink

    def test_ran(self):
        sink = self._run(ALL_OK)
        self.assertEqual(sink[-1]["judge_status"], "ran")
        self.assertEqual(sink[-1]["outcome"], "passed")

    def test_call_failed_is_recorded_not_silent(self):
        sink = self._run("")
        self.assertEqual(sink[-1]["judge_status"], "call_failed")
        self.assertEqual(sink[-1]["outcome"], "passed")      # fail-open은 유지, 다만 기록된다

    def test_partial_and_unparseable_output(self):
        self.assertEqual(self._run("pat0|true|")[-1]["judge_status"], "parse_partial")
        self.assertEqual(self._run("무관한 문장")[-1]["judge_status"], "parse_failed")

    def test_judge_ng_reason_is_kept(self):
        deps, _ = make_deps(llm_responses=[_good()] * 4,
                            judge_pass="pat0|false|後件が肯定的\n" + "\n".join(f"pat{i}|true|" for i in range(1, 10)))
        sink: list = []
        topic, passage, *_ = G.generate_passage(deps, _category(), {}, attempt_sink=sink)
        self.assertIsNone(passage)
        self.assertEqual(sink[0]["outcome"], "failed")
        self.assertEqual(sink[0]["judge_failed"], ["pat0"])
        self.assertEqual(sink[0]["judge_notes"]["pat0"], "後件が肯定的")

    def test_sink_is_optional(self):
        deps, _ = make_deps(llm_responses=[_good()], judge_pass=ALL_OK)
        out = G.generate_passage(deps, _category(), {})
        self.assertEqual(len(out), 5)                          # 기존 반환 형태 불변


class EvidenceShadowTests(unittest.TestCase):
    JUDGE_NG = "pat0|false|理由\n" + "\n".join(f"pat{i}|true|" for i in range(1, 10))

    def _run(self, *, mode="shadow", evidence=None, cap=3, judge=None):
        ev = evidence or FakeEvidence()
        deps, fakes = make_deps(
            llm_responses=[_good()] * 4, judge_pass=judge or self.JUDGE_NG,
            settings=ports.Settings(evidence_mode=mode, evidence_daily_cap=cap), evidence=ev)
        sink: list = []
        out = G.generate_passage(deps, _category(), {}, attempt_sink=sink)
        return out, sink, ev, fakes

    def test_off_never_calls_evidence(self):
        _, sink, ev, _ = self._run(mode="off")
        self.assertEqual(ev.calls, [])
        self.assertTrue(all(a["evidence"] == [] for a in sink))

    def test_shadow_records_but_does_not_change_the_decision(self):
        out, sink, ev, _ = self._run()
        self.assertIsNone(out[1])                      # 웹이 「自然」이라 해도 판정 NG는 번복되지 않는다
        self.assertEqual(sink[0]["evidence"][0]["verdict"], "natural")
        self.assertEqual(sink[0]["evidence"][0]["source"], "judge")
        self.assertEqual(sink[0]["evidence"][0]["pattern"], "pat0")

    def test_daily_cap_limits_calls(self):
        _, _, ev, _ = self._run(cap=2)
        self.assertEqual(len(ev.calls), 2)

    def test_quota_response_stops_further_calls(self):
        ev = FakeEvidence([ports.EvidenceResult(status="quota")])
        _, sink, ev, fakes = self._run(evidence=ev)
        self.assertEqual(len(ev.calls), 1)
        self.assertEqual(sink[0]["evidence"][0]["verdict"], "unavailable")
        self.assertTrue(fakes["log"].has("429") or fakes["log"].has("할당량"))

    def test_code_structural_failure_is_also_checked_with_code_source(self):
        pats = [GB.Pattern("あげく")] + [GB.Pattern(f"pat{i}") for i in range(1, 10)]
        bad = passage_text(["さんざん悩んだあげく、見事に成功した。", "pat1", "pat2", "pat3", "pat4"], 12)
        ok = _good(5)
        deps, _ = make_deps(llm_responses=[bad, bad, ok], judge_pass=ALL_OK,
                            settings=ports.Settings(evidence_mode="shadow"),
                            evidence=FakeEvidence())
        sink: list = []
        G.generate_passage(deps, _category(pats), {}, attempt_sink=sink)
        code_ev = [e for a in sink for e in a["evidence"] if e["source"] == "code"]
        self.assertTrue(code_ev)
        self.assertEqual(code_ev[0]["pattern"], "あげく")
        self.assertIn("あげく", code_ev[0]["sentence"])

    def test_enabled_without_adapter_is_a_noop(self):
        deps, _ = make_deps(llm_responses=[_good()] * 4, judge_pass=self.JUDGE_NG,
                            settings=ports.Settings(evidence_mode="shadow"))
        sink: list = []
        G.generate_passage(deps, _category(), {}, attempt_sink=sink)
        self.assertTrue(all(a["evidence"] == [] for a in sink))


class RunRecordTests(unittest.TestCase):
    def test_build_run_record_summarises_judge_status(self):
        rec = UR.build_run_record(
            date="2026-10-05", category="부사", run_id="7", commit_sha="abc", manual=False,
            rules_version="v", evidence_mode="off", judge_model="m", result="passed",
            topic="t", passage="本文。", patterns=["a"], usages=[{"pattern": "a", "sentence": "本文。"}],
            attempts=[{"attempt": 1, "judge_status": "call_failed", "outcome": "failed"},
                      {"attempt": 2, "judge_status": "ran", "outcome": "passed"}])
        self.assertEqual(rec["attempts_count"], 2)
        self.assertEqual(rec["judge_summary"], {"ran": 1, "call_failed": 1})
        self.assertEqual(rec["final_judge_status"], "ran")
        self.assertEqual(rec["schema_version"], UR.SCHEMA_VERSION)

    def test_failed_run_has_no_passage_and_no_final_status(self):
        rec = UR.build_run_record(
            date="2026-10-05", category="부사", run_id="", commit_sha="", manual=True,
            rules_version="v", evidence_mode="off", judge_model="m", result="failed",
            topic=None, passage=None, patterns=["a"], usages=[], attempts=[])
        self.assertNotIn("passage", rec)
        self.assertIsNone(rec["final_judge_status"])
        self.assertTrue(rec["manual"])


class MainUsageTests(unittest.TestCase):
    def test_successful_run_writes_record_and_commits_shard(self):
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")], with_usage=True, run_id="77")
        self.assertTrue(G.main(deps))
        recs = fakes["usage"].runs_between(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["result"], r["run_id"], r["commit_sha"]), ("passed", "77", "deadbeef"))
        self.assertEqual(r["rules_version"], G.RULES_VERSION)
        self.assertEqual(r["attempts_count"], 1)
        self.assertEqual(r["final_judge_status"], "call_failed")
        self.assertIn("passage", r)
        self.assertTrue(r["usages"])
        committed = [p for c in fakes["vcs"].commits for p in c["paths"]]
        self.assertIn("<fake>/usage_log_2026-09.json", committed)

    def test_failed_run_is_recorded_too(self):
        deps, fakes = make_deps(llm_responses=[], with_usage=True, run_id="78")
        self.assertFalse(G.main(deps))
        r = fakes["usage"].runs_between(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))[0]
        self.assertEqual(r["result"], "failed")
        self.assertEqual(r["attempts_count"], deps.settings.max_gen_attempts)
        self.assertNotIn("passage", r)

    def test_without_usage_repository_behaviour_is_unchanged(self):
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")])
        self.assertTrue(G.main(deps))
        self.assertFalse([n for n in fakes["store"].write_log if n.startswith("usage_log")])

    def test_usage_write_failure_does_not_block_sending(self):
        deps, fakes = make_deps(llm_responses=[_full_passage("부사")], with_usage=True)

        def boom(_record):
            raise OSError("disk full")
        fakes["usage"].store.write = lambda name, data: (_ for _ in ()).throw(OSError("x")) \
            if name.startswith("usage_log") else None
        self.assertTrue(G.main(deps))                      # 기록 실패는 발송을 막지 않는다
        self.assertEqual(len(fakes["mailer"].sent), 1)

    def test_evidence_candidate_is_saved_as_overturned_case(self):
        first = GB.CATEGORY_BY_KEY["부사"]["patterns"][0].id
        judge = f"{first}|false|理由"
        deps, fakes = make_deps(
            llm_responses=[_full_passage("부사")], judge_pass=judge, with_usage=True,
            settings=ports.Settings(evidence_mode="shadow"), evidence=FakeEvidence())
        G.main(deps)
        cases = fakes["usage"].overturned_between(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["direction"], "judge_ng→web_ok")
        self.assertEqual(cases[0]["pattern"], first)
        self.assertEqual(cases[0]["status"], "candidate")



class UsageHintTests(unittest.TestCase):
    """생성 프롬프트에 '사람이 확인한 용례'를 제시하는 경로 — 기본 off, 확인된 사례만 사용."""

    def _deps(self, mode="examples", with_case=True, review=True, **settings):
        store_initial = {}
        deps, fakes = make_deps(
            llm_responses=[_good()], judge_pass=ALL_OK, with_usage=True,
            settings=ports.Settings(usage_hint_mode=mode, **settings))
        if with_case:
            fakes["usage"].add_overturned({
                "date": "2026-09-10", "pattern": "pat0", "direction": "judge_ng→web_ok",
                "sentence": "言い争ったあげく、彼は怒って帰ってしまった。"})
            if review:
                cid = fakes["usage"].overturned_between(
                    datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))[0]["id"]
                fakes["store"].write("usage_review", {"confirmed": [cid]})
        return deps, fakes

    def _first_gen_prompt(self, deps, fakes):
        G.generate_passage(deps, _category(), {})
        return [c for c in fakes["llm"].calls if c["model"] is None][0]["prompt"]

    def test_off_by_default_prompt_has_no_example_section(self):
        deps, fakes = self._deps(mode="off")
        self.assertNotIn("参考用例", self._first_gen_prompt(deps, fakes))

    def test_confirmed_example_is_shown_when_enabled(self):
        deps, fakes = self._deps()
        prompt = self._first_gen_prompt(deps, fakes)
        self.assertIn("参考用例", prompt)
        self.assertIn("言い争ったあげく", prompt)
        self.assertIn("真似", prompt)                      # 베끼지 말라는 지시가 같이 간다

    def test_unconfirmed_candidate_is_never_shown(self):
        deps, fakes = self._deps(review=False)
        self.assertNotIn("参考用例", self._first_gen_prompt(deps, fakes))

    def test_hint_count_is_capped(self):
        deps, fakes = self._deps(usage_hint_max=0)
        self.assertNotIn("参考用例", self._first_gen_prompt(deps, fakes))

    def test_judge_prompt_never_receives_examples(self):
        deps, fakes = self._deps()
        G.generate_passage(deps, _category(), {})
        judge_prompts = [c["prompt"] for c in fakes["llm"].calls if c["model"] is not None]
        self.assertTrue(judge_prompts)
        self.assertTrue(all("参考用例" not in p for p in judge_prompts))

    def test_reader_failure_degrades_to_no_hints(self):
        deps, fakes = self._deps()

        class Broken:
            def confirmed_cases(self, pid):
                raise OSError("x")
        import dataclasses
        deps = dataclasses.replace(deps, usage_reader=Broken())
        self.assertNotIn("参考用例", self._first_gen_prompt(deps, fakes))



class RealStoreSmokeTests(unittest.TestCase):
    """실제 JsonFileStore(임시 디렉터리)로 main()을 끝까지 돌려 파일이 유효한 JSON으로 남는지 본다."""

    def test_main_writes_valid_usage_shard_to_disk(self):
        import dataclasses
        import json
        import tempfile

        import adapters
        from usage_repository import JsonUsageRepository
        with tempfile.TemporaryDirectory() as tmp:
            deps, fakes = make_deps(llm_responses=[_full_passage("부사")], run_id="1")
            store = adapters.JsonFileStore(tmp, deps.log)
            repo = JsonUsageRepository(store, first_month="2026-09",
                                       today_fn=lambda: datetime.date(2026, 9, 30))
            deps = dataclasses.replace(deps, store=store, usage=repo, usage_reader=repo)
            self.assertTrue(G.main(deps))
            path = Path(tmp) / "usage_log_2026-09.json"
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["runs"][0]["result"], "passed")
            self.assertIn("passage", data["runs"][0])
            self.assertTrue((Path(tmp) / "used_history.json").exists())   # 기존 이력도 그대로 기록


if __name__ == "__main__":
    unittest.main()
