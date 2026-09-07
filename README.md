# japanese-study-grammar

일본어 **동사·문법** 학습 메일링. 매일 아침 한 가지 테마를 골라 활용표와 예문을 PDF와 HTML 메일로 보낸다.

읽기 자료를 보내는 [japanese-study](https://github.com/hexartrion-oss/japanese-study)의 자매 리포지토리다.

## 설계 원칙

**활용표는 생성 모델에 맡기지 않는다.** 동사 활용은 정답이 규칙으로 확정되는 영역이라, LLM이 만들면 `話す`의 사역수동을 `話される`로 축약하는 식의 오류가 섞여도 걸러낼 방법이 없다. 그래서 `conjugator.py`가 오단/일단/불규칙 규칙으로 직접 활용형을 만들고, Gemini는 **예문 작성에만** 쓴다. Gemini가 실패해도 활용표는 그대로 발송된다.

## 요일별 테마

| 요일 | 테마 | 레벨 | 키 |
|---|---|---|---|
| 월 | 동사 활용 기초 | N5·N4 | `conjugation` |
| 화 | 자동사·타동사 짝 | N4·N3 | `transitivity` |
| 수 | 수수동사 (주고받기) | N4·N3 | `giving` |
| 목 | 수동·사역·사역수동 | N3·N2 | `voice` |
| 금 | 조건표현 と·ば·たら·なら | N3·N2 | `conditional` |
| 토 | 복합동사 | N2·N1 | `compound` |
| 일 | 경어 동사 (존경어·겸양어) | N2·N1 | `keigo` |

각 테마는 매일 데이터 뱅크에서 5~6항목을 무작위로 뽑으므로 같은 요일이라도 내용이 반복되지 않는다.

## 파일 구성

```
conjugator.py   활용 엔진 (규칙 기반, 외부 의존성 없음)
verbs.py        동사 데이터 뱅크 (레벨별 동사, 자타 짝, 경어, 복합동사, 조건표현)
get_grammar.py  테마 선정 → 항목 구성 → 예문 생성 → PDF → 메일 발송
```

## 설정

리포지토리 Secrets에 다음을 등록한다.

| 이름 | 설명 |
|---|---|
| `GMAIL_ADDRESS` | 발신 Gmail 주소 |
| `GMAIL_APP_PASSWORD` | 2단계 인증 후 발급한 앱 비밀번호 16자리 |
| `EMAIL_RECIPIENTS` | 수신자, 쉼표로 구분 |
| `GEMINI_API_KEY` | Google AI Studio 발급 키 |

로컬 실행은 `.env.example`을 `.env`로 복사해 채운다.

```bash
pip install -r requirements.txt
python get_grammar.py
```

특정 테마만 확인하려면:

```bash
FORCE_THEME=voice python get_grammar.py
```

Actions에서는 **Run workflow**로 테마와 수신자를 지정해 수동 실행할 수 있다. 수동 실행 시 `mail_to`를 채우면 그 주소로만 발송된다.

## 폰트

PDF에 일본어와 한국어가 함께 들어가므로 두 문자를 모두 포함한 폰트가 필요하다. IPA 고딕 계열은 한글 글리프가 없어 해석 부분이 깨진다. 워크플로는 `fonts-noto-cjk`를 설치하고, `find_font()`도 Noto CJK를 우선 탐색한다. 직접 지정하려면 `JAPANESE_FONT_PATH`를 쓴다.

## 확장

- 동사를 추가하려면 `verbs.py`의 `VERBS`에 `(사전형, 요미가나, 그룹, 뜻)`을 넣는다. 그룹은 `G`(오단) / `I`(일단) / `S`(する) / `K`(来る).
- 활용형을 추가하려면 `conjugator.py`에 함수를 만들고 `FORMS`에 등록한 뒤, `get_grammar.py`의 `FORM_SETS`에서 테마별로 골라 쓴다.
- 테마를 추가하려면 `WEEKLY_PLAN`과 `build_items()`에 분기를 더한다.

## 알려진 한계

- `ある`의 부정(`ない`)만 예외 처리되어 있다. `行く` 외의 て형 예외는 아직 없다.
- 예문은 매번 새로 생성되므로 같은 항목이라도 문장이 달라진다. 고정하려면 생성 결과를 캐시해야 한다.
- 복합동사·조건표현·경어는 활용표가 아니라 설명형 항목이라 `conjugate()`를 쓰지 않는다.
