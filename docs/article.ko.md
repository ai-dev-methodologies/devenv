[English](article.md)

# 문장으로 적은 규칙이 지켜지지 않을 때: Claude Code의 컨텍스트 절약을 훅으로 강제하기

이 글은 컨텍스트 절약 규칙을 훅으로 강제하고, 그 강제가 조용히 꺼지지 않게 재생으로 확인하는 Claude Code 플러그인 devenv를 다룬다.

## 문제: 큰 출력 한 번에 규칙이 무너진다

긴 Claude Code 세션에는 두 가지 문제가 있다. 첫째는 메인 세션의 컨텍스트다. 「필요한 줄만 읽는다」 같은 규칙은 지시 파일에 적혀 있고, 모델은 대체로 이를 따른다. 그러나 에이전트 목록 조회 한 번이나 범위 없는 파일 읽기 한 번이 대화에 수천 줄을 넣는다. 그 줄은 세션이 끝날 때까지 남는다.

규약으로만 지키는 규칙은 비용이 가장 큰 순간에 깨졌다. 지시 파일 자체도 부담이다. 이 플러그인을 만든 머신에서는 세션 시작 때 로드되는 지시 파일이 55,382바이트였고, 정해 둔 상한은 49,152바이트였다.

둘째는 새 세션으로 넘어갈 때다. 새 세션은 적어 둔 것에서 시작한다. 빠진 것은 결정과 그 이유, 버린 접근, 돌고 있는 작업의 상태였다.

devenv는 두 문제를 플러그인 하나로 다룬다. 첫째는 훅이 강제하는 lean 모드가 맡는다. 둘째는 스크립트가 검사하는 handoff·resume 한 쌍이 맡는다.

## 구조: 공개 엔진과 비공개 개인층

플러그인이 담을 수 있는 범위가 층 구분을 정한다.

| 구분 | 항목 |
|---|---|
| 담을 수 있다 | 스킬·명령·에이전트·훅·MCP와 LSP 서버·출력 스타일·`bin/`·`userConfig`, 그리고 `agent`·`subagentStatusLine` 두 키만 된 `settings.json` |
| 담을 수 없다 | 규칙 파일·`CLAUDE.md`(플러그인 루트에 두어도 로드되지 않는다)·권한(permissions)·환경 변수·상태 줄 |

그래서 devenv는 두 층으로 나뉜다.

| 층 | 담는 것 | 배포 |
|---|---|---|
| 엔진(공개 플러그인) | 스킬·훅·스크립트·기본값 | 마켓플레이스에서 설치한다. 인증이 필요 없다 |
| 개인층(비공개 저장소) | 규칙 파일·`CLAUDE.md`·에이전트 정의 | 복사 스크립트가 놓는다. settings 값은 옮기지 않고, 빠진 항목 이름은 doctor가 보고한다 |

사람이나 프로젝트마다 다른 동작은 규칙 층에서 읽고, 다음 순서로 적용한다.

1. 엔진 기본값: `scripts/rules_layer.py`의 상수
2. 사용자 층: `~/.claude/devenv.local.md`
3. 프로젝트 층: `<toplevel>/.claude/devenv.local.md`

`## <id>` 절 하나가 덮어쓰기 지점 하나다. 프로젝트 층에서 목록 밖의 절은 적용되지 않고 경고를 출력한다. 규칙 파일은 명령을 실행할 때 읽으므로 시작 로드에 더해지지 않는다. 강제 기준값도 이 층에 있다. `scripts/rules_layer.py`의 기본값은 다음과 같다.

```python
DEFAULT_LIMITS = {"read_bytes": 32768, "read_lines": 400, "bash_bytes": 16384, "bash_lines": 200,
                  "bash_head_lines": 40, "return_lines": 30}
```

프로젝트 층 파일은 git이 무시하게 둔다. 이 파일은 그 저장소에서 한 사람이 작업을 돌리는 방식을 적는다(승인 지점·critic 레인·장부 명령 등). 코드에 관한 사실이 아니고, 공유 이력에 남으면 안 되는 이름이 들어갈 수 있다. 파일을 만드는 명령은 `.gitignore`에 넣을 줄을 출력하고, 파일이 무시되지 않으면 doctor가 경고한다.

## 훅이 하는 일과 먼저 실측한 것

훅을 만들기 전에 헤드리스 세션(`claude -p`)을 16회 실행했다. 입력을 기록하는 임시 플러그인을 썼고, Read 훅을 만드는 중에 2회를 더 실행했다. 일부 결과는 설계를 바꿨다.

| 스크립트 | 이벤트 | lean 모드에서 하는 일 |
|---|---|---|
| `hook_read.py` | PreToolUse(Read·Write·Edit·Bash) | 줄 범위 없는 큰 Read와, 모델이 lean 모드를 끄려는 호출을 거부한다 |
| `hook_bash.py` | PostToolUse(Bash) | 큰 출력을 앞부분과 전체 출력 파일 경로로 바꾼다 |
| `hook_return.py` | SubagentStop·PreToolUse(SubagentHandback) | 30줄을 넘는 서브에이전트 반환을 한 번 돌려보내 줄이게 한다 |
| `hook_session_start.py` | SessionStart | 시작 로드를 기록하고, lean 세션에 규칙을 다시 알린다 |

**훅은 서브에이전트의 도구 호출에도 걸린다.** 서브에이전트 안의 PreToolUse 입력에는 `agent_id`와 `agent_type`가 있다. 메인 스레드의 입력에는 둘 다 없다. Read·Bash 훅은 `agent_id`가 없을 때만 동작한다. 이 조건이 없으면 큰 읽기를 위임받은 서브에이전트까지 거부된다.

**Bash 출력 크기는 실행 전에 알 수 없다.** 명령 문자열만으로는 출력이 얼마나 클지 알 수 없다. 그래서 Bash 훅은 실행 뒤 PostToolUse에서 `updatedToolOutput`을 돌려준다. 교체본은 출력 객체의 키를 보존해야 한다. 필수 키 4개(`stdout`·`stderr`·`interrupted`·`isImage`)를 모두 남기면 모델이 교체본을 봤다. `stdout`만 넣거나 문자열 하나로 돌려주면 모델은 원본을 봤고, 스트림에는 훅이 성공으로 찍혔다. 흔적은 디버그 로그의 한 줄뿐이었다.

```
[ERROR] PostToolUse hook returned updatedToolOutput that does not match Bash's output shape: reason=schema_invalid issues=invalid_type
```

지금 훅은 원래 `tool_response`를 복사해 `stdout`·`stderr`만 바꾸고, 출력하기 전에 형식을 검사한다.

**자동 권한 모드에서는 SubagentStop 차단이 무시된다.** 기본 모드에서는 SubagentStop 입력의 `last_assistant_message`에 반환 본문이 있다. 차단하면 서브에이전트가 반환을 다시 쓰고, 훅은 두 번째 호출(`stop_hook_active: true`)을 통과시킨다.

자동 모드에서는 서브에이전트가 `SubagentHandback` 도구 호출로 보고한다. 이때 SubagentStop 입력에는 본문 필드가 없다. 디버그 로그에는 `Stop hook block discarded (turn ended by tool result, no model re-invoke)`가 남는다. 그래서 게이트를 이 도구 호출에도 건다. `hooks/hooks.json`의 등록은 다음과 같다.

```json
{
  "matcher": "SubagentHandback",
  "hooks": [
    {
      "type": "command",
      "command": "${user_config.python}",
      "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/hook_return.py", "handback", "--data", "${CLAUDE_PLUGIN_DATA}"]
    }
  ]
}
```

PreToolUse에서 이 도구 호출을 거부하자, 서브에이전트는 보고를 다시 써서 도구를 다시 불렀다. 훅은 `agent_id`마다 한 번만 거부한다.

**모델 자동 호출을 끈 스킬은 명령 파일을 거쳐도 호출되지 않는다.** 엔진의 lean 끄기 스킬을 부르는 개인 명령 파일은 `cannot be used with Skill tool due to disable-model-invocation` 오류를 받았다. 모델에게 직접 부르라고 했을 때와 같은 거부다. 스킬은 사용자가 이름을 직접 입력했을 때만 실행됐다. 그래서 lean 모드를 끄려면 사용자가 `/devenv:lean-off`를 직접 입력한다.

**스킬 본문의 실행 줄은 도구 호출 훅을 거치지 않는다.** 한 실행에서 `` !`command` `` 형식의 스킬 줄이 스크립트를 실행했지만, PreToolUse Bash 훅에는 기록이 없었다. 같은 환경의 일반 Bash 호출은 기록됐다. lean 끄기 스킬이 상태 도구의 `lean off` 명령을 실행할 수 있는 이유가 이것이다. 모델이 같은 명령을 실행하면 거부된다. 실행 줄에는 실측 결과가 두 가지 더 있었다.

- 인터프리터를 바로 부르는 줄은 승인이 필요해 스킬 전체가 실패했다.
- `allowed-tools`는 `${user_config.*}`를 치환하지 않는다. 그래서 스킬은 인터프리터를 인자로 받는 `bin/devenv-py` 래퍼를 부른다.

**인터프리터가 없으면 훅은 막지 않는 오류로 끝나고, 알림 하나는 보이지 않는다.** 인터프리터 경로가 없으면 훅 명령을 적는 세 가지 방식이 모두 막지 않는 오류로 끝났다(방식에 따라 exit 1·127·126). 도구 호출과 서브에이전트 반환은 그대로 진행됐다. PreToolUse 훅의 알림은 메인 전사에 남았다. SubagentStop 훅의 알림은 서브에이전트 전사에만 남아 메인 세션에서는 보이지 않았다.

## 강제가 조용히 꺼지지 않게 하기

마지막 실측 결과대로라면 강제 훅은 아무 표시 없이 멈출 수 있다. 이를 세 가지 규칙으로 막는다.

**오류를 성공으로 보고하지 않는다.**
- 입력이 잘못되거나 내부 오류가 나면 훅은 exit 1로 끝나고, 훅 오류로 보인다. exit 0은 깨진 등록을 감추고 exit 2는 도구 호출을 막으므로 쓰지 않는다.
- 읽을 수 없는 lean 상태 파일은 off가 아니라 `unknown`이다. 호출은 통과하고 경고가 보인다.
- `onFailure: "block"`은 쓰지 않는다. 이 값을 쓰면 인터프리터가 없을 때 lean 모드가 꺼진 프로젝트의 호출까지 막힌다.

**모델이 스스로 끌 수 없게 한다.**
- 끄는 길은 사용자가 직접 입력하는 `/devenv:lean-off` 하나다.
- Read 훅은 상태 파일을 고치는 Write·Edit와, 상태 도구를 `lean`·`off` 인자로 실행하는 Bash 호출을 거부한다. 서브에이전트의 호출도 같다. 거부 사유에는 끄는 법을 적지 않는다.
- 시험용 상태 폴더 경로는 `--root` 명령줄 인자로만 받는다. 환경 변수로 받으면 새어 나간 변수 하나가 실제 훅에 빈 상태를 읽혀 강제를 끈다.

**doctor가 훅을 재생한다.**
- 강제 훅마다 임시 상태·데이터 경로로 입력 3개를 재생한다. 위반 입력은 거부되고, 깨끗한 입력과 lean off 입력은 통과해야 한다.
- SubagentStop 게이트는 `stop_hook_active: true` 입력의 통과도 재생한다. Bash 교체는 출력 키 4개를 직접 검사한다.
- 결과 줄마다 증명하는 범위를 적는다. `replay` 줄은 스크립트·인터프리터·등록 명령 줄만 증명한다.
- 하네스가 실제로 훅을 부르는지는 `live` 줄이 개발자 모드의 강제 기록으로 따로 보고한다. 현재 버전을 설치한 뒤 기록이 없으면 `SKIP`이다.
- 출력 줄은 `DOCTOR <id> PASS|WARN|FAIL|SKIP <evidence>`이고, 마지막에 요약 줄이 온다. FAIL은 기계로 확인한 것에만 쓰고, 자동 수정은 하지 않는다.

## 세션 인계

`/devenv:handoff`는 날짜가 붙은 파일을 쓰고, `/devenv:resume-handoff`는 새 세션에서 그 파일을 읽는다. 일은 스크립트가 검사할 수 있는지에 따라 나눈다.

| 스크립트가 판정한다 | 모델이 쓴다 |
|---|---|
| git 상태·프로젝트 키·이전 핸드오프·전사 경로(`handoff_state.py`) | 시도한 것, 실패한 것과 그 이유, 버린 접근 |
| 파일의 완결성(`check_handoff.py`): 열린 항목의 what·why·how, Decisions 행의 빈칸, 진행 중 항목의 키 6개, 변경 기록 없이 바뀐 목표 | 결정과 그 이유 |
| 알려진 토큰 형식의 비밀 값. 종류와 줄만 출력하고 값은 출력하지 않는다 | 다음에 할 정확한 단계 |
| 마지막 `/handoff` 뒤 사용자 메시지 중 파일에 인용되지 않은 것 | |
| 재개 뒤 이어서 진행할지 여부. 인자 `--c`·`--continue`·`계속`만 정하고, 파일의 문구는 근거가 아니다 | |

handoff 스킬은 돌고 있는 작업을 멈추지 않고, 검사기가 exit 0을 낼 때까지 완료를 보고하지 않는다. 재개가 브리핑 전에 하는 호출의 상한은 다음과 같다.

- 스크립트 호출 1회
- Read 3회: 핸드오프 파일 1개와 규칙 파일 최대 2개
- 에이전트 목록 조회 1회
- 40줄 이내로 반환하는 읽기 전용 에이전트 1개

이력 파일은 열린 항목이 가리킬 때만 이 에이전트가 읽는다.

## 한계

다음은 막지 못하고, 스킬 문서에도 그렇게 적혀 있다.

- 출력 형식이 문서에 없는 도구의 큰 출력. 예: 에이전트 목록 조회. 이 작업을 시작하게 한 사고는 아직 막지 못한다.
- 셸 리다이렉션으로 lean 상태 파일을 직접 쓰는 경로.
- 메인 세션 자신의 보고 길이. 이것은 규약으로 남는다.
- 대화형 세션에서만 확인할 수 있는 항목. 예: 스킬의 실행 줄이 승인 창을 띄우는지. `agent_type`가 빈 내부 에이전트도 헤드리스 실행에서는 발화하지 않았고, 훅은 빈 타입을 통과시킨다.

강제가 컨텍스트를 얼마나 아끼는지는 측정하지 않았다. 개발자 모드는 강제가 동작한 횟수를 기록할 뿐 절감량은 기록하지 않는다.

## 설치

엔진 저장소가 마켓플레이스를 겸한다(`https://github.com/ai-dev-methodologies/devenv`). 이 마켓플레이스에서 `devenv` 플러그인을 설치하고, 입력 요청이 나오면 Python 3.10 이상 인터프리터의 절대 경로를 넣는다. 모든 훅과 스킬은 이 인터프리터로 스크립트를 실행한다. `.claude-plugin/plugin.json`의 설정은 다음과 같다.

```json
"userConfig": {
  "python": {
    "type": "string",
    "title": "Python interpreter",
    "description": "Absolute path to a Python 3.10 or later interpreter used to run the plugin scripts.",
    "required": true
  }
}
```

lean 모드는 `/devenv:lean`(프로젝트 단위)이나 `/devenv:lean session`(이 세션만)으로 켠다. `/devenv:lean status`는 상태만 알린다. 끄려면 사용자가 `/devenv:lean-off`를 직접 입력한다. 설치와 사용법의 정본은 저장소 README다.
