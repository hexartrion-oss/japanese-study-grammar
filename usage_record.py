"""용법 기록 한 건을 조립하는 순수 함수 (파일·네트워크를 모른다).

get_grammar.py는 이 함수에 값을 넘기고 결과를 `deps.usage.record_run`에 전달하기만 한다.
"""

from __future__ import annotations

SCHEMA_VERSION = 1


def build_run_record(*, date: str, category: str, run_id: str, commit_sha: str, manual: bool,
                     rules_version: str, evidence_mode: str, judge_model: str, result: str,
                     topic: str | None, passage: str | None, patterns: list, usages: list,
                     attempts: list) -> dict:
    """실행 1회의 기록. 판정 상태(ran/call_failed/parse_*)를 집계해 가동률을 사후에 셀 수 있게 한다.

    passage/topic은 **통과한 경우에만** 넣는다 — 실패 시도의 원문은 실패 이력(failure_history)과
    실행 로그에 이미 남고, 이 기록의 목적은 통과 지문의 보존과 통계다.
    """
    judge_summary: dict = {}
    for a in attempts:
        st = a.get("judge_status")
        if st:
            judge_summary[st] = judge_summary.get(st, 0) + 1
    passed = [a for a in attempts if a.get("outcome") == "passed"]
    record = {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "category": category,
        "result": result,
        "run_id": run_id,
        "commit_sha": commit_sha,
        "manual": manual,
        "rules_version": rules_version,
        "evidence_mode": evidence_mode,
        "judge_model": judge_model,
        "patterns": list(patterns),
        "attempts_count": len(attempts),
        "judge_summary": judge_summary,
        "final_judge_status": passed[-1].get("judge_status") if passed else None,
        "attempts": attempts,
    }
    if result == "passed" and passage:
        record["topic"] = topic
        record["passage"] = passage
        record["usages"] = usages
    return record
