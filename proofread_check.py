"""문형 밖 어휘·표기·조사 오류 교정 패스 (기록 전용 shadow).

판정(judge)은 문형 5개의 문법만 본다. 그 밖의 오류(예: 「暖かいコーヒー」→「温かい」)는 아무도 보지
않아서 별도 호출로 보고만 받는다. 합격 판정에는 쓰지 않는다 — 몇 주 기록해 진짜 오류/오탐을
사람이 라벨링한 뒤에 정한다(계획 2026-10-11).
"""

from __future__ import annotations


def build_prompt(passage: str) -> str:
    return f"""次の日本語の文章を校正してください。文型の使い方は無視し、次の種類の明らかな誤りだけを探します。
- 語彙の選択ミス(例: 飲み物・食べ物に「暖かい」ではなく「温かい」)
- 同音異義語の漢字の誤り(例: 計る/測る/量る、変える/替える)
- 助詞の誤用
- 文体の不統一(だ・である体の中に です・ます が混ざる)

【ルール】
- 明らかな誤りだけ。「もっと自然な言い方がある」程度や好みの問題は書かない
- 迷ったら書かない。誤りがなければ「なし」とだけ書く
- 出力は次の形式のみ、1誤りにつき1行。他の文章は書かない:
誤っている語句|正しい語句|種類(語彙/漢字/助詞/文体)|理由(40字以内)

【文章】
{passage}"""


def parse(raw: str) -> list:
    """[{wrong, right, kind, reason}] — 형식이 안 맞는 줄은 버린다. 「なし」/빈 응답은 []."""
    out = []
    for line in (raw or "").strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1] or parts[0] == parts[1]:
            continue
        out.append({"wrong": parts[0], "right": parts[1], "kind": parts[2],
                    "reason": parts[3] if len(parts) > 3 else ""})
    return out
