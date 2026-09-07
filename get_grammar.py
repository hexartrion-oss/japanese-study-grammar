"""일본어 동사·문법 학습 메일링.

japanese-study(읽기 자료)의 자매 리포지토리.
차이점: 활용표는 규칙 기반으로 확정 생성하고, Gemini는 예문 작성에만 쓴다.

요일별 테마 순환 (월~일):
  0 동사 활용 기초   1 자동사·타동사   2 수수동사   3 수동·사역
  4 조건표현         5 복합동사        6 경어 동사
"""

import os
import re
import sys
import glob
import time
import random
import smtplib
import datetime
import platform
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

from conjugator import conjugate, FORMS, GODAN, ICHIDAN, SURU, KURU
import verbs as VB

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
OUTPUT_PDF = os.path.join(BASE_DIR, "JPN_GRAMMAR.pdf")

MANUAL_RUN = os.environ.get("MANUAL_RUN") == "1"
MANUAL_MAIL_TO = os.environ.get("MANUAL_MAIL_TO", "")
RUN_LOG = []
RUN_LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")


def _rlog(msg: str):
    print(msg)
    RUN_LOG.append(str(msg))
    try:
        with open(RUN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except OSError:
        pass


# ── 테마 정의 ──────────────────────────────────────────
THEME_CONJUGATION = "conjugation"
THEME_TRANSITIVITY = "transitivity"
THEME_GIVING = "giving"
THEME_VOICE = "voice"
THEME_CONDITIONAL = "conditional"
THEME_COMPOUND = "compound"
THEME_KEIGO = "keigo"

# 월(0) ~ 일(6)
WEEKLY_PLAN = [
    (THEME_CONJUGATION, "동사 활용 기초", ["N5", "N4"]),
    (THEME_TRANSITIVITY, "자동사·타동사 짝", ["N4", "N3"]),
    (THEME_GIVING, "수수동사 (주고받기)", ["N4", "N3"]),
    (THEME_VOICE, "수동·사역·사역수동", ["N3", "N2"]),
    (THEME_CONDITIONAL, "조건표현 と·ば·たら·なら", ["N3", "N2"]),
    (THEME_COMPOUND, "복합동사", ["N2", "N1"]),
    (THEME_KEIGO, "경어 동사 (존경어·겸양어)", ["N2", "N1"]),
]

# 테마별로 활용표에 실을 형태
FORM_SETS = {
    THEME_CONJUGATION: ["masu", "te", "ta", "nai", "teiru", "potential", "ba"],
    THEME_TRANSITIVITY: ["masu", "te", "ta", "nai", "teiru"],
    THEME_GIVING: ["masu", "te", "ta", "nai"],
    THEME_VOICE: ["passive", "causative", "caus_pass", "te", "nai"],
    THEME_CONDITIONAL: ["ba", "tara", "ta", "nai"],
    THEME_COMPOUND: ["masu", "te", "ta", "nai", "potential"],
    THEME_KEIGO: ["masu", "te", "ta", "nai"],
}


def pick_theme(today: datetime.date) -> tuple:
    if os.environ.get("FORCE_THEME"):
        forced = os.environ["FORCE_THEME"]
        for t in WEEKLY_PLAN:
            if t[0] == forced:
                _rlog(f"[테마] 강제 지정: {t[1]}")
                return t
    theme = WEEKLY_PLAN[today.weekday()]
    _rlog(f"[테마] {today} ({'월화수목금토일'[today.weekday()]}) → {theme[1]}")
    return theme


# ── Gemini 호출 ────────────────────────────────────────
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash"]


def _call_gemini(prompt: str, temperature: float = 0.4, max_tokens: int = 2048) -> str:
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return ""
    client = google_genai.Client(api_key=GEMINI_API_KEY)
    for model_id in _GEMINI_MODELS:
        print(f"[Gemini] 모델 시도: {model_id}")
        for attempt in range(2):
            try:
                cfg = {"temperature": temperature, "max_output_tokens": max_tokens}
                if "2.5" in model_id:
                    cfg["thinking_config"] = genai_types.ThinkingConfig(thinking_budget=0)
                res = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(**cfg),
                )
                return res.text or ""
            except Exception as e:
                err = str(e)
                is_quota = "429" in err or "quota" in err.lower()
                if is_quota and attempt == 0:
                    m = re.search(r"retry in (\d+(?:\.\d+)?)", err)
                    wait = int(float(m.group(1))) + 5 if m else 60
                    print(f"[Gemini] {model_id} 한도 초과. {wait}초 대기 후 재시도...")
                    time.sleep(wait)
                    continue
                if is_quota:
                    time.sleep(10)
                    break
                if ("503" in err or "UNAVAILABLE" in err) and attempt == 0:
                    print(f"[Gemini] {model_id} 503. 30초 대기 후 재시도...")
                    time.sleep(30)
                    continue
                print(f"[Gemini] {model_id} 오류(폴백 전환): {e}")
                break
    _rlog("[Gemini] 모든 모델 실패 — 예문 없이 활용표만 발송")
    return ""


# ── 유틸 ──────────────────────────────────────────────
def is_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-鿿]", text))


def sanitize_text(text: str) -> str:
    text = "".join(c for c in text if ord(c) <= 0xFFFF)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def find_font() -> str:
    env_font = os.environ.get("JAPANESE_FONT_PATH")
    if env_font and os.path.exists(env_font):
        return env_font
    system = platform.system()
    if system == "Windows":
        for f in [r"C:\Windows\Fonts\msgothic.ttc", r"C:\Windows\Fonts\meiryo.ttc"]:
            if os.path.exists(f):
                return f
    # PDF에는 한국어 라벨·해석이 함께 들어가므로 일본어 전용 폰트(IPA 고딕 등)로는
    # 한글 글리프가 빠진다. 일본어·한국어를 모두 포함하는 Noto CJK를 우선한다.
    for pattern in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Regular*.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.otf",
        "/usr/share/fonts/**/*CJK*Regular*.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/**/*ipag*.ttf",
    ]:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return sorted(hits)[0]
    raise FileNotFoundError(
        "일본어 폰트를 찾을 수 없습니다. JAPANESE_FONT_PATH에 .ttf 경로를 지정하세요."
    )


# ── 오늘의 학습 항목 선정 ──────────────────────────────
def build_items(theme_key: str, levels: list) -> list:
    """[(제목, [(라벨, 내용), ...])] 형식의 표 데이터."""
    if theme_key == THEME_TRANSITIVITY:
        picked = random.sample(VB.PAIRS, 5)
        return [
            (f"{intr} / {tr}", [
                ("자동사", f"{intr}（{ir}）— 스스로 그렇게 됨"),
                ("타동사", f"{tr}（{tres}）— 누가 그렇게 함"),
                ("뜻", mean),
                ("자동사 ている", conjugate(intr, ig, ["teiru"])[0][1] + " (상태)"),
                ("타동사 てある", conjugate(tr, tg, ["te"])[0][1] + "ある (준비된 상태)"),
            ])
            for intr, ir, ig, tr, tres, tg, mean in picked
        ]

    if theme_key == THEME_GIVING:
        picked = random.sample(VB.GIVING, 5)
        return [(v, [("뜻", mean), ("기본 예", ex)]) for v, mean, ex in picked]

    if theme_key == THEME_CONDITIONAL:
        return [(f"〜{c}", [("용법", use), ("예", ex)]) for c, use, ex in VB.CONDITIONALS]

    if theme_key == THEME_COMPOUND:
        picked = random.sample(VB.COMPOUND, 6)
        return [(suf, [("뜻", mean), ("예", ex)]) for suf, mean, ex in picked]

    if theme_key == THEME_KEIGO:
        picked = random.sample(VB.KEIGO, 5)
        return [
            (base, [("뜻", mean), ("존경어", hon), ("겸양어", hum)])
            for base, hon, hum, mean in picked
        ]

    # 활용 기초 / 태(voice) — 활용표 생성
    pool = VB.all_verbs_for(levels)
    picked = random.sample(pool, min(5, len(pool)))
    form_keys = FORM_SETS[theme_key]
    items = []
    for v, yomi, group, mean in picked:
        rows = [("뜻", mean), ("그룹", {"godan": "1그룹(五段)", "ichidan": "2그룹(一段)",
                                     "suru": "3그룹(する)", "kuru": "3그룹(来る)"}[group])]
        rows += conjugate(v, group, form_keys)
        items.append((f"{v}（{yomi}）", rows))
    return items


# ── 예문 생성 ─────────────────────────────────────────
def make_examples(theme_label: str, items: list, levels: list) -> dict:
    """{제목: [예문(일본어), 한국어 해석]} — Gemini 실패 시 빈 dict."""
    heads = [t for t, _ in items]
    detail = "\n".join(
        f"- {t}: " + " / ".join(f"{k}={v}" for k, v in rows[:4]) for t, rows in items
    )
    prompt = f"""あなたは日本語教師です。今日の学習テーマは「{theme_label}」、対象レベルはJLPT {'・'.join(levels)}です。

【今日の項目】
{detail}

上の各項目について、その項目の使い方が最もよく分かる例文を1つずつ作ってください。

【出力ルール — 絶対厳守】
1. 1行に1項目。形式は「項目名｜日本語の例文｜韓国語訳」
2. 項目名は上のリストの表記をそのまま使う
3. 例文は20〜35字程度の自然な現代日本語にすること
4. その項目の文法・活用が必ず文中に現れること
5. 説明・番号・記号・前置き・マークダウンは一切書かない
6. 暴力・犯罪・死亡・宗教に関する内容は使わない
7. 全部で{len(heads)}行だけ出力する"""

    raw = _call_gemini(prompt, temperature=0.7, max_tokens=2048)
    out = {}
    for line in raw.split("\n"):
        parts = [p.strip() for p in line.split("｜")]
        if len(parts) != 3:
            continue
        head, jp, ko = parts
        if not is_japanese(jp):
            continue
        for h in heads:
            if head in h or h in head:
                out[h] = (sanitize_text(jp), sanitize_text(ko))
                break
    _rlog(f"[예문] {len(out)}/{len(heads)}개 생성")
    return out


# ── PDF ───────────────────────────────────────────────
class GrammarPDF(FPDF):
    def __init__(self, font_path: str, title: str):
        super().__init__()
        self.title_text = title
        self.add_font("JP", "", font_path)
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        self.set_font("JP", size=9)
        self.set_text_color(130, 130, 130)
        self.cell(0, 8, self.title_text, new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-14)
        self.set_font("JP", size=8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, str(self.page_no()), align="C")


def build_pdf(theme_label: str, levels: list, items: list, examples: dict, today) -> str:
    font_path = find_font()
    header = f"{today:%Y-%m-%d} · {theme_label}"
    pdf = GrammarPDF(font_path, header)
    pdf.add_page()

    pdf.set_font("JP", size=18)
    pdf.multi_cell(0, 11, sanitize_text(theme_label), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("JP", size=10)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 7, f"{today:%Y년 %m월 %d일} · 대상 레벨 JLPT {'·'.join(levels)}",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    for idx, (head, rows) in enumerate(items, 1):
        pdf.set_font("JP", size=14)
        pdf.multi_cell(0, 10, sanitize_text(f"{idx}. {head}"),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("JP", size=11)
        for label, value in rows:
            pdf.set_text_color(110, 110, 110)
            pdf.cell(38, 7, sanitize_text(label))
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 7, sanitize_text(value),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if head in examples:
            jp, ko = examples[head]
            pdf.ln(1)
            pdf.set_text_color(40, 80, 160)
            pdf.multi_cell(0, 7, sanitize_text(f"　例  {jp}"),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(120, 120, 120)
            pdf.multi_cell(0, 7, sanitize_text(f"　　  {ko}"),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(5)

    pdf.output(OUTPUT_PDF)
    _rlog(f"[PDF] 생성 완료: {OUTPUT_PDF}")
    return OUTPUT_PDF


# ── 메일 ──────────────────────────────────────────────
def build_html(theme_label: str, levels: list, items: list, examples: dict, today) -> str:
    blocks = []
    for idx, (head, rows) in enumerate(items, 1):
        lines = "".join(
            f'<tr><td style="color:#888;padding:3px 12px 3px 0;white-space:nowrap;'
            f'vertical-align:top">{label}</td><td style="padding:3px 0">{value}</td></tr>'
            for label, value in rows
        )
        ex_html = ""
        if head in examples:
            jp, ko = examples[head]
            ex_html = (
                f'<div style="margin-top:10px;padding:10px 12px;background:#f5f7fb;'
                f'border-left:3px solid #4a6fb5;border-radius:3px">'
                f'<div style="font-size:15px">{jp}</div>'
                f'<div style="color:#888;font-size:13px;margin-top:4px">{ko}</div></div>'
            )
        blocks.append(
            f'<div style="margin-bottom:26px">'
            f'<div style="font-size:17px;font-weight:600;margin-bottom:8px">{idx}. {head}</div>'
            f'<table style="border-collapse:collapse;font-size:14px">{lines}</table>'
            f'{ex_html}</div>'
        )
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:24px;
background:#fafafa;font-family:'Helvetica Neue',Arial,'Noto Sans JP',sans-serif;color:#222">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:32px;border-radius:6px">
<div style="font-size:13px;color:#999">{today:%Y년 %m월 %d일}</div>
<h1 style="font-size:22px;margin:6px 0 4px">{theme_label}</h1>
<div style="font-size:13px;color:#999;margin-bottom:24px">
대상 레벨 JLPT {'·'.join(levels)}</div>
{''.join(blocks)}
<div style="border-top:1px solid #eee;margin-top:8px;padding-top:14px;
font-size:12px;color:#aaa">활용표는 규칙 기반으로 생성되며, 예문은 Gemini가 작성합니다.</div>
</div></body></html>"""


def send_mail(subject: str, html: str, pdf_path: str):
    if not GMAIL_ADDRESS or not GMAIL_APP_PW:
        _rlog("[메일] 인증 정보 없음 — 발송 생략")
        return
    if MANUAL_RUN and MANUAL_MAIL_TO:
        recipients = [MANUAL_MAIL_TO]
        _rlog(f"[메일] 수동 실행 — 수신자 고정: {MANUAL_MAIL_TO}")
    else:
        recipients = [r.strip() for r in EMAIL_RECIPIENTS.split(",") if r.strip()]
    if not recipients:
        _rlog("[메일] 수신자 없음 — 발송 생략")
        return

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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PW)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
    _rlog(f"[메일] 발송 완료 → {', '.join(recipients)}")


# ── 실행 ──────────────────────────────────────────────
def main():
    today = datetime.date.today()
    theme_key, theme_label, levels = pick_theme(today)

    items = build_items(theme_key, levels)
    _rlog(f"[항목] {len(items)}개: " + ", ".join(t for t, _ in items))

    examples = make_examples(theme_label, items, levels)

    pdf_path = ""
    try:
        pdf_path = build_pdf(theme_label, levels, items, examples, today)
    except FileNotFoundError as e:
        _rlog(f"[PDF] 생략: {e}")

    html = build_html(theme_label, levels, items, examples, today)
    subject = f"[일본어 문법] {today:%m/%d} {theme_label}"
    send_mail(subject, html, pdf_path)


if __name__ == "__main__":
    main()
