"""월간 누적 실패 통계 리포트.

get_grammar.py의 일일 파이프라인(오늘의 지문 생성·검증·발송)과는 관심사가
다르다 — 이쪽은 "그동안 뭐가 문제였나"를 숫자로 보여주는 것뿐이라 별도
모듈로 뒀다(SRP: 한 파일이 두 가지 이유로 바뀌지 않게).

이 모듈 안에서도 역할을 또 나눈다:
- aggregate_stats(): 이력 dict → 숫자만 계산한다(그릴 줄도, 보낼 줄도 모른다)
- render_chart(): 그 숫자 → PNG 파일. 숫자가 어떻게 모였는지는 모른다
- build_report_email(): 그 숫자 → 메일 제목·본문. 파일 입출력을 전혀 안 한다
- send_monthly_report(): 위 셋을 deps(저장소·메일·로그)에 연결하는 조립부

각자 "바뀌는 이유"가 다르므로(집계 로직 변경 / 차트 라이브러리 교체 / 메일
문구 수정 / 발송 조건 변경) 하나를 건드려도 나머지 셋은 그대로 둘 수 있다.
집계는 요일별 통계에 한해서만 "요일-매칭 실행"만 걸러낸다 — 카테고리별
통계는 FORCE_CATEGORY로 강제 실행한 날도 그 카테고리의 실제 신호이지만,
요일별 통계는 임의 요일에 강제 실행한 테스트가 섞이면 그 요일의 실패율이
왜곡된다(2026-09-13 일요일 수동 테스트처럼).

차트에 한글 폰트가 필요해서 get_grammar.py의 ports.Fonts를 그대로 재사용한다
(build_pdf()가 PDF용으로 쓰는 것과 같은 포트) — 차트 전용 새 포트를 만들지
않는다. 이미 있는 추상화로 충분한데 비슷한 것을 또 만들면 인터페이스만
늘어난다.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass

import grammar_bank as GB
import ports
from get_grammar import (
    _mask_email,
    _pattern_fail_counts,
    _plain_to_html,
    load_failure_history,
    load_history,
)

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# reason 문자열의 접두사 → 분류 라벨. validate_passage()/generate_passage()가
# 실제로 내는 문구와 맞춰뒀다(MANUAL 3장 "자주 나오는 실패 사유" 표 참고).
# 순서가 중요하다 — "문장 수"처럼 뒤에 가변 숫자가 붙는 것은 startswith로
# 비교해야 하므로, 더 구체적인 접두사를 먼저 둘 필요는 없지만 겹치는 접두사가
# 생기면 위에서부터 먼저 매치된다는 점은 유지한다.
_REASON_TYPES = [
    ("문형 누락", "문형 누락"),
    ("구조 조건 미충족", "구조 조건 미충족"),
    ("자연스러움 판정 실패", "자연스러움 판정 실패"),
    ("문장 수", "문장 수 범위 이탈"),
    ("Gemini 출력 파싱 실패", "출력 파싱 실패"),
    ("빈 지문", "출력 파싱 실패"),
    ("생성 중간에 잘림", "출력 파싱 실패"),
]


def _reason_type(reason: str) -> str:
    for prefix, label in _REASON_TYPES:
        if reason.startswith(prefix):
            return label
    return "기타"


def _is_real_weekday_match(date_str: str, category_key: str) -> bool:
    category = GB.CATEGORY_BY_KEY.get(category_key)
    if category is None:
        return False
    d = datetime.date.fromisoformat(date_str)
    return d.weekday() == category["weekday"]


@dataclass
class MonthlyStats:
    category_totals: dict   # {category: {"fail": n, "total": n}}
    weekday_totals: dict    # {"월": {"fail": n, "total": n}, ...} — 요일-매칭 실행만
    reason_breakdown: dict  # {reason_type: 실패일수}
    top_patterns: list      # [(pattern_id, 관여일수), ...] 내림차순, 상위 10
    total_runs: int
    total_failures: int


def aggregate_stats(failure_history: dict, used_history: dict) -> MonthlyStats:
    """누적 전체(주 단위로 끊지 않음) 이력을 숫자로 요약한다. 파일 입출력도,
    그리기도, 메일도 모른다 — 입력 두 dict만으로 결정되는 순수 함수라
    테스트하기 쉽다."""
    category_totals: dict = {}

    def _bump_category(cat: str, fail: bool) -> None:
        entry = category_totals.setdefault(cat, {"fail": 0, "total": 0})
        entry["total"] += 1
        if fail:
            entry["fail"] += 1

    for r in used_history.get("runs", []):
        _bump_category(r["category"], fail=False)
    for r in failure_history.get("runs", []):
        _bump_category(r["category"], fail=True)

    weekday_totals = {label: {"fail": 0, "total": 0} for label in _WEEKDAY_KR}

    def _bump_weekday(date_str: str, cat: str, fail: bool) -> None:
        if not _is_real_weekday_match(date_str, cat):
            return
        label = _WEEKDAY_KR[datetime.date.fromisoformat(date_str).weekday()]
        weekday_totals[label]["total"] += 1
        if fail:
            weekday_totals[label]["fail"] += 1

    for r in used_history.get("runs", []):
        _bump_weekday(r["date"], r["category"], fail=False)
    for r in failure_history.get("runs", []):
        _bump_weekday(r["date"], r["category"], fail=True)

    reason_breakdown: dict = {}
    for run in failure_history.get("runs", []):
        types_today = {_reason_type(f["reason"]) for f in run.get("failures", [])}
        for t in types_today:
            reason_breakdown[t] = reason_breakdown.get(t, 0) + 1

    pattern_counts = _pattern_fail_counts(failure_history, window=None)
    top_patterns = sorted(pattern_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return MonthlyStats(
        category_totals=category_totals,
        weekday_totals=weekday_totals,
        reason_breakdown=reason_breakdown,
        top_patterns=top_patterns,
        total_runs=len(used_history.get("runs", [])) + len(failure_history.get("runs", [])),
        total_failures=len(failure_history.get("runs", [])),
    )


def render_chart(deps: ports.Deps, stats: MonthlyStats, out_path: str) -> None:
    """카테고리별·요일별 실패율을 막대그래프 2개로 그려 out_path에 PNG로
    저장한다. 한글 폰트를 못 찾아도(로컬 개발 환경 등) 차트 생성 자체는
    멈추지 않는다 — 기본 폰트로라도 그리고 로그만 남긴다."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    try:
        font_path = deps.fonts.find("")
        font_manager.fontManager.addfont(font_path)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=font_path).get_name()
    except (FileNotFoundError, OSError) as e:
        deps.log(f"[월간리포트] 한글 폰트를 못 찾아 기본 폰트로 진행: {e}")
    plt.rcParams["axes.unicode_minus"] = False

    def _rate_bars(ax, totals: dict, title: str) -> None:
        labels = [k for k, v in totals.items() if v["total"] > 0]
        values = [totals[k] for k in labels]
        rates = [v["fail"] / v["total"] * 100 for v in values]
        bars = ax.bar(labels, rates, color="#c0392b")
        ax.set_title(title)
        ax.set_ylabel("실패율(%)")
        ax.set_ylim(0, 100)
        for bar, v in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2,
                    f"{v['fail']}/{v['total']}", ha="center", fontsize=8)
        ax.tick_params(axis="x", rotation=30)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    _rate_bars(ax1, stats.category_totals, "카테고리별 실패율(누적)")
    _rate_bars(ax2, stats.weekday_totals, "요일별 실패율(누적, 요일-매칭 실행만)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def build_report_email(stats: MonthlyStats, today: datetime.date) -> tuple[str, str]:
    """차트 파일에는 손대지 않는다 — 제목·본문 문자열만 만든다."""
    subject = f"[운영 알림] 월간 실패 통계 리포트 — {today.isoformat()}"
    lines = [
        f"누적 실행 {stats.total_runs}회 중 실패 {stats.total_failures}회"
        f"({(stats.total_failures / stats.total_runs * 100) if stats.total_runs else 0:.0f}%).",
        "주 단위가 아니라 서비스 시작 이래 전체 누적 기준입니다.",
        "",
        "[카테고리별]",
    ]
    for cat, v in stats.category_totals.items():
        rate = (v["fail"] / v["total"] * 100) if v["total"] else 0
        lines.append(f"  {cat}: {v['fail']}/{v['total']} ({rate:.0f}%)")

    lines.append("")
    lines.append("[요일별 — 요일-매칭 실행만, FORCE_CATEGORY 수동 실행 제외]")
    for day, v in stats.weekday_totals.items():
        if v["total"] == 0:
            continue
        rate = (v["fail"] / v["total"] * 100) if v["total"] else 0
        lines.append(f"  {day}: {v['fail']}/{v['total']} ({rate:.0f}%)")

    if stats.reason_breakdown:
        lines.append("")
        lines.append("[실패 사유 유형별 — 실패가 있었던 날짜 수 기준]")
        for t, c in sorted(stats.reason_breakdown.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {t}: {c}일")

    if stats.top_patterns:
        lines.append("")
        lines.append("[실패에 자주 관여한 문형 — 상위 10, 누적]")
        for pid, c in stats.top_patterns:
            lines.append(f"  {pid}: {c}일")

    lines.append("")
    lines.append("첨부된 차트(카테고리별·요일별 실패율)를 참고하세요.")
    return subject, _plain_to_html(lines)


def send_monthly_report(deps: ports.Deps, today: datetime.date, base_dir: str) -> None:
    """조립부. 저장소에서 읽고, 집계하고, 그리고, 메일을 보낸다 — 이 함수
    자체는 "무엇을 셀지"도 "어떻게 그릴지"도 모르고, 그냥 순서대로 부른다."""
    if not deps.secrets.can_send_mail():
        deps.log("[월간리포트] 인증 정보 없음 — 발송 생략")
        return

    failure_history = load_failure_history(deps)
    used_history = load_history(deps)
    stats = aggregate_stats(failure_history, used_history)
    if stats.total_runs == 0:
        deps.log("[월간리포트] 누적 데이터 없음 — 발송 생략")
        return

    chart_path = os.path.join(base_dir, f"monthly_report_{today.isoformat()}.png")
    render_chart(deps, stats, chart_path)
    subject, html = build_report_email(stats, today)

    admin = deps.secrets.gmail_address
    if deps.mailer.send(subject, html, [admin], chart_path):
        deps.log(f"[월간리포트] 발송 완료 → {_mask_email(admin)}")
    else:
        deps.log("[월간리포트] 발송 실패")


if __name__ == "__main__":
    import adapters

    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _deps = adapters.build_production_deps(_BASE_DIR)
    send_monthly_report(_deps, _deps.clock.now_utc().date(), _BASE_DIR)
