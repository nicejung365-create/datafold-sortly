from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="챗봇")

ENV_PATH = Path(__file__).parent / ".env"
# 공식 문서(ai.google.dev/gemini-api/docs/interactions)의 REST 예시와 같은 주소
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# 문서 예시 모델(gemini-3.8-flash)은 이 키로 응답이 오지 않아, 응답이 확인된 모델을 씀
GEMINI_MODEL = "gemini-3.5-flash-lite"


def load_env():
    # 내 컴퓨터에서는 .env 를, .env 가 없는 Streamlit Cloud 에서는 앱 설정의 Secrets 를 같은 이름으로 읽음
    if not ENV_PATH.exists():
        try:
            return {key: str(value) for key, value in st.secrets.items()}
        except Exception:  # Secrets 도 없으면 빈 묶음
            return {}
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def ask_gemini(api_key, message):
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"model": GEMINI_MODEL, "input": message}
    res = requests.post(GEMINI_URL, headers=headers, json=body, timeout=60)
    res.raise_for_status()
    # 답은 steps 중 type 이 "model_output" 인 단계의 content 안 text 에 들어 있음
    texts = [
        part.get("text", "")
        for step in res.json().get("steps", [])
        if step.get("type") == "model_output"
        for part in step.get("content", [])
        if part.get("type") == "text"
    ]
    return "".join(texts)


def error_text(e):
    if e.response is not None and e.response.text:
        return f"{e} — {e.response.text}"
    return str(e)


env = load_env()
api_key = env.get("GEMINI_API_KEY", "")

st.title("챗봇")

# 키 값은 화면에 보여주지 않고, 채워져 있는지만 알려 줌
if api_key:
    st.write("✅ .env 의 GEMINI_API_KEY 가 채워져 있습니다.")
else:
    st.write("❌ .env 의 GEMINI_API_KEY 가 비어 있습니다.")

message = st.text_input("물어볼 말", placeholder="Gemini 에게 보낼 말을 입력하세요")

if st.button("보내기"):
    if not api_key:
        st.warning(".env 에 GEMINI_API_KEY 를 먼저 채워 주세요.")
    elif not message.strip():
        st.warning("보낼 말을 입력해 주세요.")
    else:
        with st.spinner("Gemini 가 답을 쓰는 중입니다..."):
            try:
                answer = ask_gemini(api_key, message.strip())
            except requests.RequestException as e:
                st.error(f"Gemini 에게서 답을 받지 못했습니다: {error_text(e)}")
            else:
                if answer:
                    st.markdown(answer)
                else:
                    st.warning("Gemini 가 빈 답을 보냈습니다.")
