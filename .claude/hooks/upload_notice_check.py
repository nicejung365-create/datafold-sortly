# 파일 맨 위 설명글(docstring): 이 훅이 무엇을 하는지와 근거 규칙 파일을 적어 둔다 (1~3줄은 한 덩어리 문자열이라 안에는 주석을 넣지 않음)
"""안내 문구 검사: "분류하기" 화면 HTML에 빈 업로드 안내 문구가 그대로 없으면 Write/Edit 를 막는 PreToolUse 훅.
근거: .claude/rules/screen.md
"""
# 윈도우 경고창(MessageBox)을 띄우는 데 쓰는 도구
import ctypes
# Claude Code 가 넘겨주는 입력(JSON)을 읽고, 막는다는 답(JSON)을 만드는 데 쓰는 도구
import json
# 파일 경로를 다루는 도구
import os
# 경고창을 따로 떨어진 프로세스로 띄우는 데 쓰는 도구
import subprocess
# 표준 입력·출력과 실행 인자를 다루는 도구
import sys

# 이 스크립트 위치(.claude/hooks/)에서 두 단계 위로 올라간 프로젝트 폴더 경로
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 이 글자가 들어 있는 HTML 만 "화면 1" 로 보고 검사한다
TRIGGER = "분류하기"
# 화면 1 에 한 글자도 다르지 않게 들어 있어야 하는 안내 문구 (screen.md 에서 그대로 옮김)
NOTICE = "업로드된 파일이 없습니다. 파일을 업로드 후 이용해주세요."

# 경고창 제목
TITLE = "안내 문구 검사"
# 경고창 내용
MESSAGE = "[안내 문구 빠짐]"
# 막을 때 AI 에게 돌려줄 이유 문장
REASON = f'"{TRIGGER}" 화면에는 .claude/rules/screen.md 의 안내 문구 "{NOTICE}" 를 한 글자도 바꾸지 말고 그대로 넣어라'

# 경고창 버튼: [확인] 하나만
MB_OK = 0x0
# 경고창 아이콘: 노란 느낌표
MB_ICONWARNING = 0x30
# 다른 창보다 먼저 응답해야 하는 시스템 경고창으로 띄움
MB_SYSTEMMODAL = 0x1000
# 화면 맨 앞에 띄움
MB_TOPMOST = 0x40000
# 경고창용 프로세스를 띄울 때 검은 명령 창이 보이지 않게 함
CREATE_NO_WINDOW = 0x08000000


# 경고창을 띄우는 함수
def show_popup():
    # 위에서 정한 내용·제목·버튼·아이콘·맨 앞 옵션으로 윈도우 경고창을 띄운다
    ctypes.windll.user32.MessageBoxW(None, MESSAGE, TITLE, MB_OK | MB_ICONWARNING | MB_SYSTEMMODAL | MB_TOPMOST)


# 지금 디스크에 있는 파일 내용을 읽어 오는 함수
def read_file(path):
    # 읽다가 문제가 생겨도 훅이 멈추지 않게 감싼다
    try:
        # 파일을 UTF-8(한글이 깨지지 않는 방식)로 연다
        with open(path, encoding="utf-8") as f:
            # 파일 전체 내용을 돌려준다
            return f.read()
    # 파일이 없거나 UTF-8 로 읽을 수 없으면
    except (OSError, UnicodeDecodeError):
        # "내용을 알 수 없음" 을 뜻하는 None 을 돌려준다
        return None


# AI 가 쓰려는 대로 쓰고 나면 파일 내용이 어떻게 될지 계산하는 함수
def content_after(tool_name, tool_input, path):
    # 함수 설명글
    """쓰고 난 뒤의 파일 내용. 알 수 없으면 None."""
    # Write 는 파일 전체를 새로 쓰는 도구라서
    if tool_name == "Write":
        # 넘어온 내용이 곧 쓰고 난 뒤의 내용이다
        return tool_input.get("content", "")
    # Edit 는 파일의 일부만 바꾸는 도구라서
    if tool_name == "Edit":
        # 먼저 지금 파일 내용을 읽는다
        current = read_file(path)
        # 지금 내용을 읽을 수 없으면
        if current is None:
            # 결과도 알 수 없으므로 None 을 돌려준다
            return None
        # 바꿀 원래 글자
        old = tool_input.get("old_string", "")
        # 새로 넣을 글자
        new = tool_input.get("new_string", "")
        # "모두 바꾸기" 옵션이 켜져 있으면
        if tool_input.get("replace_all"):
            # 원래 글자를 전부 새 글자로 바꾼 결과를 돌려준다
            return current.replace(old, new)
        # 아니면 처음 한 곳만 바꾼 결과를 돌려준다
        return current.replace(old, new, 1)
    # Write·Edit 가 아닌 도구는 결과를 알 수 없으므로 None
    return None


# 막아야 하면 True, 그냥 둬도 되면 False 를 돌려주는 판정 함수
def should_block(data):
    # 도구에 넘어온 입력값을 꺼낸다 (없으면 빈 묶음)
    tool_input = data.get("tool_input") or {}
    # 쓰려는 파일 경로를 꺼낸다
    path = tool_input.get("file_path", "")
    # 파일 이름이 .html 이나 .htm 으로 끝나지 않으면
    if not path.lower().endswith((".html", ".htm")):
        # 검사하지 않고 통과시킨다
        return False  # HTML 이 아니면 그대로 진행
    # 경로가 프로젝트 기준의 상대 경로이면
    if not os.path.isabs(path):
        # 프로젝트 폴더를 앞에 붙여 전체 경로로 만든다
        path = os.path.join(PROJECT_DIR, path)
    # 쓰고 난 뒤의 파일 내용을 계산한다
    after = content_after(data.get("tool_name"), tool_input, path)
    # 내용을 알 수 없거나 "분류하기" 가 들어 있지 않으면
    if after is None or TRIGGER not in after:
        # 화면 1 이 아니므로 통과시킨다
        return False  # "분류하기" 화면이 아니면 그대로 진행
    # 화면 1 인데 안내 문구가 그대로 없으면 True(막기), 있으면 False(통과)
    return NOTICE not in after


# 훅이 실행되면 처음 도는 본체 함수
def main():
    # Claude Code 가 표준 입력으로 넘겨준 JSON 을 UTF-8 로 읽어 풀어낸다 (비어 있으면 빈 묶음)
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    # 막을 필요가 없으면
    if not should_block(data):
        # 아무것도 출력하지 않고 끝낸다 (그러면 쓰기가 그대로 진행된다)
        return

    # 경고창은 별도 프로세스로 띄워서 훅이 창 닫힐 때까지 기다리지 않게 한다
    # 이 스크립트를 "--popup" 인자로 한 번 더 실행해서 경고창만 띄우게 한다
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--popup"],
                     # 검은 명령 창은 숨기고, 이 프로세스와 연결을 끊은 채로 띄운다
                     creationflags=CREATE_NO_WINDOW, close_fds=True)

    # Claude Code 에 돌려줄 "막는다" 답을 만든다
    out = {
        # PreToolUse 훅이 쓰는 답 형식
        "hookSpecificOutput": {
            # 이 답이 PreToolUse(도구 쓰기 전) 훅의 답임을 알린다
            "hookEventName": "PreToolUse",
            # 이번 도구 사용을 거절(deny)한다
            "permissionDecision": "deny",
            # 거절 이유: AI 가 읽고 고칠 수 있도록 넣는 문장
            "permissionDecisionReason": REASON,
        }
    # 답 묶음 끝
    }
    # 만든 답을 JSON 글자로 바꿔 표준 출력으로 내보낸다
    sys.stdout.write(json.dumps(out))


# 이 파일을 직접 실행했을 때만 아래를 돈다
if __name__ == "__main__":
    # 실행 인자에 "--popup" 이 있으면 (위 Popen 으로 띄운 경우)
    if "--popup" in sys.argv:
        # 경고창만 띄운다
        show_popup()
    # 아니면 (Claude Code 가 훅으로 실행한 경우)
    else:
        # 판정하고 필요하면 막는다
        main()
