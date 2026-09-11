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
import json
import time
import random
import smtplib
import datetime
import subprocess
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from janome.tokenizer import Tokenizer

try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

import grammar_bank as GB

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 환경변수 ───────────────────────────────────────────
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PASSWORD")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
EMAIL_RECIPIENTS = os.environ.get("EMAIL_RECIPIENTS", "")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    GMAIL_ADDRESS = GMAIL_ADDRESS or os.getenv("GMAIL_ADDRESS")
    GMAIL_APP_PW = GMAIL_APP_PW or os.getenv("GMAIL_APP_PASSWORD")
    GEMINI_API_KEY = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
    EMAIL_RECIPIENTS = EMAIL_RECIPIENTS or os.getenv("EMAIL_RECIPIENTS", "")
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "used_history.json")
FAILURE_HISTORY_FILE = os.path.join(BASE_DIR, "failure_history.json")
SHADOW_REVIEW_FILE = os.path.join(BASE_DIR, "shadow_review.json")
RUN_LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")

MANUAL_RUN = os.environ.get("MANUAL_RUN") == "1"
MANUAL_MAIL_TO = os.environ.get("MANUAL_MAIL_TO", "")

COOLDOWN_RUNS = 3     # 같은 카테고리에서 최근 N회 안에 쓰인 문형은 제외
PATTERNS_PER_DAY = 5  # 하루 지문에 쓰는 문형 개수
SENTENCE_MIN, SENTENCE_MAX = 10, 20
MAX_GEN_ATTEMPTS = 4
FAILURE_REPEAT_WINDOW = 5    # 최근 N회 실행 중에서 반복 여부를 판단
FAILURE_REPEAT_THRESHOLD = 3  # 그 안에서 이 횟수 이상 실패하면 "반복 경고"


def _rlog(msg: str):
    print(msg)
    try:
        with open(RUN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except OSError:
        pass


def _today_kst() -> datetime.date:
    """카테고리 선택(pick_category)과 주간 리포트 요일 판정(send_weekly_shadow_report)은
    독자가 메일을 받는 KST 기준 날짜여야 한다. GitHub Actions 러너는 기본 UTC라서
    datetime.date.today()를 그대로 쓰면 워크플로가 실제로 도는 시점(cron "0 22 * * *"
    = UTC 22:00 = KST 07:00 다음날)의 요일이 하루 밀린다."""
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).date()


# ── 카테고리 선정 ──────────────────────────────────────
def pick_category(today: datetime.date) -> dict:
    forced = os.environ.get("FORCE_CATEGORY", "").strip()
    if forced:
        cat = GB.CATEGORY_BY_KEY.get(forced)
        if cat:
            _rlog(f"[카테고리] 강제 지정: {cat['key']}")
            return cat
    weekday = today.weekday()
    cat = GB.CATEGORY_BY_WEEKDAY.get(weekday)
    if cat is None:
        # 주말 등 정의 안 된 요일 — 안전하게 월요일 카테고리로 대체
        cat = GB.CATEGORY_BY_WEEKDAY[0]
        _rlog(f"[카테고리] {today} 은 순환표에 없는 요일 — 기본값으로 대체: {cat['key']}")
    else:
        _rlog(f"[카테고리] {today} → {cat['key']} (내부 로그 전용, 메일엔 비노출)")
    return cat


# ── 쿨다운 이력 ────────────────────────────────────────
def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {"runs": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        _rlog("[이력] 파일 손상 또는 없음 — 새로 시작")
        return {"runs": []}


# ── 실패 이력 (반복 실패 문형을 자동으로 감지하기 위한 별도 기록) ──────
def load_failure_history() -> dict:
    if not os.path.exists(FAILURE_HISTORY_FILE):
        return {"runs": []}
    try:
        with open(FAILURE_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        _rlog("[실패이력] 파일 손상 또는 없음 — 새로 시작")
        return {"runs": []}


def append_failure_history(fail_history: dict, category_key: str,
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
    fail_history["runs"] = fail_history["runs"][-60:]
    with open(FAILURE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(fail_history, f, ensure_ascii=False, indent=2)


# ── 그림자 비교 (코드 검증 vs LLM 판정 불일치 기록) ──────────────
def load_shadow_review() -> dict:
    if not os.path.exists(SHADOW_REVIEW_FILE):
        return {"entries": []}
    try:
        with open(SHADOW_REVIEW_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        _rlog("[그림자비교] 파일 손상 또는 없음 — 새로 시작")
        return {"entries": []}


def append_shadow_review(review: dict, category_key: str, shadow_log: list, today: datetime.date):
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
    with open(SHADOW_REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)


def find_repeat_offenders(fail_history: dict) -> list:
    """최근 FAILURE_REPEAT_WINDOW회의 실패 기록 안에서, 특정 문형이
    FAILURE_REPEAT_THRESHOLD회 이상 등장했으면 "반복 실패"로 판정한다.
    문형이 실제로 실패에 관여했다는 것만 셀 뿐, 매번 같은 사유인지는
    구분하지 않는다 — 사유가 달라도 그 문형이 계속 말썽이라는 신호는 유효하다."""
    recent_runs = fail_history.get("runs", [])[-FAILURE_REPEAT_WINDOW:]
    counts = {}
    for run in recent_runs:
        seen_today = set()
        for f in run.get("failures", []):
            pid = f["pattern"]
            if pid in seen_today:
                continue  # 같은 날 같은 문형은 한 번만 카운트(시도 4번 다 중복 집계 방지)
            seen_today.add(pid)
            counts[pid] = counts.get(pid, 0) + 1
    return [pid for pid, c in counts.items() if c >= FAILURE_REPEAT_THRESHOLD]


def recently_used(history: dict, category_key: str) -> set:
    matching = [r for r in history.get("runs", []) if r.get("category") == category_key]
    recent = matching[-COOLDOWN_RUNS:]
    used = set()
    for r in recent:
        used.update(r.get("patterns", []))
    return used


# 뒷문장에 특정 결과(부정형 결론/나쁜 결과/갈리는 결과)가 반드시 와야 완성되는
# 문형들. 이런 문형이 한 지문에 2개 이상 겹치면, 서로 다른 결말 구조를
# 동시에 자연스럽게 짜야 해서 실패율이 올라간다(실제로 ないことには+ばかりに,
# なくしては+いかんによって 조합에서 확인됨). 그래서 하루에 최대 1개만 뽑는다.
_RESULT_FORCING_IDS = {"あげく", "ばかりに", "ないことには", "なくしては", "いかんによって"}


def select_patterns(category: dict, history: dict, exclude_ids=None) -> list:
    exclude_ids = exclude_ids or set()
    used = recently_used(history, category["key"]) | exclude_ids
    pool = category["patterns"]
    candidates = [p for p in pool if p.id not in used]
    if len(candidates) < PATTERNS_PER_DAY + 2:
        _rlog(f"[쿨다운] 후보 부족({len(candidates)}개) — 쿨다운 무시하고 전체 풀 사용")
        candidates = pool

    constrained = [p for p in candidates if p.id in _RESULT_FORCING_IDS]
    free = [p for p in candidates if p.id not in _RESULT_FORCING_IDS]

    picked = []
    if constrained:
        picked.append(random.choice(constrained))
    remaining = PATTERNS_PER_DAY - len(picked)
    if len(free) >= remaining:
        picked += random.sample(free, remaining)
    else:
        # free 풀이 부족한 예외적 경우 — constrained에서 마저 채움(모자란 만큼만)
        picked += free
        leftover = [p for p in constrained if p not in picked]
        picked += random.sample(leftover, min(PATTERNS_PER_DAY - len(picked), len(leftover)))

    _rlog(f"[문형] 선택됨: {', '.join(p.id for p in picked)}")
    return picked


def append_history(history: dict, category_key: str, patterns: list, today: datetime.date):
    history.setdefault("runs", []).append({
        "date": today.isoformat(),
        "category": category_key,
        "patterns": [p.id for p in patterns],
    })
    # 무한정 커지지 않도록 최근 60회만 보존
    history["runs"] = history["runs"][-60:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ── Gemini 호출 ────────────────────────────────────────
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash"]


def _call_gemini(prompt: str, temperature: float, model: str = None) -> str:
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return ""
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    for model_id in ([model] if model else _GEMINI_MODELS):
        for attempt in range(2):
            try:
                cfg = {"temperature": temperature, "max_output_tokens": 1500}
                if "2.5" in model_id:
                    cfg["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
                res = client.models.generate_content(
                    model=model_id, contents=prompt,
                    config=genai_types.GenerateContentConfig(**cfg),
                )
                return res.text or ""
            except Exception as e:
                err = str(e)
                is_quota = "429" in err or "quota" in err.lower()
                if is_quota and attempt == 0:
                    m = re.search(r"retry in (\d+(?:\.\d+)?)", err)
                    wait = int(float(m.group(1))) + 5 if m else 60
                    _rlog(f"[Gemini] {model_id} 한도 초과. {wait}초 대기 후 재시도")
                    time.sleep(wait)
                    continue
                if is_quota:
                    time.sleep(10)
                    break
                if ("503" in err or "UNAVAILABLE" in err) and attempt == 0:
                    time.sleep(30)
                    continue
                _rlog(f"[Gemini] {model_id} 오류(폴백 전환): {e}")
                break
    return ""


def build_prompt(patterns: list, level_tag: str) -> str:
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
3. 三行目以降の文章は、必ず{SENTENCE_MIN}文以上{SENTENCE_MAX}文以内(句点「。」の数で数える)にすること。目安ではなく絶対条件であり、多すぎても少なすぎても失格とする。文型をすべて入れることよりもこの文数制限を優先せよ。目標は{SENTENCE_MIN + 2}文前後(下限ギリギリを狙わない)。よくある失敗は、必要な文型を使い終えた時点で話を早々にまとめてしまい、{SENTENCE_MIN}文に届かないまま終わることである。それを避けるため、出来事の経緯・心情の変化・具体的なエピソードを1つ以上追加で描写し、話を十分に展開させること。書き終える前に句点の数を実際に数え、{SENTENCE_MIN}文未満なら具体的な描写を足してから出力すること
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


def _normalize(text: str) -> str:
    text = _YUENI_RE.sub("ゆえに", text)
    for kanji, kana in _KANJI_VARIANT_TO_KANA.items():
        text = text.replace(kanji, kana)
    for kanji, kana in _KANJI_TO_KANA.items():
        text = text.replace(kanji, kana)
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
    "体調を崩す", "落ち込む",
]
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


def _check_pair_negative(text: str, term: str) -> bool:
    """〜ないことには, 〜なくしては: 뒤에 부정형 결론이 와야 짝이 완성됨.
    ない/できない는 아주 흔한 단어라 창을 너무 넓히면 검증이 사실상 무의미해진다
    (아무 문장에나 ない가 하나쯤 있기 마련이라). 그래서 다른 검증보다 창을 좁게 유지한다."""
    window = _window_after(text, term, span=45)
    return any(w in window for w in _NEG_RESULT_WORDS)


def _check_result_variation(text: str, term: str) -> bool:
    """〜いかんによって: 뒤에 결과가 갈린다는 서술이 와야 함."""
    window = _window_after(text, term, span=60)
    return any(w in window for w in _VARIATION_WORDS)


def _check_hardship_after(text: str, term: str) -> bool:
    """〜あげく, 〜ばかりに: 뒤에 부정적 결과가 와야 함. 결과 어휘가
    상대적으로 특이한 단어들이라(ない처럼 아무 데나 나오지 않음), 창을
    넓혀도 오탐 위험이 크지 않다고 판단해 다른 검증보다 넓게 잡는다."""
    window = _window_after(text, term, span=70)
    return any(w in window for w in _HARDSHIP_WORDS)


def _check_critical_tone(text: str, term: str) -> bool:
    """〜くせに: 완벽한 의미 판단은 불가능하므로, 앞뒤에 비판적 어조 어휘가
    최소한 하나라도 있는지만 느슨하게 확인한다. 통과해도 진짜 비판적 어조인지
    보장 못 하며, 이 목록에 없는 단어로 비판했다면 놓칠 수 있다 — 최소한의
    안전망일 뿐이다."""
    idx = text.find(term)
    if idx == -1:
        return False
    window = text[max(0, idx - 20): idx + len(term) + 30]
    return any(w in window for w in _CRITICAL_TONE_WORDS)


def _check_collocate_before(text: str, term: str, allowed: list) -> bool:
    """〜にかたくない, 〜を禁じ得ない: 앞에 정해진 어휘군이 와야 함."""
    before = _window_before(text, term)
    return any(a in before for a in allowed)


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
_sou_da_tokenizer = Tokenizer()


def _sou_da_prev_tokens(text: str) -> list:
    """지문에서 실제 伝聞/様態 조동사로 쓰인 'そう' 토큰들의 직전 토큰을 모아 반환한다.
    そう가 부사(그렇다/그렇게)로 쓰인 경우는 품사 필터로 걸러진다."""
    tokens = list(_sou_da_tokenizer.tokenize(text))
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


def validate_passage(passage: str, patterns: list):
    """(통과 여부, 실패 사유) 튜플을 반환한다. 실패 사유는 사람이 읽고 바로
    원인을 알 수 있는 짧은 문자열로, 실패 알림 메일에 그대로 실린다."""
    if not passage:
        return False, "빈 지문(파싱 실패)"
    sentence_count = passage.count("。")
    if not (SENTENCE_MIN <= sentence_count <= SENTENCE_MAX):
        reason = f"문장 수 {sentence_count}개 — 범위({SENTENCE_MIN}~{SENTENCE_MAX}) 벗어남"
        _rlog(f"[검증] {reason}")
        return False, reason
    normalized = _normalize(passage)
    missing = [p.id for p in patterns if not p.found_in(normalized)]
    if missing:
        reason = f"문형 누락: {', '.join(missing)}"
        _rlog(f"[검증] {reason}")
        return False, reason
    structural_fail = [
        p.id for p in patterns
        if p.id in _EXTRA_CHECKS and not _EXTRA_CHECKS[p.id](normalized)
    ]
    if structural_fail:
        reason = f"구조 조건 미충족: {', '.join(structural_fail)}"
        _rlog(f"[검증] {reason}")
        return False, reason
    return True, ""


# ── 자연스러움 판정 (LLM 교차 검증) ───────────────────────────
# validate_passage()는 문장 수·문형 존재 여부·일부 구조 제약(_EXTRA_CHECKS 11개)만
# 본다. 89개 중 _EXTRA_CHECKS가 없는 78개는 규칙 기반 검증이 불가능하다고 이미
# 판단된 것들이라, 생성과 다른 모델로 한 번 더 문법 오류 여부만 확인한다.
_JUDGE_MODEL = "gemini-3.5-flash"  # 생성이 2.5-flash이므로 다른 모델로 교차 판정


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


def judge_naturalness(passage: str, patterns: list) -> dict:
    """실패해도(호출 실패, 파싱 실패) 전부 ok=True를 반환해 발송을 막지 않는다.
    판정은 방어선이지 필수 관문이 아니다 — 판정 자체의 장애가 서비스 전체를
    멈추게 해서는 안 된다."""
    prompt = build_judge_prompt(passage, patterns)
    raw = _call_gemini(prompt, temperature=0.0, model=_JUDGE_MODEL)  # temperature 0: 판정은 일관성이 먼저
    if not raw:
        _rlog("[판정] 호출 실패 — 판정 생략하고 통과 처리")
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


def generate_passage(category: dict, history: dict):
    patterns = select_patterns(category, history)
    temperatures = [0.7, 0.6, 0.4, 0.2]
    attempts_log = []  # 실패 알림 메일에 그대로 실릴 시도별 진단 정보
    shadow_log = []     # 코드 검증 vs LLM 판정 불일치 기록 (발송 여부에 영향 없음)
    for attempt in range(MAX_GEN_ATTEMPTS):
        if attempt == 2:
            # 두 번 실패하면 문형 조합 자체를 바꿔서 재시도
            _rlog("[재시도] 문형 조합 교체")
            patterns = select_patterns(category, history)
        prompt = build_prompt(patterns, category["level_tag"])
        raw = _call_gemini(prompt, temperatures[attempt])
        topic, passage = parse_gemini_output(raw)
        ok, reason = validate_passage(passage, patterns) if passage else (False, "Gemini 출력 파싱 실패(--- 구분자 없음)")

        # 그림자 비교: validate_passage()의 통과/실패와 무관하게, 지문이 있으면
        # 항상 판정과 코드 검증을 나란히 실행해 불일치를 기록한다(재시도 여부에는
        # 영향 없음). 코드가 실격시킨 케이스에서 LLM이 어떻게 판단하는지가
        # 이 비교의 핵심 데이터다.
        judgment = None
        if passage:
            judgment = judge_naturalness(passage, patterns)
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
            _rlog(f"[생성] {attempt + 1}번째 시도에서 성공")
            return topic or "日本語の読み物", passage, patterns, attempts_log, shadow_log
        # 실패 사유(문형 누락/구조 미충족/문장 수)는 validate_passage가 이미
        # 로그에 남기지만, 정작 원문이 없으면 "왜" 실패했는지 사후에 알 수 없다.
        # 그래서 실패한 시도마다 원문 전체를 로그에 같이 남기고, 실패 알림
        # 메일에 그대로 실릴 수 있게 구조화된 형태로도 모아둔다.
        snippet = passage if passage else raw
        if passage:
            _rlog(f"[생성] {attempt + 1}번째 시도 원문(검증 실패):\n{passage}")
        else:
            _rlog(f"[생성] {attempt + 1}번째 시도: Gemini 출력 파싱 실패. raw 응답:\n{raw!r}")
        _rlog(f"[생성] {attempt + 1}번째 시도 실패")
        attempts_log.append({
            "attempt": attempt + 1,
            "patterns": [p.id for p in patterns],
            "reason": reason,
            "snippet": (snippet or "")[:300],
            "judgment": judgment,   # 신규 필드. None이면 판정 단계 전에 실패한 것
        })
    _rlog("[생성] 전체 시도 실패 — 발송 중단")
    return None, None, patterns, attempts_log, shadow_log


# ── PDF / 메일 템플릿 (japanese-study 원본 형식) ───────────
WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def find_font(style: str = "") -> str:
    """style: "" (Regular) 또는 "B" (Bold). Bold 파일이 없으면 Regular로 대체한다."""
    import glob
    env_key = "JAPANESE_FONT_PATH_BOLD" if style == "B" else "JAPANESE_FONT_PATH"
    env_font = os.environ.get(env_key) or (os.environ.get("JAPANESE_FONT_PATH") if style != "B" else None)
    if env_font and os.path.exists(env_font):
        return env_font
    patterns = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Bold*.ttc",
    ] if style == "B" else [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Regular*.ttc",
        "/usr/share/fonts/**/*CJK*Regular*.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/**/*ipag*.ttf",
    ]
    for pattern in patterns:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return sorted(hits)[0]
    if style == "B":
        return find_font("")  # Bold 못 찾으면 Regular로 대체
    raise FileNotFoundError("일본어 폰트를 찾을 수 없습니다. JAPANESE_FONT_PATH를 지정하세요.")


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


def build_pdf(today: datetime.date, topic: str, passage: str) -> str:
    font_regular = find_font("")
    font_bold = find_font("B")
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
    _rlog(f"[PDF] 생성 완료: {output_path}")
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
def send_mail(subject: str, html: str, pdf_path: str) -> bool:
    if not GMAIL_ADDRESS or not GMAIL_APP_PW:
        _rlog("[메일] 인증 정보 없음 — 발송 생략")
        return False
    if MANUAL_RUN and MANUAL_MAIL_TO:
        recipients = [MANUAL_MAIL_TO]
        _rlog(f"[메일] 수동 실행 — 수신자 고정: {MANUAL_MAIL_TO}")
    else:
        recipients = [r.strip() for r in EMAIL_RECIPIENTS.split(",") if r.strip()]
    if not recipients:
        _rlog("[메일] 수신자 없음 — 발송 생략")
        return False

    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                         filename=os.path.basename(pdf_path))
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
            server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
        _rlog(f"[메일] 발송 완료 → {', '.join(recipients)}")
        return True
    except smtplib.SMTPException as e:
        _rlog(f"[메일] 발송 실패: {e}")
        return False


def notify_admin_failure(reason: str, attempts_log: list = None, repeat_offenders: list = None):
    """지문 생성 실패 등으로 오늘 메일링을 못 보낸 경우, 운영자(발신 계정 본인)에게
    실패 사실을 알린다. 이게 없으면 워크플로 로그를 직접 열어보지 않는 이상
    실패가 조용히 묻힌다.

    attempts_log가 있으면(지문 생성 실패의 경우) 시도별 진단 정보(문형·사유·지문
    일부)를 메일 본문에 그대로 담는다. 이러면 GitHub Actions 로그를 따로 열어보지
    않고, 이 메일 내용만 그대로 옮겨서 진단을 요청할 수 있다.

    repeat_offenders가 있으면, 최근 며칠간 반복적으로 실패에 관여한 문형을
    경고로 먼저 보여준다 — 오늘 실패가 우연인지 누적된 문제인지 바로 판단할 수 있다."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PW:
        _rlog("[실패 알림] 인증 정보 없음 — 알림 생략")
        return
    today = _today_kst()
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
            f"{FAILURE_REPEAT_WINDOW}회 실행 중 {FAILURE_REPEAT_THRESHOLD}회 이상 "
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
                        lines.append(f"  [판정-실격] {pid}{detail}  (판정 모델: {_JUDGE_MODEL})")
                for pid, v in judgment.items():
                    if v["ok"] and v["note"]:
                        lines.append(f"  [판정-참고, 발송에는 영향 없음] {pid}: {v['note']}")
            lines.append(f"  지문 일부: {a['snippet']}")
            lines.append("")
        lines.append("(이 내용을 그대로 복사해서 진단을 요청하면 됩니다)")
    else:
        lines.append("자세한 내용은 GitHub Actions 실행 로그(run_log.txt)를 확인하세요.")
    body = "\n".join(lines)
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_ADDRESS
        msg["Subject"] = subject
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
            server.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], msg.as_string())
        _rlog(f"[실패 알림] 발송 완료 → {GMAIL_ADDRESS}")
    except smtplib.SMTPException as e:
        _rlog(f"[실패 알림] 발송 자체도 실패: {e}")


# ── 이력 커밋 (성공한 경우에만 호출됨) ──────────────────
def commit_history(today: datetime.date):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        _rlog("[이력] 로컬 실행 — git 커밋 생략 (파일만 저장됨)")
        return
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "add", HISTORY_FILE], check=True, cwd=BASE_DIR)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            _rlog("[이력] 변경 사항 없음 — 커밋 생략")
            return
        subprocess.run(["git", "commit", "-m", f"chore: update history ({today.isoformat()})"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "push"], check=True, cwd=BASE_DIR)
        _rlog("[이력] 커밋 및 푸시 완료")
    except subprocess.CalledProcessError as e:
        _rlog(f"[이력] 커밋 실패: {e}")


def commit_failure_history(today: datetime.date):
    """실패 이력은 성공 이력과 별도 파일이라 커밋도 별도로 한다.
    발송이 실패한 날에만 호출되므로, commit_history(성공 시 호출)와
    같은 실행에서 동시에 불릴 일은 없다."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        _rlog("[실패이력] 로컬 실행 — git 커밋 생략 (파일만 저장됨)")
        return
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "add", FAILURE_HISTORY_FILE], check=True, cwd=BASE_DIR)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            _rlog("[실패이력] 변경 사항 없음 — 커밋 생략")
            return
        subprocess.run(["git", "commit", "-m", f"chore: update failure history ({today.isoformat()})"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "push"], check=True, cwd=BASE_DIR)
        _rlog("[실패이력] 커밋 및 푸시 완료")
    except subprocess.CalledProcessError as e:
        _rlog(f"[실패이력] 커밋 실패: {e}")


def commit_shadow_review(today: datetime.date):
    """그림자 비교 기록은 발송 성공/실패와 무관하게 매 실행 후 커밋한다 —
    불일치 사례는 발송 여부와 상관없이 관찰 데이터로서 가치가 있다."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        _rlog("[그림자비교] 로컬 실행 — git 커밋 생략 (파일만 저장됨)")
        return
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "add", SHADOW_REVIEW_FILE], check=True, cwd=BASE_DIR)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            _rlog("[그림자비교] 변경 사항 없음 — 커밋 생략")
            return
        subprocess.run(["git", "commit", "-m", f"chore: update shadow review ({today.isoformat()})"],
                        check=True, cwd=BASE_DIR)
        subprocess.run(["git", "push"], check=True, cwd=BASE_DIR)
        _rlog("[그림자비교] 커밋 및 푸시 완료")
    except subprocess.CalledProcessError as e:
        _rlog(f"[그림자비교] 커밋 실패: {e}")


def send_weekly_shadow_report(review: dict, today: datetime.date):
    """매주 월요일에, human_label이 아직 null인 지난 7일 불일치 사례를 모아
    운영자에게 요약 메일을 보낸다. 라벨링(code_wrong/judge_wrong/ambiguous)은
    사람이 이 메일을 보고 shadow_review.json을 직접 고치거나, 다음 claude.ai
    대화에서 "이 리포트 라벨링해줘"로 위임하는 방식으로 처리한다."""
    if today.weekday() != 0:
        return
    if not GMAIL_ADDRESS or not GMAIL_APP_PW:
        _rlog("[주간리포트] 인증 정보 없음 — 발송 생략")
        return
    cutoff = today - datetime.timedelta(days=7)
    pending = [
        e for e in review.get("entries", [])
        if e.get("human_label") is None
        and datetime.date.fromisoformat(e["date"]) >= cutoff
    ]
    if not pending:
        _rlog("[주간리포트] 지난주 미라벨링 불일치 없음 — 발송 생략")
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
    body = "\n".join(lines)
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = GMAIL_ADDRESS
        msg["Subject"] = subject
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
            server.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], msg.as_string())
        _rlog(f"[주간리포트] 발송 완료 → {GMAIL_ADDRESS} ({len(pending)}건)")
    except smtplib.SMTPException as e:
        _rlog(f"[주간리포트] 발송 실패: {e}")


# ── 실행 ──────────────────────────────────────────────
def main() -> bool:
    """실행 성공 여부(bool)를 반환한다. 지문 생성 실패나 메일 발송 실패는
    False를 반환해서, 호출부가 워크플로 실패로 표시할 수 있게 한다.
    이전에는 실패해도 그냥 return만 해서 GitHub Actions가 '성공'으로
    표시하는 바람에 발송 실패가 조용히 묻힌 적이 있었다."""
    today = _today_kst()
    category = pick_category(today)
    history = load_history()

    topic, passage, patterns, attempts_log, shadow_log = generate_passage(category, history)

    # 그림자 비교 기록은 발송 성공/실패와 무관하게 매 실행 후 저장·커밋한다.
    shadow_review = load_shadow_review()
    append_shadow_review(shadow_review, category["key"], shadow_log, today)
    commit_shadow_review(today)
    send_weekly_shadow_report(shadow_review, today)

    if not passage:
        _rlog("[중단] 지문 생성 실패로 발송하지 않음")
        fail_history = load_failure_history()
        repeat_offenders = find_repeat_offenders(fail_history)
        if repeat_offenders:
            _rlog(f"[실패이력] 반복 실패 문형 감지: {', '.join(repeat_offenders)}")
        append_failure_history(fail_history, category["key"], attempts_log, today)
        commit_failure_history(today)
        notify_admin_failure(
            "지문 생성 4회 시도 전부 실패 (검증 조건 미충족)",
            attempts_log,
            repeat_offenders,
        )
        return False

    pdf_path = ""
    try:
        pdf_path = build_pdf(today, topic, passage)
    except FileNotFoundError as e:
        _rlog(f"[PDF] 생략: {e}")

    html = build_html(today, topic, passage)
    subject = build_subject(today)
    sent = send_mail(subject, html, pdf_path)

    if sent:
        if MANUAL_RUN:
            _rlog("[이력] 수동 실행 — 이력 갱신 생략")
        else:
            append_history(history, category["key"], patterns, today)
            commit_history(today)
        return True

    _rlog("[이력] 발송 실패 — 이력 갱신하지 않음 (다음 실행에서 같은 후보 유지)")
    notify_admin_failure("지문 생성은 성공했으나 메일 발송(SMTP) 단계에서 실패")
    return False


if __name__ == "__main__":
    if not main():
        _rlog("[종료] 실패 처리 — 워크플로를 실패(빨간 X)로 표시하기 위해 exit(1)")
        sys.exit(1)
