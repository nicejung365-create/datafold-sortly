from datetime import date
from pathlib import Path

import requests
import streamlit as st

st.set_page_config(page_title="구독 정리함")

ENV_PATH = Path(__file__).parent / ".env"
TABLE = "subscriptions"


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


env = load_env()
BASE_URL = f"{env['SUPABASE_URL'].rstrip('/')}/rest/v1/{TABLE}"
HEADERS = {
    "apikey": env["SUPABASE_KEY"],
    "Authorization": f"Bearer {env['SUPABASE_KEY']}",
}


def fetch_subs():
    params = {"select": "id,name,category,monthly_fee,last_used", "order": "monthly_fee.desc,id"}
    res = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def add_sub(row):
    res = requests.post(BASE_URL, headers=HEADERS, json=row, timeout=10)
    res.raise_for_status()


def update_sub(sub_id, row):
    res = requests.patch(BASE_URL, headers=HEADERS, params={"id": f"eq.{sub_id}"}, json=row, timeout=10)
    res.raise_for_status()


def delete_sub(sub_id):
    res = requests.delete(BASE_URL, headers=HEADERS, params={"id": f"eq.{sub_id}"}, timeout=10)
    res.raise_for_status()


def check_row(name, category):
    if not name.strip() or not category.strip():
        st.warning("구독 이름과 분류를 모두 입력해 주세요.")
        return False
    return True


def to_row(name, category, monthly_fee, last_used):
    return {
        "name": name.strip(),
        "category": category.strip(),
        "monthly_fee": int(monthly_fee),
        "last_used": last_used.isoformat(),
    }


def run(action, done_message):
    """Supabase 에 바꾸기를 보내고, 성공하면 안내를 남긴 뒤 화면을 새로 그린다."""
    try:
        action()
    except requests.RequestException as e:
        st.error(f"Supabase에 저장하지 못했습니다: {e}")
        return
    st.session_state["notice"] = done_message
    st.rerun()


def label(row):
    return f"{row['name']} ({row['category']} · {row['monthly_fee']:,}원)"


st.title("구독 정리함")

if "notice" in st.session_state:
    st.success(st.session_state.pop("notice"))

try:
    subs = fetch_subs()
except requests.RequestException as e:
    st.error(f"구독 목록을 불러오지 못했습니다: {e}")
    st.stop()

# 1) 보기
st.subheader("내 구독")
if not subs:
    st.caption("아직 등록된 구독이 없습니다.")
else:
    st.dataframe(
        [
            {
                "구독 이름": r["name"],
                "분류": r["category"],
                "한 달 요금(원)": r["monthly_fee"],
                "마지막으로 쓴 날": r["last_used"],
            }
            for r in subs
        ],
        hide_index=True,
        width="stretch",
        column_config={"한 달 요금(원)": st.column_config.NumberColumn(format="%d")},
    )
    total = sum(r["monthly_fee"] for r in subs)
    st.caption(f"모두 {len(subs)}건 · 한 달 요금 합계 {total:,}원 (계산한 값)")

tab_add, tab_edit, tab_delete = st.tabs(["더하기", "고치기", "지우기"])

# 2) 더하기
with tab_add:
    with st.form("add_form", clear_on_submit=True):
        name = st.text_input("구독 이름")
        category = st.text_input("분류")
        monthly_fee = st.number_input("한 달 요금(원)", min_value=0, step=100)
        last_used = st.date_input("마지막으로 쓴 날", value=date.today())
        if st.form_submit_button("더하기") and check_row(name, category):
            row = to_row(name, category, monthly_fee, last_used)
            run(lambda: add_sub(row), f"'{row['name']}'을(를) 더했습니다.")

# 3) 고치기
with tab_edit:
    if not subs:
        st.caption("고칠 구독이 없습니다.")
    else:
        target = st.selectbox("고칠 구독", subs, format_func=label, key="edit_target")
        sid = target["id"]
        with st.form("edit_form"):
            name = st.text_input("구독 이름", value=target["name"], key=f"edit_name_{sid}")
            category = st.text_input("분류", value=target["category"], key=f"edit_cat_{sid}")
            monthly_fee = st.number_input(
                "한 달 요금(원)", min_value=0, step=100, value=target["monthly_fee"], key=f"edit_fee_{sid}"
            )
            last_used = st.date_input(
                "마지막으로 쓴 날", value=date.fromisoformat(target["last_used"]), key=f"edit_date_{sid}"
            )
            if st.form_submit_button("고치기") and check_row(name, category):
                row = to_row(name, category, monthly_fee, last_used)
                run(lambda: update_sub(sid, row), f"'{row['name']}'을(를) 고쳤습니다.")

# 4) 지우기
with tab_delete:
    if not subs:
        st.caption("지울 구독이 없습니다.")
    else:
        target = st.selectbox("지울 구독", subs, format_func=label, key="delete_target")
        sure = st.checkbox("정말 지웁니다 (되돌릴 수 없음)", key=f"delete_sure_{target['id']}")
        if st.button("지우기", type="primary", disabled=not sure):
            run(lambda: delete_sub(target["id"]), f"'{target['name']}'을(를) 지웠습니다.")
