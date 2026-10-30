from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="예산·홍성 맛집 대시보드", layout="wide")

ENV_PATH = Path(__file__).parent / ".env"
# 카카오 로컬 API 공식 문서: https://developers.kakao.com/docs/latest/ko/local/dev-guide
KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
FOOD_CODE = "FD6"  # 카테고리 그룹 코드: 음식점
PAGE_SIZE = 15  # size 최대 15
MAX_PAGES = 3  # 한 검색어당 최대 45건(pageable_count 최대 45)까지만 보여 줌
REGIONS = ["예산", "홍성"]
RADIUS_OPTIONS = [300, 500, 1000, 2000, 3000, 5000, 10000, 20000]  # radius 0~20000m
NAVER_SEARCH_URL = "https://map.naver.com/p/search/"


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


def kakao_get(api_key, url, params):
    res = requests.get(url, headers={"Authorization": f"KakaoAK {api_key}"}, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def fetch_pages(api_key, url, params):
    """마지막 페이지(is_end)까지, 최대 MAX_PAGES 페이지를 불러온다."""
    docs = []
    for page in range(1, MAX_PAGES + 1):
        data = kakao_get(api_key, url, {**params, "page": page, "size": PAGE_SIZE})
        docs.extend(data["documents"])
        if data["meta"]["is_end"]:
            break
    return docs


def search_keyword(api_key, query, **extra):
    return fetch_pages(api_key, KEYWORD_URL, {"query": query, "category_group_code": FOOD_CODE, **extra})


def search_nearby(api_key, food, x, y, radius):
    """기준 좌표 반경 안 음식점. 찾을 음식이 비어 있으면 카테고리 검색으로 음식점 전체를 찾는다."""
    near = {"x": x, "y": y, "radius": radius, "sort": "distance"}
    if food:
        return search_keyword(api_key, food, **near)
    return fetch_pages(api_key, CATEGORY_URL, {"category_group_code": FOOD_CODE, **near})


def find_place(api_key, name):
    """기준 장소 후보(분류 제한 없음, 정확도순 최대 5건)."""
    return kakao_get(api_key, KEYWORD_URL, {"query": name, "size": 5})["documents"]


def get_api_key():
    api_key = load_env().get("KAKAO_REST_API_KEY")
    if not api_key:
        st.error(".env 에 KAKAO_REST_API_KEY 가 없습니다.")
    return api_key


def show_api_error(e):
    if isinstance(e, requests.HTTPError):
        st.error(f"카카오 API 오류 ({e.response.status_code}): {e.response.text}")
    else:
        st.error(f"연결 오류: {e}")


def format_radius(m):
    return f"{m / 1000:g}km" if m >= 1000 else f"{m}m"


def to_table(docs):
    rows = []
    for d in docs:
        # category_name 예: "음식점 > 한식 > 육류,고기"
        parts = [p.strip() for p in d["category_name"].split(">")]
        address = d["road_address_name"] or d["address_name"]
        rows.append({
            "이름": d["place_name"],
            "분류": parts[1] if len(parts) > 1 else parts[0],
            "세부 분류": parts[-1],
            "지역": region_of(d["address_name"]),
            "주소": address,
            "전화": d["phone"],
            "카카오맵": d["place_url"],
            "네이버지도": naver_link(d["place_name"], d["road_address_name"]),
            # distance 는 x·y 를 넘겼을 때만 온다
            "거리(m)": int(d["distance"]) if d.get("distance") else None,
            "lat": float(d["y"]),
            "lon": float(d["x"]),
            "id": d["id"],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates("id").drop(columns="id").reset_index(drop=True)
        if df["거리(m)"].isna().all():
            df = df.drop(columns="거리(m)")
    return df


def naver_link(name, road_address):
    """가게 이름 + 도로명 주소(띄어 이어 붙임)로 네이버 지도 검색 주소를 만든다. 도로명 주소가 없으면 이름만 쓴다."""
    return NAVER_SEARCH_URL + quote(f"{name} {road_address}".strip())


def region_of(address):
    """예산군·홍성군은 그대로, 그 밖은 주소의 두 번째 칸(시·군·구)을 쓴다."""
    for r in REGIONS:
        if f"{r}군" in address:
            return f"{r}군"
    parts = address.split()
    return parts[1] if len(parts) > 1 else "기타"


st.title("🍚 충남 예산·홍성 맛집 대시보드")
st.caption("카카오 로컬 API(키워드로 장소 검색, 음식점 FD6)로 찾은 결과입니다. [검색]을 누를 때만 API를 부릅니다.")

with st.form("search"):
    c1, c2 = st.columns([2, 3])
    regions = c1.multiselect("지역", REGIONS, default=REGIONS)
    keyword = c2.text_input("검색어", value="맛집", help="예: 맛집, 국밥, 짜장면, 한우")
    submitted = st.form_submit_button("검색", type="primary")

if submitted:
    if not regions or not keyword.strip():
        st.warning("지역과 검색어를 모두 넣어 주세요.")
    elif api_key := get_api_key():
        try:
            with st.spinner("카카오에서 불러오는 중..."):
                docs = []
                for r in regions:
                    docs.extend(search_keyword(api_key, f"충남 {r} {keyword.strip()}"))
            st.session_state["lunch_df"] = to_table(docs)
            st.session_state["lunch_query"] = f"{'·'.join(regions)} / {keyword.strip()}"
            st.session_state["lunch_base"] = None
        except requests.RequestException as e:
            show_api_error(e)

st.markdown("#### 📍 내 조건으로 찾기")
with st.form("near"):
    n1, n2, n3 = st.columns([2, 2, 2])
    base_name = n1.text_input("기준 장소 (역·동네 이름)", placeholder="예: 홍성역, 내포신도시, 예산읍")
    food = n2.text_input("찾을 음식", placeholder="예: 국밥, 짜장면", help="비워 두면 반경 안 음식점 전체를 찾습니다.")
    radius = n3.select_slider("반경", RADIUS_OPTIONS, value=1000, format_func=format_radius)
    near_submitted = st.form_submit_button("이 조건으로 검색", type="primary")

if near_submitted:
    if not base_name.strip():
        st.warning("기준 장소를 넣어 주세요.")
    elif api_key := get_api_key():
        try:
            with st.spinner("기준 장소를 찾는 중..."):
                places = find_place(api_key, base_name.strip())
            if not places:
                st.warning(f"카카오에서 '{base_name.strip()}' 장소를 찾지 못했습니다. 이름을 바꿔 다시 넣어 주세요.")
            else:
                base = places[0]
                with st.spinner("반경 안 음식점을 찾는 중..."):
                    docs = search_nearby(api_key, food.strip(), base["x"], base["y"], radius)
                st.session_state["lunch_df"] = to_table(docs)
                st.session_state["lunch_query"] = (
                    f"{base['place_name']} 반경 {format_radius(radius)} / {food.strip() or '음식점 전체'}"
                )
                st.session_state["lunch_base"] = {"input": base_name.strip(), "places": places}
        except requests.RequestException as e:
            show_api_error(e)

df = st.session_state.get("lunch_df")
if df is None:
    st.info("위의 [검색] 또는 [이 조건으로 검색]을 눌러 주세요.")
    st.stop()

st.subheader(f"검색 결과 — {st.session_state['lunch_query']}")

# 기준 장소: 카카오가 돌려준 값만 그대로 보여 준다
base_info = st.session_state.get("lunch_base")
if base_info:
    base = base_info["places"][0]
    st.success(
        f"기준 장소: **{base['place_name']}** — {base['road_address_name'] or base['address_name']}"
        f"  ([카카오맵]({base['place_url']}))"
    )
    others = base_info["places"][1:]
    if others:
        with st.expander(f"'{base_info['input']}'의 다른 후보 — 원하는 곳이 아니면 이름을 더 자세히 넣어 주세요"):
            for p in others:
                st.markdown(f"- {p['place_name']} — {p['road_address_name'] or p['address_name']}")

if df.empty:
    st.warning("검색 결과가 없습니다. 다른 검색어나 더 넓은 반경으로 해 보세요.")
    st.stop()

# 결과 안에서 걸러 보기·정렬 (API를 다시 부르지 않음)
f1, f2, f3, f4 = st.columns([2, 2, 1.5, 1])
name_filter = f1.text_input("이름·주소로 찾기")
cats = sorted(df["분류"].unique())
cat_filter = f2.multiselect("분류", cats, default=cats)
sort_options = ["이름", "분류", "세부 분류", "지역", "주소"]
if "거리(m)" in df.columns:
    sort_options.insert(0, "거리(m)")
sort_col = f3.selectbox("정렬 기준", sort_options)
ascending = f4.radio("순서", ["오름차순", "내림차순"]) == "오름차순"

view = df[df["분류"].isin(cat_filter)]
if name_filter.strip():
    q = name_filter.strip()
    view = view[view["이름"].str.contains(q, regex=False) | view["주소"].str.contains(q, regex=False)]
view = view.sort_values(sort_col, ascending=ascending).reset_index(drop=True)

m1, m2, m3 = st.columns(3)
m1.metric("보이는 가게 수", f"{len(view)}곳")
m2.metric("분류 수", f"{view['분류'].nunique()}개")
m3.metric("가장 많은 분류", view["분류"].mode()[0] if not view.empty else "-")

if view.empty:
    st.warning("조건에 맞는 가게가 없습니다.")
    st.stop()

ch1, ch2 = st.columns(2)
with ch1:
    st.markdown("**분류별 가게 수**")
    st.bar_chart(view["분류"].value_counts().rename("가게 수"), horizontal=True)
with ch2:
    st.markdown("**지역별 가게 수**")
    st.bar_chart(view["지역"].value_counts().rename("가게 수"))

st.markdown("**지도**")
st.map(view, latitude="lat", longitude="lon", size=40)

st.markdown("**가게 목록**")
st.dataframe(
    view.drop(columns=["lat", "lon"]),
    hide_index=True,
    width="stretch",
    column_config={
        "카카오맵": st.column_config.LinkColumn("카카오맵", display_text="열기"),
        # LinkColumn 은 누르면 새 탭으로 연다
        "네이버지도": st.column_config.LinkColumn("네이버지도", display_text="열기"),
    },
)
