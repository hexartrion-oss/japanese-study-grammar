"""주입 대상(포트) 정의.

이 파일에는 **인터페이스와 설정값만** 둔다. 실제 구현(네트워크·파일·git)은
adapters.py에, 파이프라인 로직은 get_grammar.py에 있다.

왜 이렇게 나눴나
----------------
이전에는 get_grammar.py 하나가 로직과 외부 연동을 함께 들고 있었다. 모듈을
import하는 순간 os.environ을 읽고 Gemini/SMTP/git을 직접 호출하는 구조라,
검증하려면 매번 정규식으로 함수 본문을 떼어내 exec하는 편법을 써야 했다.
실제로 2026-09-13 이후 수정분(てしまう 활용형, exclude_ids, _send_date,
らしい 오탐)을 전부 그런 식으로 확인했고, 그 과정에서 exclude_ids가 쿨다운
우회 경로에서 버려지는 것을 한 번 놓쳤다.

포트를 주입하면 같은 검증을 가짜 구현으로 정식 테스트에 쓸 수 있다. 부작용은
전부 이 경계 밖으로 나가므로, 파이프라인은 입력을 받아 출력을 내는 함수들로
남는다.

Protocol을 쓰는 이유는 구현체가 이 파일을 import하지 않아도 되기 때문이다
(구조적 서브타이핑). 어댑터도 가짜도 상속 없이 시그니처만 맞추면 된다.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence


# ── 설정 ───────────────────────────────────────────────
@dataclass(frozen=True)
class Secrets:
    """자격 증명. 로그·메일 본문에 절대 그대로 싣지 않는다."""
    gmail_address: str = ""
    gmail_app_password: str = ""
    gemini_api_key: str = ""
    email_recipients: str = ""

    def recipient_list(self) -> list[str]:
        return [a.strip() for a in self.email_recipients.split(",") if a.strip()]

    def can_send_mail(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password)


@dataclass(frozen=True)
class RunMode:
    """이번 실행이 어떤 성격인지. 워크플로가 환경변수로 넘긴다."""
    manual: bool = False           # workflow_dispatch로 사람이 실행했는가
    manual_mail_to: str = ""       # 수동 실행 시 단일 수신자
    forced_category: str = ""      # FORCE_CATEGORY (grammar_bank의 key와 정확히 일치해야 함)
    in_ci: bool = False            # GITHUB_ACTIONS == "true" (git 커밋 여부를 가른다)


@dataclass(frozen=True)
class Settings:
    """튜닝 가능한 값. 테스트에서 경계값을 직접 바꿔 넣을 수 있도록 상수가
    아니라 주입값으로 둔다."""
    cooldown_runs: int = 3          # 같은 카테고리에서 최근 N회 안에 쓰인 문형은 제외
    patterns_per_day: int = 5       # 하루 지문에 쓰는 문형 개수
    sentence_min: int = 10
    sentence_max: int = 20
    max_gen_attempts: int = 4
    failure_repeat_window: int = 5      # 최근 N회 실행 중에서 반복 여부를 판단
    failure_repeat_threshold: int = 3   # 그 안에서 이 횟수 이상이면 "반복 경고"
    history_keep_runs: int = 60         # 이력 파일에 남기는 최대 실행 수

    # 판정은 생성과 다른 모델로 돌린다 — 같은 모델이 자기 글을 스스로
    # 판정하면 방금 만든 오류를 못 보는 경향이 있다(README "자연스러움 판정" 참고).
    judge_model: str = "gemini-3.5-flash"

    # 쿨다운 우회 임계값에는 여유 마진이 필요하다. 후보가 patterns_per_day와
    # 같거나 조금 큰 상태에서는 조합이 몇 가지로 고정되어 재시도가 무의미해진다.
    # MANUAL 4장 참고 — 이 마진을 0으로 되돌리지 않는다.
    cooldown_bypass_margin: int = 2

    # 문장 구조를 강제하지 않는 짧은 조사류(_LOW_STRUCTURE_IDS, get_grammar.py)는
    # 자연스럽게 쓰다가 통째로 누락되기 쉽다(2026-09-23 강조·역접 3연패 확인).
    # 하루 조합에 이 부류가 몇 개나 섞여도 되는지의 상한 — README/MANUAL
    # "강조·역접 저구조 위험군" 참고.
    low_structure_cap: int = 2

    @property
    def cooldown_bypass_threshold(self) -> int:
        return self.patterns_per_day + self.cooldown_bypass_margin


# ── 포트 ───────────────────────────────────────────────
class Clock(Protocol):
    """'지금'을 알려주는 유일한 통로. datetime.now를 직접 부르지 않는다."""

    def now_utc(self) -> datetime.datetime: ...

    def now_kst(self) -> datetime.datetime: ...


class Rng(Protocol):
    """문형 추첨. 테스트에서 결정적으로 만들기 위해 주입한다."""

    def choice(self, seq: Sequence): ...

    def sample(self, population: Sequence, k: int) -> list: ...


class Logger(Protocol):
    """운영 로그. 실행 로그 파일과 stdout 양쪽으로 나간다."""

    def __call__(self, message: str) -> None: ...


class Llm(Protocol):
    """텍스트 생성 모델.

    model이 None이면 어댑터가 기본 모델 목록을 순서대로 시도한다. 호출이
    실패하면 빈 문자열을 돌려준다 — 예외를 던지지 않는 것은 판정 호출이
    실패해도 발송을 막지 않는(fail-open) 기존 동작을 유지하기 위해서다.
    """

    def generate(self, prompt: str, temperature: float,
                 model: str | None = None, max_output_tokens: int = 2200) -> str: ...


class Mailer(Protocol):
    """메일 발송. 성공 여부만 돌려주고 예외는 어댑터가 삼킨다."""

    def send(self, subject: str, html: str, to: Sequence[str],
             attachment_path: str = "") -> bool: ...


class Store(Protocol):
    """JSON 상태 파일 읽기/쓰기. 경로가 아니라 논리적 이름으로 접근한다."""

    def read(self, name: str, default: dict) -> dict: ...

    def write(self, name: str, data: dict) -> None: ...

    def path_of(self, name: str) -> str: ...


class Vcs(Protocol):
    """상태 파일을 리포지토리에 되돌려 커밋한다. CI 밖에서는 no-op."""

    def commit_and_push(self, paths: Sequence[str], message: str) -> bool: ...


class Fonts(Protocol):
    """PDF용 일본어 폰트 탐색. 없으면 FileNotFoundError를 던진다."""

    def find(self, style: str = "") -> str: ...


# ── 주입 컨테이너 ───────────────────────────────────────
@dataclass(frozen=True)
class Deps:
    """파이프라인이 바깥 세계와 닿는 모든 지점.

    함수가 이 컨테이너를 통째로 받는 이유는, 인자 목록이 길어지는 것보다
    '이 함수는 부작용이 있다'가 시그니처에 드러나는 쪽이 읽기 쉬워서다.
    deps를 안 받는 함수는 순수 함수라는 뜻이 된다.
    """
    clock: Clock
    rng: Rng
    log: Logger
    llm: Llm
    mailer: Mailer
    store: Store
    vcs: Vcs
    fonts: Fonts
    secrets: Secrets = field(default_factory=Secrets)
    mode: RunMode = field(default_factory=RunMode)
    settings: Settings = field(default_factory=Settings)


# 상태 파일의 논리적 이름. Store 구현이 실제 경로로 바꾼다.
HISTORY = "used_history"
FAILURE_HISTORY = "failure_history"
SHADOW_REVIEW = "shadow_review"
