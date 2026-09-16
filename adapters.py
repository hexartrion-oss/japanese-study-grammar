"""ports.py에 정의된 포트의 실제(운영) 구현.

여기 있는 코드만 네트워크·파일시스템·git·시계를 만진다. 파이프라인 로직은
get_grammar.py에 있고, 그쪽은 이 파일을 import하지 않는다 — 방향은 언제나
main() → build_production_deps() → 파이프라인이다.

동작은 리팩터링 이전 get_grammar.py와 동일하게 유지했다. 모델 폴백 순서,
한도 초과 시 대기 시간, 폰트 탐색 경로, git 커밋 절차 모두 그대로다.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import random
import re
import smtplib
import subprocess
import sys
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Sequence
from zoneinfo import ZoneInfo

import ports

try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

KST = ZoneInfo("Asia/Seoul")

# 생성 모델 폴백 순서. 앞에서부터 시도하고, 실패하면 다음으로 넘어간다.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash"]


# ── 시계 ───────────────────────────────────────────────
class SystemClock:
    def now_utc(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def now_kst(self) -> datetime.datetime:
        return datetime.datetime.now(KST)


# ── 난수 ───────────────────────────────────────────────
class SystemRng:
    def __init__(self, seed=None):
        self._r = random.Random(seed)

    def choice(self, seq: Sequence):
        return self._r.choice(seq)

    def sample(self, population: Sequence, k: int) -> list:
        return self._r.sample(population, k)


# ── 로그 ───────────────────────────────────────────────
class FileLogger:
    """stdout과 실행 로그 파일 양쪽에 남긴다. 로그 파일은 Actions 아티팩트로
    업로드되므로, 호출부는 메일 주소를 반드시 마스킹해서 넘겨야 한다."""

    def __init__(self, log_path: str):
        self.log_path = log_path

    def __call__(self, message: str) -> None:
        print(message)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(str(message) + "\n")
        except OSError:
            pass


# ── 생성 모델 ──────────────────────────────────────────
class GeminiLlm:
    def __init__(self, api_key: str, log: ports.Logger,
                 models: Sequence[str] = GEMINI_MODELS, sleep=time.sleep):
        self.api_key = api_key
        self.log = log
        self.models = list(models)
        self._sleep = sleep      # 테스트에서 실제로 기다리지 않도록 주입 가능

    def generate(self, prompt: str, temperature: float,
                 model: str | None = None, max_output_tokens: int = 2200) -> str:
        if not GEMINI_AVAILABLE or not self.api_key:
            return ""
        client = google_genai.Client(api_key=self.api_key)
        for model_id in ([model] if model else self.models):
            for attempt in range(2):
                try:
                    cfg = {"temperature": temperature,
                           "max_output_tokens": max_output_tokens}
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
                        self.log(f"[Gemini] {model_id} 한도 초과. {wait}초 대기 후 재시도")
                        self._sleep(wait)
                        continue
                    if is_quota:
                        self._sleep(10)
                        break
                    if ("503" in err or "UNAVAILABLE" in err) and attempt == 0:
                        self._sleep(30)
                        continue
                    self.log(f"[Gemini] {model_id} 오류(폴백 전환): {e}")
                    break
        return ""


# ── 메일 ───────────────────────────────────────────────
class SmtpMailer:
    def __init__(self, address: str, app_password: str, log: ports.Logger,
                 host: str = "smtp.gmail.com", port: int = 465):
        self.address = address
        self.app_password = app_password
        self.log = log
        self.host = host
        self.port = port

    def send(self, subject: str, html: str, to: Sequence[str],
             attachment_path: str = "") -> bool:
        recipients = list(to)
        if not recipients:
            return False
        msg = MIMEMultipart()
        msg["From"] = self.address
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html", "utf-8"))

        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment",
                            filename=os.path.basename(attachment_path))
            msg.attach(part)

        try:
            with smtplib.SMTP_SSL(self.host, self.port) as server:
                server.login(self.address, self.app_password)
                server.sendmail(self.address, recipients, msg.as_string())
            return True
        except smtplib.SMTPException as e:
            self.log(f"[메일] 발송 실패: {e}")
            return False


# ── 상태 파일 ──────────────────────────────────────────
class JsonFileStore:
    """논리적 이름 → 파일 경로. 파일이 없거나 깨져 있으면 기본값을 돌려준다
    (실행을 멈추지 않는다 — 이력이 없다고 그날 발송을 포기할 이유는 없다)."""

    def __init__(self, base_dir: str, log: ports.Logger):
        self.base_dir = base_dir
        self.log = log

    def path_of(self, name: str) -> str:
        return os.path.join(self.base_dir, f"{name}.json")

    def read(self, name: str, default: dict) -> dict:
        path = self.path_of(name)
        if not os.path.exists(path):
            return json.loads(json.dumps(default))
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            self.log(f"[{name}] 파일 손상 또는 없음 — 새로 시작")
            return json.loads(json.dumps(default))

    def write(self, name: str, data: dict) -> None:
        with open(self.path_of(name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ── git ────────────────────────────────────────────────
class GitVcs:
    """CI에서만 실제로 커밋한다. 로컬 실행에서는 파일만 저장되고 끝난다."""

    def __init__(self, base_dir: str, log: ports.Logger, enabled: bool):
        self.base_dir = base_dir
        self.log = log
        self.enabled = enabled

    def commit_and_push(self, paths: Sequence[str], message: str) -> bool:
        if not self.enabled:
            self.log("[git] 로컬 실행 — 커밋 생략 (파일만 저장됨)")
            return False
        try:
            subprocess.run(["git", "config", "user.email", "actions@github.com"],
                           check=True, cwd=self.base_dir)
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                           check=True, cwd=self.base_dir)
            subprocess.run(["git", "add", *paths], check=True, cwd=self.base_dir)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.base_dir)
            if diff.returncode == 0:
                self.log("[git] 변경 사항 없음 — 커밋 생략")
                return False
            subprocess.run(["git", "commit", "-m", message], check=True, cwd=self.base_dir)
            subprocess.run(["git", "push"], check=True, cwd=self.base_dir)
            return True
        except subprocess.CalledProcessError as e:
            self.log(f"[git] 커밋 실패: {e}")
            return False


# ── 폰트 ───────────────────────────────────────────────
class SystemFonts:
    """style: "" (Regular) 또는 "B" (Bold). Bold 파일이 없으면 Regular로 대체한다."""

    REGULAR_PATTERNS = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Regular*.ttc",
        "/usr/share/fonts/**/*CJK*Regular*.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/**/*ipag*.ttf",
    ]
    BOLD_PATTERNS = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/**/NotoSansCJK*Bold*.ttc",
    ]

    def __init__(self, env: dict | None = None):
        self.env = env if env is not None else os.environ

    def find(self, style: str = "") -> str:
        env_key = "JAPANESE_FONT_PATH_BOLD" if style == "B" else "JAPANESE_FONT_PATH"
        env_font = self.env.get(env_key) or (
            self.env.get("JAPANESE_FONT_PATH") if style != "B" else None)
        if env_font and os.path.exists(env_font):
            return env_font
        patterns = self.BOLD_PATTERNS if style == "B" else self.REGULAR_PATTERNS
        for pattern in patterns:
            hits = glob.glob(pattern, recursive=True)
            if hits:
                return sorted(hits)[0]
        if style == "B":
            return self.find("")
        raise FileNotFoundError(
            "일본어 폰트를 찾을 수 없습니다. JAPANESE_FONT_PATH를 지정하세요.")


# ── 조립 ───────────────────────────────────────────────
def load_secrets(env: dict | None = None, base_dir: str = "") -> ports.Secrets:
    """환경변수에서 자격 증명을 읽는다. 로컬 개발 편의를 위해 .env도 본다
    (python-dotenv가 없으면 조용히 건너뛴다)."""
    env = env if env is not None else os.environ
    values = {
        "gmail_address": env.get("GMAIL_ADDRESS", ""),
        "gmail_app_password": env.get("GMAIL_APP_PASSWORD", ""),
        "gemini_api_key": env.get("GEMINI_API_KEY", ""),
        "email_recipients": env.get("EMAIL_RECIPIENTS", ""),
    }
    if base_dir:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(base_dir, ".env"))
            for field_name, env_key in [
                ("gmail_address", "GMAIL_ADDRESS"),
                ("gmail_app_password", "GMAIL_APP_PASSWORD"),
                ("gemini_api_key", "GEMINI_API_KEY"),
                ("email_recipients", "EMAIL_RECIPIENTS"),
            ]:
                values[field_name] = values[field_name] or os.getenv(env_key, "")
        except ImportError:
            pass
    return ports.Secrets(**values)


def load_run_mode(env: dict | None = None) -> ports.RunMode:
    env = env if env is not None else os.environ
    return ports.RunMode(
        manual=env.get("MANUAL_RUN") == "1",
        manual_mail_to=env.get("MANUAL_MAIL_TO", ""),
        forced_category=env.get("FORCE_CATEGORY", "").strip(),
        in_ci=env.get("GITHUB_ACTIONS") == "true",
    )


def build_production_deps(base_dir: str, settings: ports.Settings | None = None) -> ports.Deps:
    """운영용 Deps 조립. main()에서만 호출한다."""
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    log = FileLogger(os.path.join(base_dir, "run_log.txt"))
    secrets = load_secrets(base_dir=base_dir)
    mode = load_run_mode()
    return ports.Deps(
        clock=SystemClock(),
        rng=SystemRng(),
        log=log,
        llm=GeminiLlm(secrets.gemini_api_key, log),
        mailer=SmtpMailer(secrets.gmail_address, secrets.gmail_app_password, log),
        store=JsonFileStore(base_dir, log),
        vcs=GitVcs(base_dir, log, enabled=mode.in_ci),
        fonts=SystemFonts(),
        secrets=secrets,
        mode=mode,
        settings=settings or ports.Settings(),
    )
