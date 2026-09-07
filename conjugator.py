"""동사 활용 엔진 — 규칙 기반(LLM 미사용).

활용형은 정답이 확정되어 있으므로 생성 모델에 맡기지 않는다.
Gemini는 '예문 작성'에만 사용하고, 활용표 자체는 이 모듈이 만든다.
"""

# ── 오단(五段) 활용을 위한 행(行) 매핑 ────────────────
# 사전형 말미 かな → (a단, i단, e단, o단)
_ROW = {
    "う": ("わ", "い", "え", "お"),  # 買う → 買わない (あ가 아니라 わ)
    "く": ("か", "き", "け", "こ"),
    "ぐ": ("が", "ぎ", "げ", "ご"),
    "す": ("さ", "し", "せ", "そ"),
    "つ": ("た", "ち", "て", "と"),
    "ぬ": ("な", "に", "ね", "の"),
    "ぶ": ("ば", "び", "べ", "ぼ"),
    "む": ("ま", "み", "め", "も"),
    "る": ("ら", "り", "れ", "ろ"),
}

# 오단 て형 음편(音便)
_TE_GODAN = {
    "う": "って", "つ": "って", "る": "って",
    "む": "んで", "ぶ": "んで", "ぬ": "んで",
    "く": "いて", "ぐ": "いで",
    "す": "して",
}

# 예외: 行く는 いて가 아니라 って
_TE_EXCEPTION = {"行く": "行って", "いく": "いって"}

GODAN, ICHIDAN, SURU, KURU = "godan", "ichidan", "suru", "kuru"


def _tail(verb: str) -> str:
    return verb[-1]


def _stem(verb: str) -> str:
    """어간(사전형에서 마지막 かな 제거)."""
    return verb[:-1]


def _godan(verb: str, col: int) -> str:
    """오단 어간 + 지정한 단(0=a,1=i,2=e,3=o)."""
    return _stem(verb) + _ROW[_tail(verb)][col]


def masu_stem(verb: str, group: str) -> str:
    """ます형 어간. 書く→書き / 食べる→食べ / する→し / 来る→来."""
    if group == GODAN:
        return _godan(verb, 1)
    if group == ICHIDAN:
        return _stem(verb)
    if group == SURU:
        return verb[:-2] + "し" if len(verb) > 2 else "し"
    return verb[:-2] + "来" if len(verb) > 2 else "来"  # 来る → 来(き)


def te_form(verb: str, group: str) -> str:
    if verb in _TE_EXCEPTION:
        return _TE_EXCEPTION[verb]
    if group == GODAN:
        return _stem(verb) + _TE_GODAN[_tail(verb)]
    if group == ICHIDAN:
        return _stem(verb) + "て"
    if group == SURU:
        return masu_stem(verb, group) + "て"
    return masu_stem(verb, group) + "て"


def ta_form(verb: str, group: str) -> str:
    """た형은 て형의 て/で를 た/だ로 치환."""
    te = te_form(verb, group)
    return te[:-1] + ("た" if te[-1] == "て" else "だ")


def nai_form(verb: str, group: str) -> str:
    if verb in ("ある",):
        return "ない"
    if group == GODAN:
        return _godan(verb, 0) + "ない"
    if group == ICHIDAN:
        return _stem(verb) + "ない"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "しない"
    return (verb[:-2] if len(verb) > 2 else "") + "来ない"


def potential(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 2) + "る"
    if group == ICHIDAN:
        return _stem(verb) + "られる"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "できる"
    return (verb[:-2] if len(verb) > 2 else "") + "来られる"


def passive(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 0) + "れる"
    if group == ICHIDAN:
        return _stem(verb) + "られる"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "される"
    return (verb[:-2] if len(verb) > 2 else "") + "来られる"


def causative(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 0) + "せる"
    if group == ICHIDAN:
        return _stem(verb) + "させる"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "させる"
    return (verb[:-2] if len(verb) > 2 else "") + "来させる"


def causative_passive(verb: str, group: str) -> str:
    """사역수동. 오단은 축약형(〜される)이 실사용에서 일반적이나
    す로 끝나는 동사는 축약하지 않는다(話す→話させられる)."""
    if group == GODAN:
        if _tail(verb) == "す":
            return _godan(verb, 0) + "せられる"
        return _godan(verb, 0) + "される"
    if group == ICHIDAN:
        return _stem(verb) + "させられる"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "させられる"
    return (verb[:-2] if len(verb) > 2 else "") + "来させられる"


def volitional(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 3) + "う"
    if group == ICHIDAN:
        return _stem(verb) + "よう"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "しよう"
    return (verb[:-2] if len(verb) > 2 else "") + "来よう"


def imperative(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 2)
    if group == ICHIDAN:
        return _stem(verb) + "ろ"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "しろ"
    return (verb[:-2] if len(verb) > 2 else "") + "来い"


def conditional_ba(verb: str, group: str) -> str:
    if group == GODAN:
        return _godan(verb, 2) + "ば"
    if group == ICHIDAN:
        return _stem(verb) + "れば"
    if group == SURU:
        return (verb[:-2] if len(verb) > 2 else "") + "すれば"
    return (verb[:-2] if len(verb) > 2 else "") + "来れば"


def conditional_tara(verb: str, group: str) -> str:
    return ta_form(verb, group) + "ら"


def masu(verb: str, group: str) -> str:
    return masu_stem(verb, group) + "ます"


def masen(verb: str, group: str) -> str:
    return masu_stem(verb, group) + "ません"


def mashita(verb: str, group: str) -> str:
    return masu_stem(verb, group) + "ました"


def teiru(verb: str, group: str) -> str:
    return te_form(verb, group) + "いる"


# ── 활용형 카탈로그 ───────────────────────────────────
# key: (한국어 라벨, 함수, 대표 레벨)
FORMS = {
    "masu":        ("ます형 (정중)", masu, "N5"),
    "masen":       ("ません형 (정중 부정)", masen, "N5"),
    "mashita":     ("ました형 (정중 과거)", mashita, "N5"),
    "te":          ("て형", te_form, "N5"),
    "ta":          ("た형 (과거)", ta_form, "N5"),
    "nai":         ("ない형 (부정)", nai_form, "N5"),
    "teiru":       ("ている형 (진행·상태)", teiru, "N4"),
    "potential":   ("가능형", potential, "N4"),
    "volitional":  ("의지형", volitional, "N4"),
    "ba":          ("ば 조건형", conditional_ba, "N4"),
    "tara":        ("たら 조건형", conditional_tara, "N4"),
    "passive":     ("수동형", passive, "N4"),
    "causative":   ("사역형", causative, "N4"),
    "imperative":  ("명령형", imperative, "N4"),
    "caus_pass":   ("사역수동형", causative_passive, "N3"),
}


def conjugate(verb: str, group: str, form_keys=None) -> list:
    """[(라벨, 활용형)] 반환."""
    keys = form_keys or list(FORMS.keys())
    out = []
    for k in keys:
        label, fn, _ = FORMS[k]
        try:
            out.append((label, fn(verb, group)))
        except KeyError:
            continue
    return out
