"""ports.py의 포트를 구현한 가짜(fake)들.

네트워크·SMTP·git·시계를 실제로 건드리지 않고 get_grammar.py의 파이프라인을
끝까지 실행하기 위한 인메모리 구현이다. adapters.py의 실제 구현과 시그니처가
같아야 하며(Protocol이라 상속은 필요 없다), 동작만 테스트용으로 단순화한다.
"""

from __future__ import annotations

import datetime
from typing import Sequence

import ports

from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


class FakeClock:
    """고정된 시각을 돌려준다. advance()로 시간을 흘려보내 지연 시나리오를
    재현한다(2026-09-14 회차가 5시간41분 밀린 사고 재현에 쓴다)."""

    def __init__(self, utc: datetime.datetime):
        assert utc.tzinfo is not None, "aware datetime이어야 한다"
        self._utc = utc

    def advance(self, **kwargs):
        self._utc = self._utc + datetime.timedelta(**kwargs)

    def now_utc(self) -> datetime.datetime:
        return self._utc

    def now_kst(self) -> datetime.datetime:
        return self._utc.astimezone(_KST)


class FakeRng:
    """항상 목록의 첫 항목(들)을 고른다 — 결정적이라 시나리오를 재현하기
    쉽다. 특정 항목을 강제로 뽑고 싶으면 force_choice/force_sample로 지정한다."""

    def __init__(self):
        self.force_choice = None
        self.force_sample = None

    def choice(self, seq: Sequence):
        if self.force_choice is not None:
            for item in seq:
                if getattr(item, "id", item) == self.force_choice:
                    return item
        return seq[0]

    def sample(self, population: Sequence, k: int) -> list:
        pop = list(population)
        if self.force_sample:
            wanted = [p for p in pop if getattr(p, "id", p) in self.force_sample]
            rest = [p for p in pop if p not in wanted]
            return (wanted + rest)[:k]
        return pop[:k]


class ListLogger:
    """로그를 리스트에 쌓기만 한다. 테스트에서 특정 메시지가 찍혔는지 검사할
    때 쓴다(예: "[쿨다운] ... 직전 조합은 계속 제외" 같은 로그)."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(str(message))

    def has(self, substring: str) -> bool:
        return any(substring in line for line in self.lines)


class ScriptedLlm:
    """생성 호출(model=None)과 판정 호출(model=judge_model)을 별도 큐로
    구분한다 — generate_passage()가 시도마다 두 호출을 다 하므로, 하나의
    큐를 같이 쓰면 판정 호출이 생성용 응답을 가로채 간다.

    생성 큐는 시도 순서 그대로 준비해 두면 된다(4회 시도, 3회차에 조합
    교체하는 재시도 로직을 그대로 재현할 수 있다). 큐가 비면 빈 문자열을
    돌려준다 — 실제 어댑터가 호출 실패 시 빈 문자열을 돌려주는 것과 같다.
    판정 큐는 기본적으로 "전부 통과"만 채워지며, 호출 때마다 소진된다."""

    def __init__(self, responses: list[str] | None = None,
                judge_pass: str = ""):
        self.responses = list(responses or [])
        self._i = 0
        # 빈 문자열이면 judge_naturalness()의 fail-open 경로(호출 실패 →
        # 판정 생략, 전부 통과 처리)를 그대로 탄다 — 판정 자체를 테스트하는
        # 게 아니면 이 기본값으로 충분하다.
        self.judge_pass = judge_pass
        self.calls: list[dict] = []

    def generate(self, prompt: str, temperature: float,
                model: str | None = None, max_output_tokens: int = 2200) -> str:
        self.calls.append({"prompt": prompt, "temperature": temperature, "model": model})
        if model is not None:
            return self.judge_pass
        if self._i < len(self.responses):
            raw = self.responses[self._i]
            self._i += 1
            return raw
        return ""


class RecordingMailer:
    """보낸 메일을 전부 기록한다. send_result로 성공/실패를 강제할 수 있다
    (SMTP 실패 경로를 네트워크 없이 재현하기 위해)."""

    def __init__(self, send_result: bool = True):
        self.sent: list[dict] = []
        self.send_result = send_result

    def send(self, subject: str, html: str, to: Sequence[str],
             attachment_path: str = "") -> bool:
        self.sent.append({"subject": subject, "html": html, "to": list(to),
                          "attachment_path": attachment_path})
        return self.send_result


class InMemoryStore:
    """JSON 파일 대신 dict에 저장한다. 초기 상태를 생성자에 넘겨 "쿨다운이
    풀을 거의 덮은 상황" 같은 시나리오를 바로 구성할 수 있다."""

    def __init__(self, initial: dict | None = None):
        self._data = {k: _deep_copy(v) for k, v in (initial or {}).items()}
        self.write_log: list[str] = []  # 어떤 이름이 몇 번 쓰였는지 검사용

    def read(self, name: str, default: dict) -> dict:
        if name not in self._data:
            return _deep_copy(default)
        return _deep_copy(self._data[name])

    def write(self, name: str, data: dict) -> None:
        self._data[name] = _deep_copy(data)
        self.write_log.append(name)

    def path_of(self, name: str) -> str:
        return f"<fake>/{name}.json"


def _deep_copy(obj):
    import copy
    return copy.deepcopy(obj)


class RecordingVcs:
    """실제로 커밋하지 않고 호출만 기록한다."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.commits: list[dict] = []

    def commit_and_push(self, paths: Sequence[str], message: str) -> bool:
        if not self.enabled:
            return False
        self.commits.append({"paths": list(paths), "message": message})
        return True


class FakeFonts:
    def __init__(self, path: str = "/fake/font.ttc"):
        self.path = path

    def find(self, style: str = "") -> str:
        return self.path


def make_deps(
    *,
    utc: datetime.datetime | None = None,
    manual: bool = False,
    forced_category: str = "",
    in_ci: bool = True,
    store_initial: dict | None = None,
    llm_responses: list[str] | None = None,
    mail_ok: bool = True,
    settings: ports.Settings | None = None,
    secrets: ports.Secrets | None = None,
) -> tuple[ports.Deps, dict]:
    """테스트에서 자주 쓰는 조합을 한 번에 조립한다. 두 번째 반환값은
    개별 가짜 구현에 접근할 수 있는 dict — 호출 기록을 검사할 때 쓴다."""
    if utc is None:
        # 2026-09-14(월) 11:00 UTC — 정시 실행의 기본 시각
        utc = datetime.datetime(2026, 9, 14, 11, 0, tzinfo=datetime.timezone.utc)
    clock = FakeClock(utc)
    rng = FakeRng()
    log = ListLogger()
    llm = ScriptedLlm(llm_responses)
    mailer = RecordingMailer(send_result=mail_ok)
    store = InMemoryStore(store_initial)
    vcs = RecordingVcs(enabled=in_ci)
    fonts = FakeFonts()

    deps = ports.Deps(
        clock=clock, rng=rng, log=log, llm=llm, mailer=mailer,
        store=store, vcs=vcs, fonts=fonts,
        secrets=secrets or ports.Secrets(
            gmail_address="ops@example.com", gmail_app_password="x",
            gemini_api_key="x", email_recipients="reader@example.com",
        ),
        mode=ports.RunMode(manual=manual, forced_category=forced_category,
                           in_ci=in_ci),
        settings=settings or ports.Settings(),
    )
    fakes = {"clock": clock, "rng": rng, "log": log, "llm": llm,
             "mailer": mailer, "store": store, "vcs": vcs, "fonts": fonts}
    return deps, fakes


def passage_text(patterns_terms: list[str], sentence_count: int, ending: str = "。") -> str:
    """검증을 통과할 최소 지문을 조립한다. patterns_terms에 넣은 문자열이
    그대로 한 문장씩 들어가고, 부족한 문장 수는 무해한 필러로 채운다."""
    sentences = list(patterns_terms)
    filler = "その日は天気が良かった。"
    while len(sentences) < sentence_count:
        sentences.append(filler)
    body = "".join(s if s.endswith(ending) else s + ending for s in sentences[:sentence_count])
    return f"テーマ\n---\n{body}"
