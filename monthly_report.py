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


# 차트 축에 쓰는 표시명 — 코드가 기록하는 상태 문자열(ran 등)을 사람이 읽는 말로.
_STATUS_NAMES = {
    "ran": "판정 실행", "call_failed": "호출 실패\n(통과 처리)", "parse_partial": "파싱 일부 누락",
    "parse_failed": "파싱 실패", "natural": "자연", "unnatural": "부자연", "unknown": "불명",
    "unavailable": "호출 불가",
}


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


@dataclass
class UsageStats:
    """용법 기록(usage_log_*)에서 뽑은 통계. 실패 통계(MonthlyStats)와 별개로 둔다(OCP)."""
    runs_total: int
    runs_passed: int
    runs_failed: int
    final_judge: dict        # 통과한 실행의 최종 시도에서 판정이 어땠나 {status: n}
    judge_attempts: dict     # 전체 시도의 판정 상태 {status: n}
    evidence_verdicts: dict  # {natural/unnatural/unknown/unavailable: n}
    evidence_status: dict    # {ok/quota/error/unavailable: n}
    overturned_status: dict  # {candidate/confirmed/vetoed: n}
    top_patterns: list       # [(pattern, 통과 지문 사용 횟수)] 상위 10
    rules_versions: dict     # {rules_version: 실행 수} — 규칙 개정 전후를 가르는 용도
    review_queue: list       # 사람 확인을 기다리는 후보(최대 10)


def aggregate_usage_stats(runs: list, overturned: list) -> UsageStats:
    """순수 함수: 기록 리스트 → 숫자. 파일·메일·차트를 모른다."""
    def bump(d: dict, k) -> None:
        if k is not None and k != "":
            d[k] = d.get(k, 0) + 1

    final_judge: dict = {}
    judge_attempts: dict = {}
    ev_verdicts: dict = {}
    ev_status: dict = {}
    rules: dict = {}
    pattern_counts: dict = {}
    passed = failed = 0
    for r in runs:
        if r.get("result") == "passed":
            passed += 1
            bump(final_judge, r.get("final_judge_status"))
            for pid in r.get("patterns", []):
                bump(pattern_counts, pid)
        else:
            failed += 1
        bump(rules, r.get("rules_version"))
        for a in r.get("attempts", []):
            bump(judge_attempts, a.get("judge_status"))
            for ev in a.get("evidence", []):
                bump(ev_verdicts, ev.get("verdict"))
                bump(ev_status, ev.get("status"))
    cases_status: dict = {}
    queue = []
    for c in overturned:
        bump(cases_status, c.get("status"))
        if c.get("status") == "candidate" and len(queue) < 10:
            queue.append(c)
    top = sorted(pattern_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return UsageStats(len(runs), passed, failed, final_judge, judge_attempts, ev_verdicts,
                      ev_status, cases_status, top, rules, queue)


def render_chart(deps: ports.Deps, stats: MonthlyStats, out_path: str,
                 usage_stats: "UsageStats | None" = None) -> None:
    """카테고리별·요일별 실패율을 막대그래프로 그려 out_path에 PNG로 저장한다. usage_stats가
    있으면 아래 행에 판정 상태·증거 verdict·문형 사용 횟수를 한 장에 더 그린다(Mailer가 첨부를
    1개만 받으므로 한 장으로 합친다). 한글 폰트를 못 찾아도 차트 생성 자체는 멈추지 않는다."""
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

    def _quiet(ax) -> None:
        # 격자·축은 눈에 덜 띄게: 위·오른쪽 선 제거, 가는 연회색 가로 격자만
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.yaxis.grid(True, color="#e3e3e3", linewidth=0.6)
        ax.set_axisbelow(True)

    def _rate_bars(ax, totals: dict, title: str) -> None:
        labels = [k for k, v in totals.items() if v["total"] > 0]
        if not labels:
            ax.set_title(title)
            ax.text(0.5, 0.5, "데이터 없음", ha="center", va="center", transform=ax.transAxes,
                    color="#777777")
            ax.set_xticks([]); ax.set_yticks([])
            return
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
        _quiet(ax)

    from matplotlib.ticker import MaxNLocator

    def _count_bars(ax, counts: dict, title: str, order: list | None = None, horizontal=False) -> None:
        keys = [k for k in (order or list(counts)) if counts.get(k)] or list(counts)
        vals = [counts.get(k, 0) for k in keys]
        names = [_STATUS_NAMES.get(k, k) for k in keys]
        ax.set_title(title, fontsize=10)
        if not keys:
            ax.text(0.5, 0.5, "데이터 없음", ha="center", va="center", transform=ax.transAxes,
                    color="#777777")
            ax.set_xticks([]); ax.set_yticks([])
            return
        if horizontal:
            bars = ax.barh(names[::-1], vals[::-1], color="#2a6fb0", height=0.6)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            for b, v in zip(bars, vals[::-1]):
                ax.text(b.get_width() + 0.05, b.get_y() + b.get_height() / 2, str(v),
                        va="center", fontsize=8)
            ax.xaxis.grid(True, color="#e3e3e3", linewidth=0.6)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.set_axisbelow(True)
        else:
            bars = ax.bar(names, vals, color="#2a6fb0", width=0.6)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            for b, v in zip(bars, vals):
                ax.text(b.get_x() + b.get_width() / 2, b.get_height(), str(v), ha="center",
                        va="bottom", fontsize=8)
            _quiet(ax)
            ax.tick_params(axis="x", rotation=20)

    if usage_stats is not None and usage_stats.runs_total > 0:
        fig = plt.figure(figsize=(12, 8.5))
        gs = fig.add_gridspec(2, 3)
        ax1 = fig.add_subplot(gs[0, :1]); ax2 = fig.add_subplot(gs[0, 1:])
        _rate_bars(ax1, stats.category_totals, "카테고리별 실패율(누적)")
        _rate_bars(ax2, stats.weekday_totals, "요일별 실패율(누적, 요일-매칭 실행만)")
        _count_bars(fig.add_subplot(gs[1, 0]), usage_stats.final_judge,
                    "통과한 실행의 판정 상태(최종 시도)", ["ran", "call_failed", "parse_partial", "parse_failed"])
        _count_bars(fig.add_subplot(gs[1, 1]), usage_stats.evidence_verdicts,
                    "증거 검증 판정(shadow)", ["natural", "unnatural", "unknown", "unavailable"])
        _count_bars(fig.add_subplot(gs[1, 2]), dict(usage_stats.top_patterns),
                    "통과 지문에 쓰인 문형(상위)", horizontal=True)
    else:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
        _rate_bars(ax1, stats.category_totals, "카테고리별 실패율(누적)")
        _rate_bars(ax2, stats.weekday_totals, "요일별 실패율(누적, 요일-매칭 실행만)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def build_report_email(stats: MonthlyStats, today: datetime.date,
                       usage_stats: "UsageStats | None" = None) -> tuple[str, str]:
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

    if usage_stats is not None and usage_stats.runs_total > 0:
        lines.extend(_usage_lines(usage_stats))
    lines.append("")
    lines.append("첨부된 차트(카테고리별·요일별 실패율" +
                 (", 판정 상태·증거·문형 사용" if usage_stats is not None and usage_stats.runs_total > 0 else "") +
                 ")를 참고하세요.")
    return subject, _plain_to_html(lines)


def _usage_lines(u: "UsageStats") -> list:
    """용법·판정·증거 섹션 문구. 계산은 aggregate_usage_stats가 이미 했다."""
    out = ["", "──── 용법·판정·증거 통계 ────",
           f"기록된 실행 {u.runs_total}회 (통과 {u.runs_passed} / 실패 {u.runs_failed})"]
    ran = u.final_judge.get("ran", 0)
    out.append(f"[판정 가동률] 통과한 실행의 최종 시도 중 판정이 실제로 돈 것 {ran}/{u.runs_passed} — "
               "나머지는 호출 실패·파싱 누락으로 판정 없이 통과(fail-open)")
    if u.judge_attempts:
        out.append("  전체 시도의 판정 상태: " + ", ".join(f"{k} {v}" for k, v in sorted(u.judge_attempts.items())))
    if u.evidence_verdicts or u.evidence_status:
        out.append("[증거 검증(shadow)] 판정: " + ", ".join(f"{k} {v}" for k, v in sorted(u.evidence_verdicts.items()))
                   + " / 호출 상태: " + ", ".join(f"{k} {v}" for k, v in sorted(u.evidence_status.items())))
    if u.overturned_status:
        out.append("[판정·코드 vs 웹 이견 사례] " + ", ".join(f"{k} {v}" for k, v in sorted(u.overturned_status.items())))
    if u.rules_versions:
        out.append("[규칙 버전별 실행 수] " + ", ".join(f"{k}: {v}" for k, v in sorted(u.rules_versions.items())))
    if u.review_queue:
        out.append("[검토 대기 후보 — usage_review.json의 confirmed/vetoed에 id를 넣어 주세요]")
        for c in u.review_queue:
            out.append(f"  {c.get('id', '')} · {c.get('pattern')} · {c.get('direction')}")
            out.append(f"    문장: {c.get('sentence', '')[:80]}")
            if c.get("hosts"):
                out.append(f"    출처 도메인: {', '.join(c['hosts'][:4])}")
    return out


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

    usage_stats = None
    if deps.usage_reader is not None:
        start = datetime.date.fromisoformat(deps.settings.usage_start_month + "-01")
        usage_stats = aggregate_usage_stats(deps.usage_reader.runs_between(start, today),
                                            deps.usage_reader.overturned_between(start, today))

    chart_path = os.path.join(base_dir, f"monthly_report_{today.isoformat()}.png")
    render_chart(deps, stats, chart_path, usage_stats)
    subject, html = build_report_email(stats, today, usage_stats)

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
