"""PRD 자물쇠: AI가 PRD.md 를 Write/Edit 하거나 명령(Bash/PowerShell)으로 바꾸기 전에 막는 PreToolUse 훅."""
import ctypes
import json
import os
import re
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRD_PATH = os.path.normcase(os.path.join(PROJECT_DIR, "PRD.md"))

TITLE = "PRD 자물쇠"
MESSAGE = "[다시생각해봐]"
REASON = "PRD.md 는 잠겨 있으니 고치지 말고 바꿀 내용을 제안만 하라"

MB_OK = 0x0
MB_ICONWARNING = 0x30
MB_SYSTEMMODAL = 0x1000
MB_TOPMOST = 0x40000
CREATE_NO_WINDOW = 0x08000000

# 명령 안에서 PRD.md 를 가리키는 부분 (경로 앞부분·따옴표 허용, OLD_PRD.md 같은 다른 이름은 제외)
PRD = r"""["']?(?:[^\s"'|;&<>]*[\\/])?PRD\.md["']?"""

# PRD.md 를 바꾸는 명령 모양. 읽기 명령(cat, head, tail, grep, Get-Content 등)은 여기에 없으므로 막지 않는다.
WRITE_PATTERNS = [
    rf">>?\|?\s*{PRD}",                                        # 리다이렉트: > PRD.md, >> PRD.md
    rf"\btee\b[^|;&]*{PRD}",                                   # tee PRD.md
    rf"\b(?:sed|perl)\b[^|;&]*\s-[a-zA-Z]*i[^|;&]*{PRD}",      # sed -i / perl -i
    rf"\b(?:rm|mv|truncate|touch|ln|unlink|shred|chmod|attrib)\b[^|;&]*{PRD}",
    rf"\b(?:cp|copy|Copy-Item|cpi)\b[^|;&]*{PRD}\s*(?:$|[|;&)])",  # 복사 대상(맨 끝 인자)이 PRD.md
    rf"\bdd\b[^|;&]*\bof={PRD}",
    rf"\b(?:Set-Content|sc|Add-Content|ac|Out-File|Clear-Content|clc|Remove-Item|ri|del|erase|rd|rmdir"
    rf"|Move-Item|mi|move|Rename-Item|rni|ren|New-Item|ni)\b[^|;&]*{PRD}",
    rf"\bgit\b[^|;&]*\b(?:checkout|restore|reset|stash|apply|mv|rm)\b[^|;&]*{PRD}",
    rf"open\([^)]*{PRD}[^)]*,\s*(?:mode\s*=\s*)?[rb]?[\"'][^\"']*[wax+]",  # 파이썬 open(..., 'w'/'a')
    rf"{PRD}[^;\n]*\.(?:write_text|write_bytes|open\([\"'][^\"']*[wax+]|unlink|rename|replace)\(",
    rf"(?:WriteAll\w*|AppendAll\w*|File\]::(?:Delete|Move|Copy|Replace|Create|Open\w*))\([^)]*{PRD}",
    rf"\bos\.(?:remove|unlink|rename|replace)\([^)]*{PRD}",
    rf"\bshutil\.\w+\([^)]*{PRD}",
]
WRITE_RE = [re.compile(p, re.IGNORECASE) for p in WRITE_PATTERNS]


def show_popup():
    ctypes.windll.user32.MessageBoxW(None, MESSAGE, TITLE, MB_OK | MB_ICONWARNING | MB_SYSTEMMODAL | MB_TOPMOST)


def is_prd(file_path):
    if not file_path:
        return False
    if not os.path.isabs(file_path):
        file_path = os.path.join(PROJECT_DIR, file_path)
    return os.path.normcase(os.path.abspath(file_path)) == PRD_PATH


def command_changes_prd(command):
    if not command or not re.search(r"(?<![\w.-])PRD\.md", command, re.IGNORECASE):
        return False  # PRD.md 를 언급하지 않는 명령은 그대로 진행
    return any(r.search(command) for r in WRITE_RE)


def should_block(data):
    tool_input = data.get("tool_input") or {}
    if data.get("tool_name") in ("Bash", "PowerShell"):
        return command_changes_prd(tool_input.get("command", ""))
    return is_prd(tool_input.get("file_path", ""))


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    if not should_block(data):
        return  # 다른 파일·읽기 명령은 그대로 진행

    # 경고창은 별도 프로세스로 띄워서 훅이 창 닫힐 때까지 기다리지 않게 한다
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--popup"],
                     creationflags=CREATE_NO_WINDOW, close_fds=True)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    if "--popup" in sys.argv:
        show_popup()
    else:
        main()
