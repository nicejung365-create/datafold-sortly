import uuid
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="사진 보관함")

ENV_PATH = Path(__file__).parent / ".env"
BUCKET = "photos"
IMAGE_TYPES = ["png", "jpg", "jpeg", "gif", "webp"]


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
STORAGE_URL = f"{env['SUPABASE_URL'].rstrip('/')}/storage/v1/object"
HEADERS = {
    "apikey": env["SUPABASE_KEY"],
    "Authorization": f"Bearer {env['SUPABASE_KEY']}",
}


def make_name(original):
    """Storage 는 한글 파일 이름을 받지 않으므로, 올린 시각 + 짧은 무작위 글자로 이름을 새로 짓는다."""
    ext = Path(original).suffix.lower()
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}{ext}"


def upload_photo(file):
    res = requests.post(
        f"{STORAGE_URL}/{BUCKET}/{make_name(file.name)}",
        headers={**HEADERS, "Content-Type": file.type or "application/octet-stream"},
        data=file.getvalue(),
        timeout=30,
    )
    res.raise_for_status()


def fetch_photo_list():
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


@st.cache_data(show_spinner=False)
def download_photo(name):
    """버킷이 공개가 아니어도 보이도록, 키를 붙여 그림 내용을 직접 받아 온다."""
    res = requests.get(f"{STORAGE_URL}/{BUCKET}/{name}", headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.content


st.title("사진 보관함")

with st.form("upload_form", clear_on_submit=True):
    files = st.file_uploader("올릴 그림 고르기", type=IMAGE_TYPES, accept_multiple_files=True)
    submitted = st.form_submit_button("올리기")

if submitted:
    if not files:
        st.warning("올릴 그림을 골라 주세요.")
    else:
        done = 0
        for file in files:
            try:
                upload_photo(file)
                done += 1
            except requests.RequestException as e:
                st.error(f"{file.name} 을(를) 올리지 못했습니다: {e}")
        if done:
            st.success(f"그림 {done}장을 올렸습니다.")

st.subheader("올라와 있는 그림")
try:
    photos = fetch_photo_list()
except requests.RequestException as e:
    st.error(f"그림 목록을 불러오지 못했습니다: {e}")
    photos = []

if not photos:
    st.caption("아직 올라온 그림이 없습니다.")
else:
    st.caption(f"모두 {len(photos)}장")

for photo in photos:
    try:
        st.image(download_photo(photo["name"]), width="stretch")
    except requests.RequestException as e:
        st.error(f"그림을 불러오지 못했습니다: {e}")
    st.caption(photo["name"])
