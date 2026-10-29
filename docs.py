import math
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="문서 보관함")

ENV_PATH = Path(__file__).parent / ".env"
BUCKET = "docs"
DOC_TYPES = ["pdf", "docx", "xlsx", "pptx", "hwp", "csv", "txt"]
LINK_SECONDS = 60  # 내려받기 링크가 열리는 시간(초)


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


env = load_env()
# Supabase Storage 공식 문서: https://supabase.com/docs/guides/storage
STORAGE_BASE = f"{env['SUPABASE_URL'].rstrip('/')}/storage/v1"
STORAGE_URL = f"{STORAGE_BASE}/object"
HEADERS = {
    "apikey": env["SUPABASE_KEY"],
    "Authorization": f"Bearer {env['SUPABASE_KEY']}",
}


def make_name(original):
    """Storage 는 한글 파일 이름을 받지 않으므로, 올린 시각 + 짧은 무작위 글자로 이름을 새로 짓는다."""
    ext = Path(original).suffix.lower()
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}{ext}"


def upload_doc(file):
    res = requests.post(
        f"{STORAGE_URL}/{BUCKET}/{make_name(file.name)}",
        headers={**HEADERS, "Content-Type": file.type or "application/octet-stream"},
        data=file.getvalue(),
        timeout=30,
    )
    res.raise_for_status()


def fetch_doc_list():
    """버킷 안의 파일 목록을 최근에 올린 것부터 불러온다."""
    body = {
        "prefix": "",
        "limit": 1000,
        "offset": 0,
        "sortBy": {"column": "created_at", "order": "desc"},
    }
    res = requests.post(f"{STORAGE_URL}/list/{BUCKET}", headers=HEADERS, json=body, timeout=10)
    res.raise_for_status()
    # id 가 없는 것은 폴더이므로 빼고, 파일만 남긴다
    return [item for item in res.json() if item.get("id")]


def make_signed_links(names):
    """문서마다 LINK_SECONDS 초 동안만 열리는 내려받기 링크를 한 번에 만든다. {이름: 링크}"""
    body = {"expiresIn": LINK_SECONDS, "paths": names}
    res = requests.post(f"{STORAGE_URL}/sign/{BUCKET}", headers=HEADERS, json=body, timeout=10)
    res.raise_for_status()
    # download= 를 붙이면 브라우저에서 열지 않고 파일로 내려받는다
    return {
        item["path"]: f"{STORAGE_BASE}{item['signedURL']}&download="
        for item in res.json()
        if item.get("signedURL")
    }


def to_korea_time(text):
    # Supabase 는 UTC 로 돌려주므로 한국 시간으로 바꿔 보여 줌
    utc = datetime.fromisoformat(text[:19])
    return (utc + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")


def size_text(size):
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    return f"{size / 1024:.0f}KB"


st.title("문서 보관함")

with st.form("upload_form", clear_on_submit=True):
    files = st.file_uploader("올릴 문서 고르기", type=DOC_TYPES, accept_multiple_files=True)
    submitted = st.form_submit_button("올리기")

uploaded = False
if submitted:
    if not files:
        st.warning("올릴 문서를 골라 주세요.")
    else:
        done = 0
        for file in files:
            try:
                upload_doc(file)
                done += 1
            except requests.RequestException as e:
                st.error(f"{file.name} 을(를) 올리지 못했습니다: {e}")
        if done:
            st.success(f"문서 {done}개를 올렸습니다.")
            uploaded = True

st.subheader("올라와 있는 문서")
clicked = st.button("링크 새로 만들기")
st.caption(f"내려받기 링크는 만든 뒤 {LINK_SECONDS}초 동안만 열립니다. 시간이 지나면 [링크 새로 만들기]를 눌러 주세요.")

# 링크는 처음 열 때, 문서를 올렸을 때, [링크 새로 만들기]를 눌렀을 때만 새로 만들고
# 만든 시각과 함께 기억해 둔다 — 화면이 다시 그려질 때마다 새로 만들면 타이머가 맞지 않음
if clicked or uploaded or "links_made_at" not in st.session_state:
    try:
        docs = fetch_doc_list()
        st.session_state.docs = docs
        st.session_state.links = make_signed_links([doc["name"] for doc in docs]) if docs else {}
    except requests.RequestException as e:
        st.error(f"문서 목록을 불러오지 못했습니다: {e}")
        st.session_state.docs, st.session_state.links = [], {}
    st.session_state.links_made_at = time.time()


def seconds_left():
    return math.ceil(LINK_SECONDS - (time.time() - st.session_state.links_made_at))


# 링크가 살아 있는 동안만 1초마다 이 부분(타이머와 버튼)만 다시 그린다
ticking = seconds_left() > 0


@st.fragment(run_every=1 if ticking else None)
def show_docs():
    docs, links = st.session_state.docs, st.session_state.links
    if not docs:
        st.caption("아직 올라온 문서가 없습니다.")
        return

    left = seconds_left()
    expired = left <= 0
    if expired:
        st.warning("내려받기 링크 시간이 지났습니다. [링크 새로 만들기]를 눌러 주세요.")
    else:
        st.info(f"내려받기 링크 남은 시간: {left}초")
    st.caption(f"모두 {len(docs)}개")

    for doc in docs:
        name_col, button_col = st.columns([4, 1])
        with name_col:
            st.text(doc["name"])
            size = (doc.get("metadata") or {}).get("size", 0)
            st.caption(f"{to_korea_time(doc['created_at'])} · {size_text(size)}")
        with button_col:
            if doc["name"] in links:
                st.link_button("내려받기", links[doc["name"]], disabled=expired, key=f"down_{doc['name']}")
            else:
                st.caption("링크를 만들지 못했습니다")

    # 시간이 다 되면 화면 전체를 한 번 다시 그려 1초마다 다시 그리기를 멈춘다
    if ticking and expired:
        st.rerun()


show_docs()
