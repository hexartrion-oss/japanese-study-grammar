"""일본어 문법 학습 메일링 (N3+).

japanese-study의 자연스러운 지문 생성 파이프라인을 그대로 가져오되,
학습 대상을 동사 활용이 아니라 실제 문법 구조(부사+구문 세트, 인용·전문,
강조·역접, 비즈니스 논리 접속, N1 문어체)로 좁힌 버전이다.

핵심 원칙 — 전부 이전 대화에서 확정된 방침:
1. 힌트 금지: 지문에 강조 표시나 설명을 넣지 않는다. 번역도 첨부하지 않는다.
   학습자가 지문을 읽고 직접 번역하면서 문형을 스스로 알아채는 방식이다.
2. 문법 카테고리 비노출: 메일 제목·본문 어디에도 오늘이 무슨 카테고리인지
   드러내지 않는다. 카테고리 정보는 run_log.txt(운영자 로그)에만 남는다.
3. 문형은 구조로만 채택한다: 단어 하나(예: せっかく)가 아니라 문형 세트
   (せっかく〜のに)로만 뱅크에 올라간다.
4. 문형 뱅크는 외부 사이트 스크래핑 없이 직접 검증해 점진적으로 확장한다.
5. 쿨다운: 같은 문형이 너무 자주 재등장하지 않도록 최근 사용 이력을 확인한다.
"""

import os
import re
import sys
import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from janome.tokenizer import Tokenizer

import grammar_bank as GB
import ports

# ── 경로/기본값 ────────────────────────────────────────
# 환경변수·자격 증명은 더 이상 이 모듈이 직접 읽지 않는다. adapters.py가
# 읽어서 ports.Deps로 주입한다 — import만으로 부작용이 생기지 않아야
# 테스트가 가능해지기 때문이다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _today_kst(deps: ports.Deps) -> datetime.date:
    """실행 시각의 KST 날짜. 수동 실행의 회차 날짜로만 쓴다 — 정규 실행은
    _send_date()를 거쳐야 한다(이유는 그쪽 주석 참고)."""
    return deps.clock.now_kst().date()


def _send_date(deps: ports.Deps) -> datetime.date:
    """이 실행이 담당하는 '발송 회차'의 날짜. 카테고리 선택(pick_category),
    주말 가드, 주간 리포트 요일 판정(send_weekly_shadow_report), 이력 기록이
    모두 이 날짜를 기준으로 한다.

    정규 실행은 cron "0 11 * * 1-5"(UTC 11:00)로 예약되지만 GitHub은 부하에
    따라 예약 실행을 몇 시간씩 미룬다 — 이 저장소 실측으로 2시간52분~5시간41분.
    실행 시각의 KST 벽시계로 날짜를 잡으면 지연이 KST 자정을 넘기는 순간
    회차가 통째로 다음 날로 밀린다. 2026-09-14(월) 회차가 5시간41분 밀려
    화요일 '인용'으로 나갔고, 월요일에만 도는 주간 리포트도 발동하지 않았다.
    금요일에 같은 일이 생기면 토요일로 밀려 주말 가드에 걸리고, 그날 발송은
    조용히 사라진다.

    UTC 날짜는 예정 시각이 11:00 UTC이므로 13시간까지 지연돼도 예정 날짜와
    같다(11:00 + 13:00 = 다음 날 00:00 UTC). 관측된 최대 지연의 두 배 이상
    여유가 있으므로 정규 실행은 UTC 날짜를 기준으로 삼는다.

    수동 실행(workflow_dispatch)은 예약이 아니라 사람이 누른 시각 자체가
    의도이므로 KST 날짜를 그대로 쓴다. 여기에 UTC 날짜를 쓰면 KST 09:00 이전
    실행에서 오히려 전날로 어긋난다."""
    if deps.mode.manual:
        return _today_kst(deps)
    return deps.clock.now_utc().date()


# ── 카테고리 선정 ──────────────────────────────────────
def pick_category(deps: ports.Deps, today: datetime.date) -> dict:
    forced = deps.mode.forced_category
    if forced:
        cat = GB.CATEGORY_BY_KEY.get(forced)
        if cat:
            deps.log(f"[카테고리] 강제 지정: {cat['key']}")
            return cat
    weekday = today.weekday()
    cat = GB.CATEGORY_BY_WEEKDAY.get(weekday)
    if cat is None:
        # 순환표는 월~금만 정의한다(주 5일 발송). 여기 도달하는 건 수동 실행뿐이며
        # (자동 실행은 main()의 주말 가드에서 이미 중단된다) 그때는 월요일
        # 카테고리로 대체해 수동 발송 길을 막지 않는다.
        cat = GB.CATEGORY_BY_WEEKDAY[0]
        deps.log(f"[카테고리] {today} 은 순환표에 없는 요일 — 기본값으로 대체: {cat['key']}")
    else:
        deps.log(f"[카테고리] {today} → {cat['key']} (내부 로그 전용, 메일엔 비노출)")
    return cat


# ── 쿨다운 이력 ────────────────────────────────────────
def load_history(deps: ports.Deps) -> dict:
    return deps.store.read(ports.HISTORY, {"runs": []})


# ── 실패 이력 (반복 실패 문형을 자동으로 감지하기 위한 별도 기록) ──────
def load_failure_history(deps: ports.Deps) -> dict:
    return deps.store.read(ports.FAILURE_HISTORY, {"runs": []})


def append_failure_history(deps: ports.Deps, fail_history: dict, category_key: str,
                           attempts_log: list, today: datetime.date):
    """오늘 실패에서 등장한 (문형, 실패사유) 쌍을 전부 기록한다.
    실패 안 한 날(발송 성공한 날)은 이 파일에 아무것도 안 남는다 —
    "성공 여부"가 아니라 "실패가 있었는지"만 추적하는 파일이기 때문이다."""
    entries = [
        {"pattern": p, "reason": a["reason"]}
        for a in attempts_log for p in a["patterns"]
    ]
    fail_history.setdefault("runs", []).append({
        "date": today.isoformat(),
        "category": category_key,
        "failures": entries,
    })
    fail_history["runs"] = fail_history["runs"][-deps.settings.history_keep_runs:]
    deps.store.write(ports.FAILURE_HISTORY, fail_history)


# ── 그림자 비교 (코드 검증 vs LLM 판정 불일치 기록) ──────────────
def load_shadow_review(deps: ports.Deps) -> dict:
    return deps.store.read(ports.SHADOW_REVIEW, {"entries": []})


def append_shadow_review(deps: ports.Deps, review: dict, category_key: str,
                         shadow_log: list, today: datetime.date):
    """shadow_log에는 이미 code_ok != judge_ok인 항목만 들어 있다(일치하는
    항목은 generate_passage()에서 아예 담지 않는다). human_label은 사람이
    메일을 보고 채울 때까지 항상 null로 시작한다."""
    for item in shadow_log:
        review.setdefault("entries", []).append({
            "date": today.isoformat(),
            "category": category_key,
            "pattern": item["pattern"],
            "code_ok": item["code_ok"],
            "judge_ok": item["judge_ok"],
            "agree": False,
            "snippet": item["snippet"],
            "human_label": None,
        })
    deps.store.write(ports.SHADOW_REVIEW, review)


def find_repeat_offenders(deps: ports.Deps, fail_history: dict) -> list:
    """최근 settings.failure_repeat_window회의 실패 기록 안에서, 특정 문형이
    settings.failure_repeat_threshold회 이상 등장했으면 "반복 실패"로 판정한다.
    문형이 실제로 실패에 관여했다는 것만 셀 뿐, 매번 같은 사유인지는
    구분하지 않는다 — 사유가 달라도 그 문형이 계속 말썽이라는 신호는 유효하다."""
    recent_runs = fail_history.get("runs", [])[-deps.settings.failure_repeat_window:]
    counts = {}
    for run in recent_runs:
        seen_today = set()
        for f in run.get("failures", []):
            pid = f["pattern"]
            if pid in seen_today:
                continue  # 같은 날 같은 문형은 한 번만 카운트(시도 4번 다 중복 집계 방지)
            seen_today.add(pid)
            counts[pid] = counts.get(pid, 0) + 1
    return [pid for pid, c in counts.items()
            if c >= deps.settings.failure_repeat_threshold]


def recently_used(deps: ports.Deps, history: dict, category_key: str) -> set:
    matching = [r for r in history.get("runs", []) if r.get("category") == category_key]
    recent = matching[-deps.settings.cooldown_runs:]
    used = set()
    for r in recent:
        used.update(r.get("patterns", []))
    return used


# 뒷문장에 특정 결과(부정형 결론/나쁜 결과/갈리는 결과)가 반드시 와야 완성되는
# 문형들. 이런 문형이 한 지문에 2개 이상 겹치면, 서로 다른 결말 구조를
# 동시에 자연스럽게 짜야 해서 실패율이 올라간다(실제로 ないことには+ばかりに,
# なくしては+いかんによって 조합에서 확인됨). 그래서 하루에 최대 1개만 뽑는다.
_RESULT_FORCING_IDS = {"あげく", "ばかりに", "ないことには", "なくしては", "いかんによって"}


def select_patterns(deps: ports.Deps, category: dict, history: dict,
                    exclude_ids=None) -> list:
    exclude_ids = exclude_ids or set()
    used = recently_used(deps, history, category["key"]) | exclude_ids
    pool = category["patterns"]
    candidates = [p for p in pool if p.id not in used]
    if len(candidates) < deps.settings.cooldown_bypass_threshold:
        # 쿨다운은 풀어도 exclude_ids는 유지한다. 여기서 pool 전체로 되돌리면
        # 직전 시도를 죽인 조합이 그대로 다시 뽑힌다 — 재시도 상황은 정의상
        # 후보가 얇아진 상태라 이 우회가 반드시 발동하고, 결국 재선정이 가장
        # 필요한 순간에만 골라서 무력화된다(2026-09-15 인용 회차에서 재선정
        # 조합이 직전과 3/5 겹쳐 そうだ(様態)가 유임, 2·3차 연속 실패).
        relaxed = [p for p in pool if p.id not in exclude_ids]
        if len(relaxed) < deps.settings.patterns_per_day:
            # exclude_ids까지 빼면 5개를 못 채우는 극단적 경우에만 전체 풀로.
            deps.log(f"[쿨다운] 후보 부족({len(candidates)}개) — 직전 조합 제외로도"
                  f" 부족({len(relaxed)}개), 전체 풀 사용")
            relaxed = pool
        else:
            deps.log(f"[쿨다운] 후보 부족({len(candidates)}개) — 쿨다운만 무시"
                  f"(직전 조합 {len(exclude_ids)}개는 계속 제외, 후보 {len(relaxed)}개)")
        candidates = relaxed

    constrained = [p for p in candidates if p.id in _RESULT_FORCING_IDS]
    free = [p for p in candidates if p.id not in _RESULT_FORCING_IDS]

    picked = []
    if constrained:
        picked.append(deps.rng.choice(constrained))
    remaining = deps.settings.patterns_per_day - len(picked)
    if len(free) >= remaining:
        picked += deps.rng.sample(free, remaining)
    else:
        # free 풀이 부족한 예외적 경우 — constrained에서 마저 채움(모자란 만큼만)
        picked += free
        leftover = [p for p in constrained if p not in picked]
        picked += deps.rng.sample(
            leftover, min(deps.settings.patterns_per_day - len(picked), len(leftover)))

    deps.log(f"[문형] 선택됨: {', '.join(p.id for p in picked)}")
    return picked


def append_history(deps: ports.Deps, history: dict, category_key: str,
                   patterns: list, today: datetime.date):
    history.setdefault("runs", []).append({
        "date": today.isoformat(),
        "category": category_key,
        "patterns": [p.id for p in patterns],
    })
    # 무한정 커지지 않도록 최근 N회만 보존
    history["runs"] = history["runs"][-deps.settings.history_keep_runs:]
    deps.store.write(ports.HISTORY, history)


# ── Gemini 호출 ────────────────────────────────────────
def build_prompt(deps: ports.Deps, patterns: list, level_tag: str) -> str:
    s_min = deps.settings.sentence_min
    s_max = deps.settings.sentence_max
    s_target = s_min + 2   # 하한 바로 위를 목표치로 제시한다
    lines = []
    for p in patterns:
        if p.note:
            lines.append(f"- {p.id} — 【注意】{p.note}")
        else:
            lines.append(f"- {p.id}")
    pattern_list = "\n".join(lines)
    return f"""あなたは日本語で自然な読み物を書くライターです。対象レベルはJLPT {level_tag}です。

【必ず使う文型】(必ず全部、それぞれ最低1回、自然な文脈で使うこと。【注意】が付いている文型は、その指示に厳密に従うこと)
{pattern_list}

【出力ルール — 絶対厳守】
1. まず一行目に、内容を表す短い見出しを日本語で書く(文型名や文法用語は絶対に書かない、あくまで話の題材を表す一言)
2. 二行目は「---」だけ
3. 三行目以降の文章は、必ず{s_min}文以上{s_max}文以内(句点「。」の数で数える)にすること。目安ではなく絶対条件であり、多すぎても少なすぎても失格とする。文型をすべて入れることよりもこの文数制限を優先せよ。目標は{s_target}文前後(下限ギリギリを狙わない)。よくある失敗は、必要な文型を使い終えた時点で話を早々にまとめてしまい、{s_min}文に届かないまま終わることである。それを避けるため、出来事の経緯・心情の変化・具体的なエピソードを1つ以上追加で描写し、話を十分に展開させること。書き終える前に句点の数を実際に数え、{s_min}文未満なら具体的な描写を足してから出力すること
4. 文章は一つのまとまった話として展開すること(起承転結や心情の変化があること)。バラバラな文を並べただけにしない。ただし、心情や登場人物への評価が変化する場合は、その変化を自然に繋ぐ描写を必ず入れること(例: 批判的な描写から好意的な描写に移る場合、その心境の転換点を一文入れる)。前半と後半で書き手の評価や感情のトーンが理由なく矛盾しないよう、書き終えた後に一度全体を読み返して確認すること
5. 上に挙げた文型を全部、不自然にならない範囲で文章中に組み込む。ただし文数制限(ルール3)を破ってまで全部を無理に詰め込む必要はない。特に【注意】付きの文型は、指定された接続・文脈を外れると文法的に誤りになるため、必ず指示通りに使うこと
6. 説明、翻訳、注釈、箇条書き、記号、マークダウンの装飾は一切書かない。特に「**」のような強調記号は絶対に使わない(文型を目立たせる目的で強調するのは厳禁)。読み物本文だけを、装飾のない平文で書く
7. 暴力・犯罪・死亡・宗教・政治的に偏った内容は避ける
8. 見出しは内容だけを表すこと(文法カテゴリーが分かるような単語は使わない)"""


def _strip_markdown_decoration(text: str) -> str:
    """Gemini가 프롬프트 지시를 어기고 마크다운 강조 기호를 넣는 경우가
    있어서(문형을 눈에 띄게 **강조**해버리는 등), 출력 단계에서 한 번 더
    강제로 제거한다. 프롬프트 지침만으로는 안 지켜질 수 있으니 이중 방어."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # **강조**
    text = re.sub(r"__(.+?)__", r"\1", text)        # __강조__
    text = re.sub(r"(?<!\S)\*(\S.*?\S|\S)\*(?!\S)", r"\1", text)  # *강조*
    text = text.replace("**", "").replace("__", "")  # 짝이 안 맞는 잔여 기호
    return text


def parse_gemini_output(raw: str):
    if "---" not in raw:
        return None, None
    head, _, body = raw.partition("---")
    topic = head.strip().strip("#").strip()
    passage = body.strip()
    passage = re.sub(r"\n{2,}", "\n", passage)
    passage = "".join(line.strip() for line in passage.split("\n"))
    passage = _strip_markdown_decoration(passage)
    topic = _strip_markdown_decoration(topic)
    return topic, passage


# Gemini가 흔히 한자로 표기하는 문형 구성 요소를 히라가나로 되돌려서
# 검증 시 놓치지 않도록 한다 (예: ば良かった → ばよかった).
_KANJI_TO_KANA = {
    "良かった": "よかった", "良ければ": "よければ", "良い": "よい",
    "事": "こと", "為": "ため", "通り": "とおり", "筈": "はず",
    "訳": "わけ", "様だ": "ようだ", "無い": "ない", "出来る": "できる",
    "有る": "ある", "頃": "ころ", "気味": "ぎみ",
    "難くない": "かたくない", "堪えない": "たえない",
}

# Gemini가 흔히 쓰는 한자 표기 이체자 보정 (挙げ句/揚げ句/挙句 등).
_KANJI_VARIANT_TO_KANA = {
    "挙げ句": "あげく", "揚げ句": "あげく", "挙句": "あげく",
    "にも関わらず": "にもかかわらず", "にも拘らず": "にもかかわらず",
    "に他ならない": "にほかならない",
}

# 故に → ゆえに 치환은 事故に/縁故に 같은 단어와 충돌하므로 정규식으로
# 앞 글자를 제한한다. _KANJI_TO_KANA의 事→こと 치환이 먼저 적용되면
# 이 룩비하인드가 무력화되므로 반드시 그보다 먼저 실행해야 한다.
_YUENI_RE = re.compile(r"(?<![事縁物])(?<!こと)故に")

# 문형 id는 사전형(〜てしまう)으로 등록되어 있는데, 지문은 거의 항상 이야기체
# 과거 서술이라 실제로는 활용형(てしまった·てしまわない·でしまった 등)으로 등장한다.
# 리터럴 매칭만 하면 정상적으로 쓴 문형을 "누락"으로 오판한다
# (2026-09-13 「投げ出してしまわないか」가 4회 시도 전부 실패의 원인이 됨).
# 검증용 텍스트에만 쓰이므로 「てしまわない」→「てしまうない」처럼 뒤가 조금
# 어색해져도 무방하다 — 필요한 건 사전형 리터럴의 존재 여부뿐이다.
_TESHIMAU_RE = re.compile(r"[てで]しま(?:った|って|う|わ|い|え|お)")


def _normalize(text: str) -> str:
    text = _YUENI_RE.sub("ゆえに", text)
    for kanji, kana in _KANJI_VARIANT_TO_KANA.items():
        text = text.replace(kanji, kana)
    for kanji, kana in _KANJI_TO_KANA.items():
        text = text.replace(kanji, kana)
    text = _TESHIMAU_RE.sub("てしまう", text)
    return text


# ── 위험 문형 사후 구조 검증 ──────────────────────────────────
# 89개 전수 점검(문형별 결합 제약 조사)에서 "문자열만 있으면 통과되는
# 검증으로는 못 잡는" 오류가 나올 수 있다고 확인된 문형에 한해,
# 문자열 등장 여부와 별개로 구조를 한 번 더 확인한다.
# 나머지 문형(비교적 결합 범위가 넓은 것)은 build_prompt의 note 지침에만 의존한다.
_NEG_RESULT_WORDS = ["ない", "できない", "わからない", "分からない", "難しい", "無理", "不可能"]
_VARIATION_WORDS = ["分かれる", "異なる", "変わる", "決まる", "次第", "左右され", "変動する"]
_HARDSHIP_WORDS = [
    "結局", "無駄", "失敗", "後悔", "苦労", "疲れ", "諦め", "破綻", "叱られ", "怒られ", "台無し",
    "虚しい", "むなしい", "落胆", "報われ", "無意味", "徒労", "空回り", "骨折り損", "がっかり", "挫折",
    "体調を崩す", "落ち込む", "迷惑",
]
# 落ち込む는 "낙담하다"(감정) 외에 "(기온·수치가) 떨어지다"(물리적) 뜻도 있다.
# 화자 본인과 무관한 물리적 하락이 와도 _contains_any()가 통과시킬 수 있는데,
# 이 위험은 활용형 대응(_contains_any) 이전부터 사전형 그대로도 있었다 — 다의어가
# 근본 원인이라 어느 표면형이든 매치되면 동일하게 발생한다. 코드 검증이 통과시켜도
# 판정 모델이 독립적으로 다시 확인해 되돌리므로(README "자연스러움 판정" 참고)
# 곧바로 발송으로 이어지지는 않는다.
_CRITICAL_TONE_WORDS = [
    "文句", "批判", "生意気", "偉そう", "呆れ", "情けない", "許せない",
    "腹が立", "不満", "非難", "責め", "説教", "困った", "困る",
]


def _window_after(text: str, term: str, span: int = 40) -> str:
    idx = text.find(term)
    if idx == -1:
        return ""
    return text[idx + len(term): idx + len(term) + span]


def _window_before(text: str, term: str, span: int = 8) -> str:
    idx = text.find(term)
    if idx == -1:
        return ""
    return text[max(0, idx - span): idx]


# 형태소 분석기는 생성 비용이 있어 모듈 전역에 하나만 둔다. そうだ(伝聞/様態
# 구분)·らしい(존재 판정 보정)·아래 _contains_any(활용형 대응 어휘 매칭)가
# 전부 이 인스턴스를 공유하므로 이름을 용도 중립으로 둔다.
_tokenizer = Tokenizer()


def _lemmatize(text: str) -> str:
    """활용형을 사전형(base_form)으로 되돌린 텍스트. 검증용 어휘 매칭에서만
    쓴다 — 발송되는 지문 자체는 건드리지 않는다.

    2026-09-17 ばかりに 구조 검증에서 「落ち込んだ」가 목록의 사전형
    「落ち込む」와 리터럴로 안 맞아 놓친 사례가 실제로 나왔다. Gemini는
    활용형을 정확히 쓰는데, 검증 쪽이 사전형만 찾고 있었던 것이 원인이다.
    てしまう(_TESHIMAU_RE)·らしい(_check_rashii)도 같은 뿌리의 문제였지만
    그때그때 개별 대응했다 — 여기서는 동사/형용사 활용 전반을 한 번에
    다룬다.

    주의: しまう(치우다/보조동사)처럼 활용형만 보면 구분 안 되는 동형이의어가
    존재한다(_check_sou_da·_check_rashii가 품사까지 보는 이유). 그래서
    이 함수는 아래 _EXTRA_CHECKS의 단순 어휘 목록(명사·형용사·일반 동사,
    동형이의어 위험 없음 확인됨) 매칭에만 쓴다 — _TESHIMAU_RE·_check_rashii·
    Pattern.found_in()의 기본 매칭은 이번에 건드리지 않는다(별도 검토 필요)."""
    return "".join(tok.base_form if tok.base_form != "*" else tok.surface
                   for tok in _tokenizer.tokenize(text))


def _contains_any(window: str, words: list) -> bool:
    """window(원문 슬라이스) 안에 words 중 하나라도 있으면 True.
    원문 그대로 먼저 보고(대부분의 어휘는 사전형 그대로 나온다), 없으면
    활용형을 사전형으로 되돌린 버전도 본다 — 落ち込んだ처럼 활용된 채로
    등장해 리터럴 매칭을 놓치는 경우를 잡기 위해서다."""
    if any(w in window for w in words):
        return True
    lemma = _lemmatize(window)
    return any(w in lemma for w in words)


def _check_pair_negative(text: str, term: str) -> bool:
    """〜ないことには, 〜なくしては: 뒤에 부정형 결론이 와야 짝이 완성됨.
    ない/できない는 아주 흔한 단어라 창을 너무 넓히면 검증이 사실상 무의미해진다
    (아무 문장에나 ない가 하나쯤 있기 마련이라). 그래서 다른 검증보다 창을 좁게 유지한다."""
    window = _window_after(text, term, span=45)
    return _contains_any(window, _NEG_RESULT_WORDS)


def _check_result_variation(text: str, term: str) -> bool:
    """〜いかんによって: 뒤에 결과가 갈린다는 서술이 와야 함."""
    window = _window_after(text, term, span=60)
    return _contains_any(window, _VARIATION_WORDS)


def _check_hardship_after(text: str, term: str) -> bool:
    """〜あげく, 〜ばかりに: 뒤에 부정적 결과가 와야 함. 결과 어휘가
    상대적으로 특이한 단어들이라(ない처럼 아무 데나 나오지 않음), 창을
    넓혀도 오탐 위험이 크지 않다고 판단해 다른 검증보다 넓게 잡는다."""
    window = _window_after(text, term, span=70)
    return _contains_any(window, _HARDSHIP_WORDS)


def _check_critical_tone(text: str, term: str) -> bool:
    """〜くせに: 완벽한 의미 판단은 불가능하므로, 앞뒤에 비판적 어조 어휘가
    최소한 하나라도 있는지만 느슨하게 확인한다. 통과해도 진짜 비판적 어조인지
    보장 못 하며, 이 목록에 없는 단어로 비판했다면 놓칠 수 있다 — 최소한의
    안전망일 뿐이다."""
    idx = text.find(term)
    if idx == -1:
        return False
    window = text[max(0, idx - 20): idx + len(term) + 30]
    return _contains_any(window, _CRITICAL_TONE_WORDS)


def _check_collocate_before(text: str, term: str, allowed: list) -> bool:
    """〜にかたくない, 〜を禁じ得ない: 앞에 정해진 어휘군이 와야 함."""
    before = _window_before(text, term)
    return _contains_any(before, allowed)


def _check_kirai_no_double_softening(text: str) -> bool:
    """〜きらいがある: つつある/ている와 겹쳐 이중 완곡화되면 안 됨."""
    idx = text.find("きらいがある")
    if idx == -1:
        return False
    before = text[max(0, idx - 10): idx]
    return "つつある" not in before


# ── そうだ(伝聞) vs そうだ(様態) 구분 ─────────────────────
# 둘은 표기가 똑같이 "そうだ"라서 문자열 매칭만으로는 절대 구분이 안 된다.
# Janome 형태소 분석기로 실제 활용형을 확인해서 구분한다.
#
# 확인된 사실 (테스트로 검증됨):
# - "そうだ"는 항상 "そう"(名詞/接尾, 助動詞語幹) + "だ"(助動詞) 두 토큰으로 분리된다.
# - "そう"가 "그렇다/그렇게"라는 뜻의 부사(副詞)로 쓰인 경우(彼はそうだ 등)는
#   품사가 다르게 나와서(副詞), 助動詞語幹 필터로 자동 제외된다 — 문자열 검색으로는
#   못 걸렀던 오탐(誤探)까지 이번 교체로 같이 해결된다.
# - そう 직전 토큰의 활用形(infl_form)이:
#     基本形               → 伝聞 (降るそうだ, 降ったそうだ, 忙しいそうだ, 元気だそうだ의 だ)
#     連用形 / ガル接続      → 様態 (降りそうだ, 忙しそうだ, 来そうだ, 降らなそうだ의 な)
#     표층형이 정확히 "さ"   → 様態 (よさそうだ, なさそうだ의 さ, 活用形 필드가 비어있어 별도 처리)
#     な형용사 어간이 だ 없이 직접 접속 → 様態 (元気そうだ)
# - 동형이의어(降り가 降る/降りる 중 어느 쪽으로 인식되든)는 두 경우 모두 活用形이
#   "連用形"으로 같은 범주라 판정에 영향을 주지 않음을 확인했다.
def _sou_da_prev_tokens(text: str) -> list:
    """지문에서 실제 伝聞/様態 조동사로 쓰인 'そう' 토큰들의 직전 토큰을 모아 반환한다.
    そう가 부사(그렇다/그렇게)로 쓰인 경우는 품사 필터로 걸러진다."""
    tokens = list(_tokenizer.tokenize(text))
    prevs = []
    for i, tok in enumerate(tokens):
        if tok.surface == "そう" and "助動詞語幹" in tok.part_of_speech:
            prevs.append(tokens[i - 1] if i > 0 else None)
    return prevs


def _classify_prev_token(prev) -> str:
    if prev is None:
        return "様態"
    if prev.surface == "さ":
        return "様態"                              # よさそうだ・なさそうだ
    infl = prev.infl_form
    if infl == "基本形":
        return "伝聞"
    if infl in ("連用形", "ガル接続"):
        return "様態"
    if "形容動詞語幹" in prev.part_of_speech:
        return "様態"                              # 元気そうだ (だ 없이 어간 직접 접속)
    return "様態"


def _check_sou_da(text: str, want: str) -> bool:
    """지문 안의 모든 진짜 そうだ(조동사) 자리를 검사해, want(伝聞/様態)로
    판정되는 자리가 하나라도 있으면 통과시킨다."""
    prevs = _sou_da_prev_tokens(text)
    return any(_classify_prev_token(p) == want for p in prevs)


# ── 존재 검증 보정 ─────────────────────────────────────
# Pattern.found_in()은 리터럴 부분문자열 매칭이라, 문형과 똑같은 꼬리를 가진
# 일반 어휘를 문형으로 오인할 수 있다. 그런 문형만 형태소 분석으로 한 번 더
# 거른다. _EXTRA_CHECKS(용법이 맞는지)와 달리 이쪽은 "그 문형이 정말 쓰였는지"를
# 보므로, 실패하면 "구조 조건 미충족"이 아니라 "문형 누락"으로 보고해야 한다.
def _check_rashii(text: str) -> bool:
    """推量の助動詞 らしい가 실제로 쓰였는지 확인한다.

    「素晴らしい」「可愛らしい」「男らしい」는 형용사라서 문형 らしい가 아닌데,
    리터럴 매칭은 꼬리만 보고 통과시킨다. 2026-09-15 4차 지문의
    「素晴らしい経験だった」가 실제로 오탐됐고(코드는 사용됨, 판정 모델은
    미사용으로 지적), 그 지문엔 추량의 らしい가 한 번도 없었다.

    Janome는 이 둘을 품사로 명확히 가른다 — 문형은 助動詞, 어휘는 形容詞
    (素晴らしい·可愛らしい·男らしい 모두 形容詞 한 토큰으로 분석된다).

    같은 위험이 있어 보이는 っぽい에는 이 방법을 쓰면 안 된다. 정상 용법인
    「言っているっぽい」도 形容詞로 분석돼서 문형 자체가 죽는다."""
    return any(tok.surface == "らしい" and tok.part_of_speech.startswith("助動詞")
               for tok in _tokenizer.tokenize(text))


# 문형 id → 존재 판정 보정 함수. found_in()이 True인 경우에만 호출된다.
_PRESENCE_CHECKS = {
    "らしい": _check_rashii,
}


def _pattern_used(pattern, text: str) -> bool:
    """문형이 지문에 실제로 쓰였는지. 리터럴 매칭이 기본이고, _PRESENCE_CHECKS에
    등록된 문형은 형태소 분석으로 한 번 더 확인한다."""
    if not pattern.found_in(text):
        return False
    refine = _PRESENCE_CHECKS.get(pattern.id)
    return refine(text) if refine else True


# 문형 id → 검증 함수. 두 인자(text, term) 또는 (text)만 받는 함수를 통일해서 다룬다.
_EXTRA_CHECKS = {
    "ないことには": lambda t: _check_pair_negative(t, "ないことには"),
    "なくしては": lambda t: _check_pair_negative(t, "なくしては"),
    "いかんによって": lambda t: _check_result_variation(t, "いかんによって"),
    "あげく": lambda t: _check_hardship_after(t, "あげく"),
    "ばかりに": lambda t: _check_hardship_after(t, "ばかりに"),
    "くせに": lambda t: _check_critical_tone(t, "くせに"),
    "にかたくない": lambda t: (
        _check_collocate_before(t, "にかたくない", ["想像", "推察", "察する", "理解"])
        or _check_collocate_before(t, "に難くない", ["想像", "推察", "察する", "理解"])
    ),
    "を禁じ得ない": lambda t: _check_collocate_before(
        t, "を禁じ得ない", ["涙", "怒り", "驚き", "失望", "感動", "悲しみ"]
    ),
    "きらいがある": lambda t: _check_kirai_no_double_softening(t),
    "そうだ(伝聞)": lambda t: _check_sou_da(t, "伝聞"),
    "そうだ(様態)": lambda t: _check_sou_da(t, "様態"),
}


def validate_passage(deps: ports.Deps, passage: str, patterns: list):
    """(통과 여부, 실패 사유) 튜플을 반환한다. 실패 사유는 사람이 읽고 바로
    원인을 알 수 있는 짧은 문자열로, 실패 알림 메일에 그대로 실린다."""
    if not passage:
        return False, "빈 지문(파싱 실패)"
    stripped = passage.rstrip()
    if not stripped.endswith("。"):
        return False, "생성 중간에 잘림(마지막 문장이 완성되지 않음)"
    sentence_count = passage.count("。")
    s_min, s_max = deps.settings.sentence_min, deps.settings.sentence_max
    if not (s_min <= sentence_count <= s_max):
        reason = f"문장 수 {sentence_count}개 — 범위({s_min}~{s_max}) 벗어남"
        deps.log(f"[검증] {reason}")
        return False, reason
    normalized = _normalize(passage)
    missing = [p.id for p in patterns if not _pattern_used(p, normalized)]
    if missing:
        reason = f"문형 누락: {', '.join(missing)}"
        deps.log(f"[검증] {reason}")
        return False, reason
    structural_fail = [
        p.id for p in patterns
        if p.id in _EXTRA_CHECKS and not _EXTRA_CHECKS[p.id](normalized)
    ]
    if structural_fail:
        reason = f"구조 조건 미충족: {', '.join(structural_fail)}"
        deps.log(f"[검증] {reason}")
        return False, reason
    return True, ""


# ── 자연스러움 판정 (LLM 교차 검증) ───────────────────────────
# validate_passage()는 문장 수·문형 존재 여부·일부 구조 제약(_EXTRA_CHECKS 11개)만
# 본다. 89개 중 _EXTRA_CHECKS가 없는 78개는 규칙 기반 검증이 불가능하다고 이미
# 판단된 것들이라, 생성과 다른 모델로 한 번 더 문법 오류 여부만 확인한다.
def build_judge_prompt(passage: str, patterns: list) -> str:
    """생성에 쓴 문형 목록에 대해서만 개별로 '문법적으로 틀렸는가'를 묻는다.
    '자연스러운가'를 묻지 않는다 — 그러면 어휘 선택 취향까지 반려 대상이 된다."""
    lines = []
    for p in patterns:
        if p.note:
            lines.append(f"- {p.id} — 사용 규칙: {p.note}")
        else:
            lines.append(f"- {p.id}")
    pattern_list = "\n".join(lines)
    return f"""次の日本語の文章の中で、以下の文型がそれぞれ「文法的に誤って」
使われていないか確認してください。

【確認対象の文型】
{pattern_list}

【判定基準 — 重要】
- 「文法的に間違っている」場合のみ NG とすること
  (例: 「あげく」の後にポジティブな結末が来ている、等)
- 「文法的には正しいが、もっと自然な言い方がある」場合は NG にしない。
  そのケースは note 欄に参考として書いてよいが、ok は true のままにする
  (例: 「さえ」でも文法的には正しいが「まで」の方が自然、というのは NG ではない)
- 迷った場合は NG にしない(過剰検出を避けるため)

【出力形式 — 必ずこの通りに、他の文章は一切書かない】
各文型について1行ずつ、次の形式で出力すること:
文型名|true または false|理由(50字以内、問題なければ空欄可)

【文章】
{passage}"""


def parse_judge_output(raw: str, patterns: list) -> dict:
    """judge 출력을 {pattern_id: {"ok": bool, "note": str|None}} 형태로 파싱.
    파싱 실패한 줄은 판정 불가로 보고 ok=True(관대한 쪽)로 처리한다 —
    판정 단계 자체의 파싱 오류가 발송을 막아서는 안 된다."""
    result = {p.id: {"ok": True, "note": None} for p in patterns}  # 기본값: 통과
    for line in raw.strip().split("\n"):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        pid = parts[0].strip()
        if pid not in result:
            continue
        ok = parts[1].strip().lower() != "false"
        note = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        result[pid] = {"ok": ok, "note": note}
    return result


def judge_naturalness(deps: ports.Deps, passage: str, patterns: list) -> dict:
    """실패해도(호출 실패, 파싱 실패) 전부 ok=True를 반환해 발송을 막지 않는다.
    판정은 방어선이지 필수 관문이 아니다 — 판정 자체의 장애가 서비스 전체를
    멈추게 해서는 안 된다."""
    prompt = build_judge_prompt(passage, patterns)
    # temperature 0: 판정은 일관성이 먼저
    raw = deps.llm.generate(prompt, temperature=0.0, model=deps.settings.judge_model)
    if not raw:
        deps.log("[판정] 호출 실패 — 판정 생략하고 통과 처리")
        return {p.id: {"ok": True, "note": "판정 호출 실패"} for p in patterns}
    return parse_judge_output(raw, patterns)


def check_extra_structural(passage: str, patterns: list) -> dict:
    """_EXTRA_CHECKS 결과를 문형별로 {id: bool} 형태로 반환한다.
    validate_passage()의 통과/실패 판정 로직은 건드리지 않고,
    그림자 비교에 쓸 원시 결과만 별도로 뽑아낸다."""
    normalized = _normalize(passage)
    return {
        p.id: _EXTRA_CHECKS[p.id](normalized)
        for p in patterns
        if p.id in _EXTRA_CHECKS
    }


def compare_judgments(code_results: dict, judge_result: dict) -> dict:
    """code_results: {id: bool}, judge_result: {id: {"ok": bool, ...}}."""
    comparison = {}
    for pid, code_ok in code_results.items():
        judge_ok = judge_result.get(pid, {}).get("ok")
        if judge_ok is None:
            continue
        comparison[pid] = {
            "code_ok": code_ok,
            "judge_ok": judge_ok,
            "agree": code_ok == judge_ok,
        }
    return comparison


def generate_passage(deps: ports.Deps, category: dict, history: dict):
    patterns = select_patterns(deps, category, history)
    temperatures = [0.7, 0.6, 0.4, 0.2]
    attempts_log = []  # 실패 알림 메일에 그대로 실릴 시도별 진단 정보
    shadow_log = []     # 코드 검증 vs LLM 판정 불일치 기록 (발송 여부에 영향 없음)
    for attempt in range(deps.settings.max_gen_attempts):
        if attempt == 2:
            # 두 번 실패하면 문형 조합 자체를 바꿔서 재시도. 직전 조합을
            # exclude_ids로 넘기지 않으면 실패 원인 문형이 그대로 다시 뽑혀
            # 남은 시도까지 같은 지뢰를 밟는다(2026-09-13 かえって〜てしまう가
            # 재선정 후에도 유임되어 4회 전부 실패).
            deps.log("[재시도] 문형 조합 교체")
            patterns = select_patterns(deps, category, history,
                                       exclude_ids={p.id for p in patterns})
        prompt = build_prompt(deps, patterns, category["level_tag"])
        raw = deps.llm.generate(prompt, temperatures[attempt])
        topic, passage = parse_gemini_output(raw)
        ok, reason = (validate_passage(deps, passage, patterns) if passage
                      else (False, "Gemini 출력 파싱 실패(--- 구분자 없음)"))

        # 그림자 비교: validate_passage()의 통과/실패와 무관하게, 지문이 있으면
        # 항상 판정과 코드 검증을 나란히 실행해 불일치를 기록한다(재시도 여부에는
        # 영향 없음). 코드가 실격시킨 케이스에서 LLM이 어떻게 판단하는지가
        # 이 비교의 핵심 데이터다.
        judgment = None
        if passage:
            judgment = judge_naturalness(deps, passage, patterns)
            code_results = check_extra_structural(passage, patterns)
            comparison = compare_judgments(code_results, judgment)
            for pid, c in comparison.items():
                if not c["agree"]:
                    shadow_log.append({
                        "pattern": pid,
                        "code_ok": c["code_ok"],
                        "judge_ok": c["judge_ok"],
                        "snippet": (passage or "")[:300],
                    })

        if ok:
            failed = {pid: v for pid, v in judgment.items() if not v["ok"]} if judgment else {}
            if failed:
                ok = False
                reason = "자연스러움 판정 실패: " + ", ".join(failed.keys())

        if ok:
            deps.log(f"[생성] {attempt + 1}번째 시도에서 성공")
            return topic or "日本語の読み物", passage, patterns, attempts_log, shadow_log
        # 실패 사유(문형 누락/구조 미충족/문장 수)는 validate_passage가 이미
        # 로그에 남기지만, 정작 원문이 없으면 "왜" 실패했는지 사후에 알 수 없다.
        # 그래서 실패한 시도마다 원문 전체를 로그에 같이 남기고, 실패 알림
        # 메일에 그대로 실릴 수 있게 구조화된 형태로도 모아둔다.
        snippet = passage if passage else raw
        if passage:
            deps.log(f"[생성] {attempt + 1}번째 시도 원문(검증 실패):\n{passage}")
        else:
            deps.log(f"[생성] {attempt + 1}번째 시도: Gemini 출력 파싱 실패. raw 응답:\n{raw!r}")
        deps.log(f"[생성] {attempt + 1}번째 시도 실패")
        attempts_log.append({
            "attempt": attempt + 1,
            "patterns": [p.id for p in patterns],
            "reason": reason,
            "snippet": (snippet or "")[:300],
            "judgment": judgment,   # 신규 필드. None이면 판정 단계 전에 실패한 것
        })
    deps.log("[생성] 전체 시도 실패 — 발송 중단")
    return None, None, patterns, attempts_log, shadow_log


# ── PDF / 메일 템플릿 (japanese-study 원본 형식) ───────────
WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# PDF/HTML은 header_lines() 대신 build_pdf/build_html 안에서 직접 스타일링한다.


def split_sentences(passage: str) -> list:
    """'。' 기준으로 문장을 나눈다. 원본 템플릿처럼 문장마다 한 줄로 표시하기 위함."""
    parts = [s.strip() for s in passage.split("。") if s.strip()]
    return [s + "。" for s in parts]


class ReadingPDF(FPDF):
    def __init__(self, font_regular: str, font_bold: str):
        super().__init__()
        self.add_font("JP", "", font_regular)
        self.add_font("JP", "B", font_bold)
        self.set_auto_page_break(auto=True, margin=18)


def build_pdf(deps: ports.Deps, today: datetime.date, topic: str,
              passage: str) -> str:
    font_regular = deps.fonts.find("")
    font_bold = deps.fonts.find("B")
    pdf = ReadingPDF(font_regular, font_bold)
    pdf.add_page()
    page_w = pdf.w - pdf.l_margin - pdf.r_margin
    week_no = today.isocalendar()[1]
    date_line = f"{today.isoformat()} ({WEEKDAY_EN[today.weekday()]}) | Week {week_no}"

    # 메인 타이틀 — 크게, 굵게, 중앙 정렬
    pdf.set_font("JP", "B", 20)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(page_w, 12, "日本語学習 表現読解", align="C",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # 날짜 줄 — 중앙 정렬, 회색
    pdf.set_font("JP", "", 11)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(page_w, 7, date_line, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # 구분선
    pdf.set_draw_color(210, 210, 210)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(8)

    # 테마 — 옅은 회색
    pdf.set_font("JP", "", 11)
    pdf.set_text_color(150, 150, 150)
    pdf.multi_cell(page_w, 7, f"テーマ: {topic}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)

    # 본문
    pdf.set_font("JP", "", 12)
    pdf.set_text_color(20, 20, 20)
    for sentence in split_sentences(passage):
        pdf.multi_cell(page_w, 8.5, sentence, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    output_path = os.path.join(BASE_DIR, f"JPN_{today.isoformat()}_表現読解.pdf")
    pdf.output(output_path)
    deps.log(f"[PDF] 생성 완료: {output_path}")
    return output_path


def build_html(today: datetime.date, topic: str, passage: str) -> str:
    week_no = today.isocalendar()[1]
    date_line = f"{today.isoformat()} ({WEEKDAY_EN[today.weekday()]}) | Week {week_no}"
    sentences_html = "<br><br>".join(split_sentences(passage))
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:24px;
background:#fafafa;font-family:'Helvetica Neue',Arial,'Noto Sans JP',sans-serif;color:#222">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:32px;border-radius:6px">
<h1 style="text-align:center;font-size:22px;font-weight:700;margin:0 0 6px">日本語学習 表現読解</h1>
<div style="text-align:center;font-size:13px;color:#888;margin-bottom:16px">{date_line}</div>
<hr style="border:none;border-top:1px solid #eee;margin:0 0 16px">
<div style="font-size:13px;color:#999;margin-bottom:20px">テーマ: {topic}</div>
<div style="font-size:16px;line-height:2">{sentences_html}</div>
</div></body></html>"""


def build_subject(today: datetime.date) -> str:
    return f"日本語学習 表現読解 · {today.isoformat()} ({WEEKDAY_EN[today.weekday()]})"


# ── 메일 발송 ──────────────────────────────────────────
def _mask_email(addr: str) -> str:
    """run_log.txt는 GitHub Actions 아티팩트로 매 실행마다 올라가고 이 리포는
    public이라, 로그에 실제 이메일을 그대로 남기면 그때마다 노출된다."""
    if "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    masked = local[0] + "*" * max(len(local) - 2, 1) + (local[-1] if len(local) > 2 else "")
    return f"{masked}@{domain}"


def resolve_recipients(deps: ports.Deps) -> list:
    """수동 실행에 단일 수신자가 지정됐으면 그쪽만, 아니면 구독자 전체."""
    if deps.mode.manual and deps.mode.manual_mail_to:
        deps.log(f"[메일] 수동 실행 — 수신자 고정: "
                 f"{_mask_email(deps.mode.manual_mail_to)}")
        return [deps.mode.manual_mail_to]
    return deps.secrets.recipient_list()


def send_mail(deps: ports.Deps, subject: str, html: str, pdf_path: str) -> bool:
    if not deps.secrets.can_send_mail():
        deps.log("[메일] 인증 정보 없음 — 발송 생략")
        return False
    recipients = resolve_recipients(deps)
    if not recipients:
        deps.log("[메일] 수신자 없음 — 발송 생략")
        return False
    ok = deps.mailer.send(subject, html, recipients, pdf_path)
    if ok:
        deps.log(f"[메일] 발송 완료 → "
                 f"{', '.join(_mask_email(r) for r in recipients)}")
    return ok


def _plain_to_html(lines: list) -> str:
    """운영자 알림은 원래 plain text였다. Mailer 포트는 본문을 html로 받으므로
    <pre>로 감싸 줄바꿈과 정렬을 그대로 유지한다(메일 내용 자체는 동일)."""
    import html as _html
    return "<pre style=\"font-family:monospace;white-space:pre-wrap\">" + \
        _html.escape("\n".join(lines)) + "</pre>"


def notify_admin_failure(deps: ports.Deps, reason: str, attempts_log: list = None,
                         repeat_offenders: list = None):
    """지문 생성 실패 등으로 오늘 메일링을 못 보낸 경우, 운영자(발신 계정 본인)에게
    실패 사실을 알린다. 이게 없으면 워크플로 로그를 직접 열어보지 않는 이상
    실패가 조용히 묻힌다.

    attempts_log가 있으면(지문 생성 실패의 경우) 시도별 진단 정보(문형·사유·지문
    일부)를 메일 본문에 그대로 담는다. 이러면 GitHub Actions 로그를 따로 열어보지
    않고, 이 메일 내용만 그대로 옮겨서 진단을 요청할 수 있다.

    repeat_offenders가 있으면, 최근 며칠간 반복적으로 실패에 관여한 문형을
    경고로 먼저 보여준다 — 오늘 실패가 우연인지 누적된 문제인지 바로 판단할 수 있다."""
    if not deps.secrets.can_send_mail():
        deps.log("[실패 알림] 인증 정보 없음 — 알림 생략")
        return
    today = _send_date(deps)
    subject = f"[운영 알림] 표현독해 발송 실패 — {today.isoformat()}"
    lines = [
        f"오늘({today.isoformat()}) 일본어 표현독해 메일링이 실패해서 발송되지 않았습니다.",
        "",
        f"사유: {reason}",
        "",
    ]
    if repeat_offenders:
        lines.append(
            f"⚠ 반복 경고: {', '.join(repeat_offenders)}는(은) 최근 "
            f"{deps.settings.failure_repeat_window}회 실행 중 "
            f"{deps.settings.failure_repeat_threshold}회 이상 "
            f"실패에 관여했습니다. 지침 보강이 필요할 수 있습니다."
        )
        lines.append("")
    if attempts_log:
        lines.append("=== 시도별 진단 ===")
        for a in attempts_log:
            lines.append(f"[{a['attempt']}차 시도]")
            lines.append(f"  문형: {', '.join(a['patterns'])}")
            lines.append(f"  실패 사유: {a['reason']}")
            judgment = a.get("judgment")
            if judgment:
                for pid, v in judgment.items():
                    if not v["ok"]:
                        detail = f": {v['note']}" if v["note"] else ""
                        lines.append(f"  [판정-실격] {pid}{detail}  "
                                     f"(판정 모델: {deps.settings.judge_model})")
                for pid, v in judgment.items():
                    if v["ok"] and v["note"]:
                        lines.append(f"  [판정-참고, 발송에는 영향 없음] {pid}: {v['note']}")
            lines.append(f"  지문 일부: {a['snippet']}")
            lines.append("")
        lines.append("(이 내용을 그대로 복사해서 진단을 요청하면 됩니다)")
    else:
        lines.append("자세한 내용은 GitHub Actions 실행 로그(run_log.txt)를 확인하세요.")
    admin = deps.secrets.gmail_address
    if deps.mailer.send(subject, _plain_to_html(lines), [admin]):
        deps.log(f"[실패 알림] 발송 완료 → {_mask_email(admin)}")
    else:
        deps.log("[실패 알림] 발송 자체도 실패")


# ── 이력 커밋 (성공한 경우에만 호출됨) ──────────────────
def commit_state(deps: ports.Deps, name: str, label: str, today: datetime.date):
    """상태 파일 하나를 커밋·푸시한다.

    이전에는 used_history/failure_history/shadow_review마다 같은 함수가
    복사돼 있었다(커밋 메시지와 파일 경로만 달랐다). 절차가 바뀌면 세 곳을
    모두 고쳐야 해서 어긋나기 쉬웠으므로 하나로 합쳤다."""
    ok = deps.vcs.commit_and_push(
        [deps.store.path_of(name)],
        f"chore: update {label} ({today.isoformat()})",
    )
    if ok:
        deps.log(f"[{label}] 커밋 및 푸시 완료")


def send_weekly_shadow_report(deps: ports.Deps, review: dict, today: datetime.date):
    """매주 월요일에, human_label이 아직 null인 지난 7일 불일치 사례를 모아
    운영자에게 요약 메일을 보낸다. 라벨링(code_wrong/judge_wrong/ambiguous)은
    사람이 이 메일을 보고 shadow_review.json을 직접 고치거나, 다음 claude.ai
    대화에서 "이 리포트 라벨링해줘"로 위임하는 방식으로 처리한다."""
    if today.weekday() != 0:
        return
    if not deps.secrets.can_send_mail():
        deps.log("[주간리포트] 인증 정보 없음 — 발송 생략")
        return
    cutoff = today - datetime.timedelta(days=7)
    pending = [
        e for e in review.get("entries", [])
        if e.get("human_label") is None
        and datetime.date.fromisoformat(e["date"]) >= cutoff
    ]
    if not pending:
        deps.log("[주간리포트] 지난주 미라벨링 불일치 없음 — 발송 생략")
        return
    subject = f"[운영 알림] 판정 불일치 주간 리뷰 — {today.isoformat()}"
    lines = [
        f"지난 7일간 코드 검증과 LLM 판정이 갈린 사례 {len(pending)}건입니다.",
        "shadow_review.json에서 human_label을 \"code_wrong\"/\"judge_wrong\"/\"ambiguous\" 중 하나로 채워주세요.",
        "",
    ]
    for e in pending:
        lines.append(f"[{e['date']} · {e['category']} · {e['pattern']}]")
        lines.append(f"  코드 판정: {'통과' if e['code_ok'] else '실격'} / LLM 판정: {'통과' if e['judge_ok'] else '실격'}")
        lines.append(f"  지문 일부: {e['snippet']}")
        lines.append("")
    admin = deps.secrets.gmail_address
    if deps.mailer.send(subject, _plain_to_html(lines), [admin]):
        deps.log(f"[주간리포트] 발송 완료 → {_mask_email(admin)} ({len(pending)}건)")
    else:
        deps.log("[주간리포트] 발송 실패")


# ── 실행 ──────────────────────────────────────────────
def main(deps: ports.Deps) -> bool:
    """실행 성공 여부(bool)를 반환한다. 지문 생성 실패나 메일 발송 실패는
    False를 반환해서, 호출부가 워크플로 실패로 표시할 수 있게 한다.
    이전에는 실패해도 그냥 return만 해서 GitHub Actions가 '성공'으로
    표시하는 바람에 발송 실패가 조용히 묻힌 적이 있었다.

    deps를 인자로 받는 이유는 이 함수가 테스트 대상이기 때문이다. 운영에서는
    __main__ 블록이 adapters.build_production_deps()로 실제 구현을 만들어
    넘기고, 테스트는 가짜 구현을 넘겨 네트워크 없이 전 구간을 돌린다."""
    today = _send_date(deps)

    # 주 5일(월~금) 발송. cron을 "0 11 * * 1-5"로 한정했지만 그것만으로는
    # workflow_dispatch로 주말에 돌렸을 때를 막지 못하고, cron 요일 필드가
    # 다시 넓어지면 조용히 주말 발송이 부활한다. 그래서 코드에도 가드를 둔다.
    # 수동 실행은 의도적으로 통과시킨다(주말에 한 편 더 받고 싶을 수 있다).
    if today.weekday() >= 5 and not deps.mode.manual:
        deps.log(f"[중단] {today} 은 주말 — 주 5일 발송 정책에 따라 실행하지 않음")
        return True

    category = pick_category(deps, today)
    history = load_history(deps)

    topic, passage, patterns, attempts_log, shadow_log = generate_passage(
        deps, category, history)

    # 그림자 비교 기록은 발송 성공/실패와 무관하게 매 실행 후 저장·커밋한다.
    shadow_review = load_shadow_review(deps)
    append_shadow_review(deps, shadow_review, category["key"], shadow_log, today)
    commit_state(deps, ports.SHADOW_REVIEW, "그림자비교", today)
    send_weekly_shadow_report(deps, shadow_review, today)

    if not passage:
        deps.log("[중단] 지문 생성 실패로 발송하지 않음")
        fail_history = load_failure_history(deps)
        repeat_offenders = find_repeat_offenders(deps, fail_history)
        if repeat_offenders:
            deps.log(f"[실패이력] 반복 실패 문형 감지: {', '.join(repeat_offenders)}")
        append_failure_history(deps, fail_history, category["key"], attempts_log, today)
        commit_state(deps, ports.FAILURE_HISTORY, "실패이력", today)
        notify_admin_failure(
            deps,
            f"지문 생성 {deps.settings.max_gen_attempts}회 시도 전부 실패 (검증 조건 미충족)",
            attempts_log,
            repeat_offenders,
        )
        return False

    pdf_path = ""
    try:
        pdf_path = build_pdf(deps, today, topic, passage)
    except FileNotFoundError as e:
        deps.log(f"[PDF] 생략: {e}")

    html = build_html(today, topic, passage)
    subject = build_subject(today)
    sent = send_mail(deps, subject, html, pdf_path)

    if sent:
        if deps.mode.manual:
            deps.log("[이력] 수동 실행 — 이력 갱신 생략")
        else:
            append_history(deps, history, category["key"], patterns, today)
            commit_state(deps, ports.HISTORY, "이력", today)
        return True

    deps.log("[이력] 발송 실패 — 이력 갱신하지 않음 (다음 실행에서 같은 후보 유지)")
    notify_admin_failure(deps, "지문 생성은 성공했으나 메일 발송(SMTP) 단계에서 실패")
    return False


if __name__ == "__main__":
    import adapters   # 운영 구현은 진입점에서만 import한다(테스트는 가짜를 주입)

    _deps = adapters.build_production_deps(BASE_DIR)
    if not main(_deps):
        _deps.log("[종료] 실패 처리 — 워크플로를 실패(빨간 X)로 표시하기 위해 exit(1)")
        sys.exit(1)
