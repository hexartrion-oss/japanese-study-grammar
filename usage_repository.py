"""용법·판정·증거 기록 저장소 (Repository 패턴 = 데이터 접근 계층).

GitHub 리포가 아니라 기존 `Store`(논리 이름 → JSON 파일)를 그대로 쓴다. 월별 샤드
`usage_log_YYYY-MM`에 누적하고(자르지 않음 — 기존 이력은 history_keep_runs로 잘리지만 이쪽은
통계용이다), 사람이 직접 고치는 검토 파일 `usage_review`는 봇이 쓰지 않는다.
사람 편집과 봇의 일일 커밋이 같은 파일을 건드려 충돌하는 것을 피하려는 분리다.

기록 실패는 발송을 막지 않는다: 모든 쓰기는 예외 대신 False를 돌려준다.
"""

from __future__ import annotations

import datetime
from typing import Callable

_SHARD_DEFAULT = {"runs": [], "overturned": []}
USABLE_DIRECTION = "judge_ng→web_ok"   # 생성 힌트로 쓸 수 있는 방향(판정은 어색, 웹은 자연)


class JsonUsageRepository:
    """UsageWriter + UsageReader 구현."""

    def __init__(self, store, first_month: str = "2026-10",
                 today_fn: Callable[[], datetime.date] = datetime.date.today,
                 review_name: str = "usage_review", log: Callable[[str], None] | None = None):
        self.store = store
        self.first_month = first_month
        self.today_fn = today_fn
        self.review_name = review_name
        self._log = log or (lambda _msg: None)

    # ── 샤드 ──
    @staticmethod
    def shard_name(date: datetime.date) -> str:
        return f"usage_log_{date.year:04d}-{date.month:02d}"

    def _read_shard(self, name: str) -> dict:
        data = self.store.read(name, _SHARD_DEFAULT)
        if not isinstance(data, dict):
            return {"runs": [], "overturned": []}
        data.setdefault("runs", [])
        data.setdefault("overturned", [])
        if not isinstance(data["runs"], list) or not isinstance(data["overturned"], list):
            return {"runs": [], "overturned": []}
        return data

    def _months(self, start: datetime.date, end: datetime.date):
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            yield datetime.date(y, m, 1)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    # ── 쓰기 (UsageWriter) ──
    def record_run(self, record: dict) -> bool:
        """(date, run_id)가 같으면 덮어쓴다 — 워크플로 재시도가 같은 날 기록을 부풀리지 않게."""
        try:
            date = datetime.date.fromisoformat(record["date"])
            name = self.shard_name(date)
            shard = self._read_shard(name)
            key = (record["date"], str(record.get("run_id", "")))
            shard["runs"] = [r for r in shard["runs"]
                             if (r.get("date"), str(r.get("run_id", ""))) != key]
            shard["runs"].append(record)
            self.store.write(name, shard)
            return True
        except Exception as e:  # noqa: BLE001 — 기록 실패는 발송을 막지 않는다
            self._log(f"[용법기록] 실행 기록 실패: {e}")
            return False

    def add_overturned(self, case: dict) -> bool:
        try:
            date = datetime.date.fromisoformat(case["date"])
            name = self.shard_name(date)
            shard = self._read_shard(name)
            case = dict(case)
            case.setdefault("id", f"{case['date']}-{case['pattern']}-{case['direction']}")
            case.setdefault("status", "candidate")
            shard["overturned"] = [c for c in shard["overturned"] if c.get("id") != case["id"]]
            shard["overturned"].append(case)
            self.store.write(name, shard)
            return True
        except Exception as e:  # noqa: BLE001
            self._log(f"[용법기록] 이견 사례 기록 실패: {e}")
            return False

    # ── 읽기 (UsageReader) ──
    def runs_between(self, start: datetime.date, end: datetime.date) -> list:
        out = []
        for month in self._months(start, end):
            for r in self._read_shard(self.shard_name(month))["runs"]:
                if start.isoformat() <= str(r.get("date", "")) <= end.isoformat():
                    out.append(r)
        return sorted(out, key=lambda r: (r.get("date", ""), str(r.get("run_id", ""))))

    def _review(self) -> tuple[set, set]:
        data = self.store.read(self.review_name, {"confirmed": [], "vetoed": []})
        if not isinstance(data, dict):
            return set(), set()
        return set(data.get("confirmed", []) or []), set(data.get("vetoed", []) or [])

    def overturned_between(self, start: datetime.date, end: datetime.date) -> list:
        confirmed, vetoed = self._review()
        out = []
        for month in self._months(start, end):
            for c in self._read_shard(self.shard_name(month))["overturned"]:
                if not (start.isoformat() <= str(c.get("date", "")) <= end.isoformat()):
                    continue
                c = dict(c)
                cid = c.get("id")
                c["status"] = "vetoed" if cid in vetoed else ("confirmed" if cid in confirmed else "candidate")
                out.append(c)
        return sorted(out, key=lambda c: (c.get("date", ""), c.get("id", "")))

    def confirmed_cases(self, pattern_id: str) -> list:
        """사람이 확인(confirmed)했고 veto되지 않은 `judge_ng→web_ok` 사례. 자동 승격은 없다."""
        first = datetime.date.fromisoformat(self.first_month + "-01")
        cases = self.overturned_between(first, self.today_fn())
        return [c for c in cases
                if c.get("pattern") == pattern_id
                and c.get("direction") == USABLE_DIRECTION
                and c["status"] == "confirmed"]
