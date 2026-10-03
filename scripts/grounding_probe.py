"""검색 그라운딩 실측용 일회성 프로브 (단계 0).

운영 파이프라인과 무관한 진단 스크립트다 — get_grammar.py를 import하지 않고, 결과는
stdout과 probe_results.jsonl로만 남긴다. 확인하려는 것:
  1. 판정 모델(3.x)에서 google_search 그라운딩이 동작하는가 / 한도·지연
  2. 응답에 출처(도메인)가 어떤 형태로 담기는가, "학위논문·기사·기고문" 지시가 먹는가
  3. 같은 질문의 반복 일관성(재현성)
  4. 맞는 주장(지지 기대)과 일부러 틀린 주장(반증 기대)에 대한 응답 — 확증 편향 점검

주의: 이 스크립트는 규칙 개정이나 번복을 하지 않는다. 숫자만 낸다.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import urlparse

MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]

# (id, 기대 판정, 주장). 기대 판정은 문법서 일반 지식 기준이며 정답 확정이 아니다 —
# "지지/반증 방향이 기대와 같은가"만 본다.
PROBES = [
    ("禁じ得ない-前接拡張", "支持",
     "「を禁じ得ない」の前には、涙・怒り・驚き・失望・感動・悲しみ以外の感情を表す名詞"
     "(例:同情、疑問、不安、笑い)も来ることができる。"),
    ("禁じ得ない-過去形", "支持",
     "「を禁じ得ない」は過去形「禁じ得なかった」の形でも用いられる。"),
    ("あげく-否定結果", "支持",
     "「あげく」の後件は、否定的・望ましくない結果を表すのが基本で、"
     "肯定的な結果(例:見事に成功した)が続くのは不自然である。"),
    ("ばかりに-否定結果", "支持",
     "「ばかりに」は原因・理由を表し、後件には望ましくない結果が来る。"),
    ("くせに-批判語不要", "支持",
     "「くせに」は話し手の非難・不満を表す文型で、文中に「批判」「不満」などの語が明示されて"
     "いなくても、「勉強していないくせに満点を取った」のように文として成立する。"),
    ("にかたくない-前接拡張", "支持",
     "「にかたくない」の前には「想像」のほか「予想」「理解」「推察」などの語も来ることができる。"),
    ("対照偽-あげく肯定", "反証",
     "「あげく」の後件には、肯定的で望ましい結果が来るのが普通である。"),
    ("対照偽-禁じ得ない意志", "反証",
     "「を禁じ得ない」の前には、「決断」「行動」のような意志的な行為を表す名詞が来る。"),
    ("対照偽-ばかりに限定", "反証",
     "「自分の意見ばかりに固執して」の「ばかりに」は、原因・理由を表す文型「ばかりに」と"
     "同じ用法である。"),
]

PROMPT = """次の日本語文法に関する主張が正しいかどうか、Google検索で調べて判定してください。
参照してよい出典は、学位論文・学術論文・新聞記事・新聞の寄稿やコラムに限ります。
日本語学習サイト、ブログ、Q&Aサイト、SNS、学習者向けの辞書サイトは参照しないでください。
適切な出典が見つからない場合は「不明」としてください。

【主張】
{claim}

出力は次の3行だけにしてください。
判定: 支持 または 反証 または 不明
根拠: (60字以内)
出典種別: 論文 または 記事 または 寄稿 または なし
"""

_ACADEMIC_SUFFIXES = (".ac.jp", ".go.jp")
_ACADEMIC_HOSTS = ("jstage.jst.go.jp", "cir.nii.ac.jp", "ci.nii.ac.jp", "nii.ac.jp",
                   "ndlsearch.ndl.go.jp", "ndl.go.jp")
_NEWS_HOSTS = ("asahi.com", "mainichi.jp", "yomiuri.co.jp", "nikkei.com", "nhk.or.jp",
               "sankei.com", "tokyo-np.co.jp", "kyodonews.net", "jiji.com", "47news.jp",
               "news.yahoo.co.jp")


def classify_domain(host: str) -> str:
    """도메인 → academic / news / other. 화이트리스트는 코드가 판정한다(모델 자기신고 불신)."""
    h = (host or "").lower()
    if h.endswith(_ACADEMIC_SUFFIXES) or any(h == x or h.endswith("." + x) for x in _ACADEMIC_HOSTS):
        return "academic"
    if any(h == x or h.endswith("." + x) for x in _NEWS_HOSTS):
        return "news"
    return "other"


def extract_sources(response) -> dict:
    """응답에서 검색어와 출처를 뽑는다. 필드가 없으면 빈 값 — 예외를 던지지 않는다."""
    out = {"queries": [], "sources": []}
    try:
        cand = response.candidates[0]
        gm = getattr(cand, "grounding_metadata", None)
        if gm is None:
            return out
        out["queries"] = list(getattr(gm, "web_search_queries", None) or [])
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            if web is None:
                continue
            title = getattr(web, "title", "") or ""
            uri = getattr(web, "uri", "") or ""
            # title이 도메인인 경우가 많다. 아니면 uri의 호스트를 쓴다(리다이렉트면 의미 없음).
            host = title if re.fullmatch(r"[\w.-]+\.[a-z]{2,}", title) else urlparse(uri).netloc
            out["sources"].append({"title": title, "host": host, "uri": uri,
                                   "class": classify_domain(host)})
    except (AttributeError, IndexError, TypeError):
        pass
    return out


def parse_verdict(text: str) -> dict:
    def grab(label):
        m = re.search(label + r"\s*[::]\s*(.+)", text or "")
        return m.group(1).strip() if m else ""
    return {"verdict": grab("判定"), "basis": grab("根拠"), "src_type": grab("出典種別")}


def call(client, types, model, claim):
    cfg = types.GenerateContentConfig(
        temperature=0.0,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    t0 = time.time()
    res = client.models.generate_content(model=model, contents=PROMPT.format(claim=claim), config=cfg)
    return res, time.time() - t0


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("GEMINI_API_KEY 없음 — 중단")
        return 1
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    repeat = int(os.environ.get("PROBE_REPEAT", "2"))
    rows = []
    for model in MODELS:
        for pid, expected, claim in PROBES:
            for run in range(repeat):
                row = {"model": model, "probe": pid, "expected": expected, "run": run + 1}
                try:
                    res, dt = call(client, types, model, claim)
                    text = res.text or ""
                    ex = extract_sources(res)
                    um = getattr(res, "usage_metadata", None)
                    row.update(ok=True, latency_s=round(dt, 1), text=text[:400],
                               model_version=getattr(res, "model_version", None),
                               tokens=getattr(um, "total_token_count", None),
                               queries=ex["queries"][:5], sources=ex["sources"][:10],
                               n_sources=len(ex["sources"]),
                               n_qualified=sum(1 for s in ex["sources"] if s["class"] != "other"),
                               **parse_verdict(text))
                except Exception as e:  # 진단 목적 — 오류 문구만 남기고 계속한다
                    msg = str(e).replace(key, "***")
                    row.update(ok=False, error=msg[:300])
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                time.sleep(2)

    with open("probe_results.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n===== 요약 =====")
    for model in MODELS:
        mine = [r for r in rows if r["model"] == model]
        ok = [r for r in mine if r["ok"]]
        grounded = [r for r in ok if r["n_sources"] > 0]
        qualified = [r for r in ok if r["n_qualified"] >= 2]
        agree = [r for r in ok if r["verdict"] and r["expected"] in r["verdict"]]
        unknown = [r for r in ok if "不明" in r["verdict"]]
        avg_dt = sum(r["latency_s"] for r in ok) / len(ok) if ok else 0
        print(f"[{model}] 호출 {len(mine)} / 성공 {len(ok)} / 출처 있음 {len(grounded)} / "
              f"적격 출처≥2 {len(qualified)} / 판정=기대 {len(agree)} / 不明 {len(unknown)} / "
              f"평균 {avg_dt:.1f}s")
        errs = {}
        for r in mine:
            if not r["ok"]:
                k = r["error"][:80]
                errs[k] = errs.get(k, 0) + 1
        for k, v in errs.items():
            print(f"    오류 x{v}: {k}")
        # 반복 일관성: 같은 (probe) 두 번의 판정이 같은가
        by = {}
        for r in ok:
            by.setdefault(r["probe"], []).append(r["verdict"])
        incons = [p for p, v in by.items() if len(set(v)) > 1]
        print(f"    반복 불일치 프로브: {incons}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
