"""Claude 키 확인 — .env 에 ANTHROPIC_API_KEY 가 채워져 있는지만 알려 주는 화면.

키 값은 화면에 보여 주지 않음.
실행: streamlit run key_check.py
"""
from pathlib import Path

import streamlit as st

ENV_PATH = Path(__file__).parent / ".env"
KEY_NAME = "ANTHROPIC_API_KEY"

st.set_page_config(page_title="Claude 키 확인")


def load_env():
    # app.py 와 같은 방식으로 .env 를 "이름=값" 줄 단위로 읽음 (파일이 없으면 빈 묶음)
    env = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


st.title("Claude 키 확인")

if not ENV_PATH.exists():
    st.error(".env 파일이 없습니다.")
elif load_env().get(KEY_NAME):
    st.success(f".env 의 {KEY_NAME} 가 채워져 있습니다.")
else:
    st.warning(f".env 의 {KEY_NAME} 가 비어 있습니다.")
