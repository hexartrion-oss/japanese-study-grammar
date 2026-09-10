---
name: remember
description: Append a short, dated note to this repo's CLAUDE.md so it survives session loss and is auto-loaded in every future Claude Code session opened in this project. Use whenever the user types /remember, or asks to "기억해줘", "세션 끊겨도 남게 기록해줘", "나중에 확인해줘", "remember this", or similar — especially right after scheduling something ephemeral (a CronCreate reminder, a ScheduleWakeup) that will NOT survive if this session ends before it fires.
---

# /remember

CronCreate나 ScheduleWakeup 같은 예약은 이 세션에만 존재한다 — 세션이 끊기면
예약도 같이 사라진다. 반면 이 리포의 `CLAUDE.md`는 Claude Code가 이 리포에서
새 세션을 열 때마다 자동으로 읽는 파일이라, git에 커밋만 해두면 세션이
바뀌어도(심지어 다른 사람이 열어도) 다시 보인다. 이 스킬은 그 차이를 메운다 —
"세션 전용 기억"을 "리포에 적힌, 누구나·언제나 다시 보는 기억"으로 바꾼다.

## 할 일

1. 리포 루트에 `CLAUDE.md`가 없으면 새로 만든다. 처음 만들 때는 아래 한 줄이면
   충분하다 — 거창한 템플릿을 짓지 않는다:
   ```
   # japanese-study-grammar — 세션 간 메모

   Claude Code가 이 리포에서 세션을 열 때마다 자동으로 읽는 파일이다. 아래
   "진행 중 메모" 섹션은 /remember 스킬로 관리된다.
   ```
2. `## 진행 중 메모` 섹션이 없으면 파일 끝에 추가한다.
3. 그 섹션 안에 오늘 날짜와 함께 한 줄을 추가한다: `- [YYYY-MM-DD] <기억할 내용>`.
   기존 `CLAUDE.md`의 다른 내용·다른 섹션은 건드리지 않는다 — 이 섹션에만 append한다.
4. 커밋 여부는 평소 git 작업 규칙(다른 변경과 같이 커밋하거나, 사용자가 명시적으로
   요청했을 때 커밋)을 그대로 따른다. 이 스킬 자체가 커밋을 강제하지는 않는다 —
   워킹 트리에만 남아 있어도 다음 커밋 때 같이 올라가면 된다. 다만 정말 세션이
   끊길 위험이 크다면, 그 자리에서 바로 커밋·푸시까지 해두는 편이 안전하다.

## 정리(삭제)는 사람 확인 후에만

이 메모는 기본적으로 사람이 직접 지운다. 다음 세션에서 이 파일을 읽다가 이미
처리된 것으로 보이는 항목을 발견해도, 그냥 지우지 말고 먼저 확인한다: "이 메모
{내용} 처리된 것 같은데 지워도 될까요?" 확인 없이 삭제하지 않는다 — 아직 진행
중인 일을 실수로 지우면 이 스킬의 존재 의미가 없어진다.

## 예시

사용자: "/remember 월요일에 주간 리포트가 정상 발동하는지 확인"

→ `CLAUDE.md`에 다음을 추가(파일이 이미 있고 섹션도 있다면 그 섹션 마지막 줄에만
추가):

```
## 진행 중 메모

- [2026-09-10] 월요일에 주간 리포트가 정상 발동하는지 확인
```
