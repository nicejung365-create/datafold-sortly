from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="한 줄 방명록")

ENV_PATH = Path(__file__).parent / ".env"
TABLE = "guestbook"


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


env = load_env()
BASE_URL = f"{env['SUPABASE_URL'].rstrip('/')}/rest/v1/{TABLE}"
HEADERS = {
    "apikey": env["SUPABASE_KEY"],
    "Authorization": f"Bearer {env['SUPABASE_KEY']}",
}


def fetch_messages():
    params = {"select": "id,created_at,message", "order": "created_at.desc"}
    res = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def add_message(message):
    res = requests.post(BASE_URL, headers=HEADERS, json={"message": message}, timeout=10)
    res.raise_for_status()


st.title("한 줄 방명록")

with st.form("guestbook_form", clear_on_submit=True):
    message = st.text_input("한 줄 남기기")
    submitted = st.form_submit_button("남기기")

if submitted:
    if not message.strip():
        st.warning("내용을 입력해 주세요.")
    else:
        try:
            add_message(message.strip())
            st.success("방명록에 남겼습니다.")
        except requests.RequestException as e:
            st.error(f"저장하지 못했습니다: {e}")

st.subheader("남겨진 글")
try:
    rows = fetch_messages()
except requests.RequestException as e:
    st.error(f"글을 불러오지 못했습니다: {e}")
    rows = []

if not rows:
    st.caption("아직 남겨진 글이 없습니다.")
for row in rows:
    st.text(row["message"])
    st.caption(row["created_at"][:16].replace("T", " "))
