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
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from fpdf import FPDF
from fpdf.enums import XPos, YPos

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
RUN_LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")

MANUAL_RUN = os.environ.get("MANUAL_RUN") == "1"
MANUAL_MAIL_TO = os.environ.get("MANUAL_MAIL_TO", "")

COOLDOWN_RUNS = 3     # 같은 카테고리에서 최근 N회 안에 쓰인 문형은 제외
PATTERNS_PER_DAY = 5  # 하루 지문에 쓰는 문형 개수
SENTENCE_MIN, SENTENCE_MAX = 10, 15
MAX_GEN_ATTEMPTS = 4


def _rlog(msg: str):
    print(msg)
    try:
        with open(RUN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except OSError:
        pass


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


def recently_used(history: dict, category_key: str) -> set:
    matching = [r for r in history.get("runs", []) if r.get("category") == category_key]
    recent = matching[-COOLDOWN_RUNS:]
    used = set()
    for r in recent:
        used.update(r.get("patterns", []))
    return used


def select_patterns(category: dict, history: dict, exclude_ids=None) -> list:
    exclude_ids = exclude_ids or set()
    used = recently_used(history, category["key"]) | exclude_ids
    pool = category["patterns"]
    candidates = [p for p in pool if p.id not in used]
    if len(candidates) < PATTERNS_PER_DAY:
        _rlog(f"[쿨다운] 후보 부족({len(candidates)}개) — 쿨다운 무시하고 전체 풀 사용")
        candidates = pool
    picked = random.sample(candidates, min(PATTERNS_PER_DAY, len(candidates)))
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


def _call_gemini(prompt: str, temperature: float) -> str:
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return ""
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    for model_id in _GEMINI_MODELS:
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
    pattern_list = "\n".join(f"- {p.id}" for p in patterns)
    return f"""あなたは日本語で自然な読み物を書くライターです。対象レベルはJLPT {level_tag}です。

【必ず使う文型】(必ず全部、それぞれ最低1回、自然な文脈で使うこと)
{pattern_list}

【出力ルール — 絶対厳守】
1. まず一行目に、内容を表す短い見出しを日本語で書く(文型名や文法用語は絶対に書かない、あくまで話の題材を表す一言)
2. 二行目は「---」だけ
3. 三行目以降に、{SENTENCE_MIN}〜{SENTENCE_MAX}文程度の、一つのはっきりしたテーマを持つ自然な日本語の文章を書く
4. 文章は一つのまとまった話として展開すること(起承転結や心情の変化があること)。バラバラな文を並べただけにしない
5. 上に挙げた文型を全部、不自然にならない範囲で文章中に組み込む
6. 説明、翻訳、注釈、箇条書き、記号、マークダウンの装飾は一切書かない。読み物本文だけを書く
7. 暴力・犯罪・死亡・宗教・政治的に偏った内容は避ける
8. 見出しは内容だけを表すこと(文法カテゴリーが分かるような単語は使わない)"""


def parse_gemini_output(raw: str):
    if "---" not in raw:
        return None, None
    head, _, body = raw.partition("---")
    topic = head.strip().strip("#").strip()
    passage = body.strip()
    passage = re.sub(r"\n{2,}", "\n", passage)
    passage = "".join(line.strip() for line in passage.split("\n"))
    return topic, passage


# Gemini가 흔히 한자로 표기하는 문형 구성 요소를 히라가나로 되돌려서
# 검증 시 놓치지 않도록 한다 (예: ば良かった → ばよかった).
_KANJI_TO_KANA = {
    "良かった": "よかった", "良ければ": "よければ", "良い": "よい",
    "事": "こと", "為": "ため", "通り": "とおり", "筈": "はず",
    "訳": "わけ", "様だ": "ようだ", "無い": "ない", "出来る": "できる",
    "有る": "ある", "頃": "ころ", "気味": "ぎみ",
}


def _normalize(text: str) -> str:
    for kanji, kana in _KANJI_TO_KANA.items():
        text = text.replace(kanji, kana)
    return text


def validate_passage(passage: str, patterns: list) -> bool:
    if not passage:
        return False
    sentence_count = passage.count("。")
    if not (SENTENCE_MIN - 2 <= sentence_count <= SENTENCE_MAX + 3):
        _rlog(f"[검증] 문장 수 {sentence_count}개 — 범위 벗어남")
        return False
    normalized = _normalize(passage)
    missing = [p.id for p in patterns if not p.found_in(normalized)]
    if missing:
        _rlog(f"[검증] 문형 누락: {', '.join(missing)}")
        return False
    return True


def generate_passage(category: dict, history: dict):
    patterns = select_patterns(category, history)
    temperatures = [0.7, 0.6, 0.4, 0.2]
    for attempt in range(MAX_GEN_ATTEMPTS):
        if attempt == 2:
            # 두 번 실패하면 문형 조합 자체를 바꿔서 재시도
            _rlog("[재시도] 문형 조합 교체")
            patterns = select_patterns(category, history)
        prompt = build_prompt(patterns, category["level_tag"])
        raw = _call_gemini(prompt, temperatures[attempt])
        topic, passage = parse_gemini_output(raw)
        if passage and validate_passage(passage, patterns):
            _rlog(f"[생성] {attempt + 1}번째 시도에서 성공")
            return topic or "日本語の読み物", passage, patterns
        _rlog(f"[생성] {attempt + 1}번째 시도 실패")
    _rlog("[생성] 전체 시도 실패 — 발송 중단")
    return None, None, patterns


# ── PDF / 메일 템플릿 (japanese-study 원본 형식) ───────────
WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def find_font() -> str:
    import glob
    env_font = os.environ.get("JAPANESE_FONT_PATH")
    if env_font and os.path.exists(env_font):
        return env_font
    for pattern in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Regular*.ttc",
        "/usr/share/fonts/**/*CJK*Regular*.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/**/*ipag*.ttf",
    ]:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return sorted(hits)[0]
    raise FileNotFoundError("일본어 폰트를 찾을 수 없습니다. JAPANESE_FONT_PATH를 지정하세요.")


def header_lines(today: datetime.date, level_tag: str, topic: str) -> list:
    week_no = today.isocalendar()[1]
    return [
        "日本語学習 読み物",
        f"{today.isoformat()} ({WEEKDAY_EN[today.weekday()]}) | Week {week_no}",
        f"[ {level_tag} ]",
        f"テーマ: {topic}",
    ]


def split_sentences(passage: str) -> list:
    """'。' 기준으로 문장을 나눈다. 원본 템플릿처럼 문장마다 한 줄로 표시하기 위함."""
    parts = [s.strip() for s in passage.split("。") if s.strip()]
    return [s + "。" for s in parts]


class ReadingPDF(FPDF):
    def __init__(self, font_path: str):
        super().__init__()
        self.add_font("JP", "", font_path)
        self.set_auto_page_break(auto=True, margin=18)


def build_pdf(today: datetime.date, level_tag: str, topic: str, passage: str) -> str:
    font_path = find_font()
    pdf = ReadingPDF(font_path)
    pdf.add_page()
    pdf.set_font("JP", size=11)
    for line in header_lines(today, level_tag, topic):
        pdf.multi_cell(0, 7, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)
    pdf.set_font("JP", size=12)
    for sentence in split_sentences(passage):
        pdf.multi_cell(0, 8.5, sentence, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    output_path = os.path.join(BASE_DIR, f"JPN_{today.isoformat()}_文法活用.pdf")
    pdf.output(output_path)
    _rlog(f"[PDF] 생성 완료: {output_path}")
    return output_path


def build_html(today: datetime.date, level_tag: str, topic: str, passage: str) -> str:
    head = "<br>".join(header_lines(today, level_tag, topic))
    sentences_html = "<br>".join(split_sentences(passage))
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:24px;
background:#fafafa;font-family:'Helvetica Neue',Arial,'Noto Sans JP',sans-serif;color:#222">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:32px;border-radius:6px">
<div style="font-size:13px;color:#999;line-height:1.7">{head}</div>
<div style="margin-top:20px;font-size:16px;line-height:2">{sentences_html}</div>
</div></body></html>"""


def build_subject(today: datetime.date) -> str:
    return f"日本語学習 読み物 · {today.isoformat()} ({WEEKDAY_EN[today.weekday()]})"


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
        part.add_header("Content-Disposition",
                         f'attachment; filename="{os.path.basename(pdf_path)}"')
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


# ── 실행 ──────────────────────────────────────────────
def main():
    today = datetime.date.today()
    category = pick_category(today)
    history = load_history()

    topic, passage, patterns = generate_passage(category, history)
    if not passage:
        _rlog("[중단] 지문 생성 실패로 발송하지 않음")
        return

    pdf_path = ""
    try:
        pdf_path = build_pdf(today, category["level_tag"], topic, passage)
    except FileNotFoundError as e:
        _rlog(f"[PDF] 생략: {e}")

    html = build_html(today, category["level_tag"], topic, passage)
    subject = build_subject(today)
    sent = send_mail(subject, html, pdf_path)

    if sent:
        append_history(history, category["key"], patterns, today)
        commit_history(today)
    else:
        _rlog("[이력] 발송 실패 — 이력 갱신하지 않음 (다음 실행에서 같은 후보 유지)")


if __name__ == "__main__":
    main()
