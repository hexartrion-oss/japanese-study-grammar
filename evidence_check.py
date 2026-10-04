"""웹 검색 근거 조회의 순수 로직 (네트워크·파일을 모른다).

2026-10-03 실측 결과가 이 모듈의 설계를 정했다:
  - 모델이 적는 "출처 종류(論文/記事)"는 믿을 수 없다 → **출처 적격은 코드가 도메인으로 판정**한다.
  - 학술·신문 도메인 출처가 2건 이상 나온 응답이 15회 중 1회뿐 → 적격 기준은 기본 1건 이상.
  - 검색 호출은 느리고(평균 30초) 한도가 작다 → 호출은 어댑터가, 호출 수 상한은 호출부가 관리한다.
번복(enforce)은 구현하지 않는다 — 여기서는 "기록용 요약"과 "번복 후보 판정"까지만 한다.
"""

from __future__ import annotations

import re

import ports

_ACADEMIC_SUFFIXES = (".ac.jp",)
_ACADEMIC_HOSTS = ("jstage.jst.go.jp", "cir.nii.ac.jp", "ci.nii.ac.jp", "nii.ac.jp",
                   "ndlsearch.ndl.go.jp", "ndl.go.jp")
_NEWS_HOSTS = ("asahi.com", "mainichi.jp", "yomiuri.co.jp", "nikkei.com", "nhk.or.jp",
               "sankei.com", "tokyo-np.co.jp", "kyodonews.net", "jiji.com", "47news.jp")


def classify_domain(host: str) -> str:
    """도메인 → academic / news / other. 접미사는 점 경계로만 비교한다(유사 도메인 방어)."""
    h = (host or "").lower().strip(".")
    if any(h.endswith(s) and len(h) > len(s) for s in _ACADEMIC_SUFFIXES):
        return "academic"
    if any(h == x or h.endswith("." + x) for x in _ACADEMIC_HOSTS):
        return "academic"
    if any(h == x or h.endswith("." + x) for x in _NEWS_HOSTS):
        return "news"
    return "other"


_PROMPT = """次の文が、文型「{pattern}」の用法として文法的に正しく自然かどうかを、Google検索で調べて判定してください。
参照してよい出典は、学位論文・学術論文・新聞記事・新聞の寄稿やコラムに限ります。
日本語学習サイト、ブログ、Q&Aサイト、SNS、学習者向けの辞書サイトは参照しないでください。
適切な出典が見つからない場合は「不明」としてください。

【文】
{sentence}

出力は次の3行だけにしてください。
判定: 自然 または 不自然 または 不明
根拠: (60字以内)
出典種別: 論文 または 記事 または 寄稿 または なし
"""


def build_prompt(pattern_id: str, sentence: str) -> str:
    return _PROMPT.format(pattern=pattern_id, sentence=sentence)


def extract_sentence(passage: str, contains) -> str:
    """지문에서 문형이 쓰인 문장 하나. contains(sentence)->bool은 호출부가 준다(변형 허용 로직 재사용)."""
    parts = [s.strip() + "。" for s in passage.split("。") if s.strip()]
    for s in parts:
        if contains(s):
            return s
    return parts[0] if parts else passage[:120]


def parse_verdict(text: str) -> dict:
    """'判定:' 줄을 natural / unnatural / unknown 으로. 「不自然」 안의 「自然」에 속지 않는다."""
    m = re.search(r"判定\s*[:\uff1a]\s*(\S+)", text or "")
    raw = m.group(1) if m else ""
    if "不自然" in raw:
        verdict = "unnatural"
    elif "自然" in raw:
        verdict = "natural"
    else:
        verdict = "unknown"
    basis = re.search(r"根拠\s*[:\uff1a]\s*(.+)", text or "")
    return {"verdict": verdict, "basis": basis.group(1).strip() if basis else ""}


def summarize(result: ports.EvidenceResult) -> dict:
    """기록용 요약. 호출이 실패하면 verdict는 unknown이 아니라 unavailable(상태를 구분해 남긴다)."""
    hosts = [s.get("host", "") for s in result.sources]
    classes = [classify_domain(h) for h in hosts]
    base = {"status": result.status, "model": result.model, "latency_s": result.latency_s,
            "n_sources": len(hosts), "n_qualified": sum(1 for c in classes if c != "other"),
            "hosts": hosts[:8], "classes": classes[:8]}
    if result.status != "ok":
        return {**base, "verdict": "unavailable", "basis": ""}
    return {**base, **parse_verdict(result.text)}


def usable_for_overturn(summary: dict, min_qualified: int) -> bool:
    """번복 '후보'로 기록할 만한가 (실제 번복은 하지 않는다 — shadow 전용)."""
    return (summary.get("status") == "ok" and summary.get("verdict") == "natural"
            and summary.get("n_qualified", 0) >= min_qualified)
