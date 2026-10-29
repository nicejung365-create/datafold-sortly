import io
import json
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import base64

import pandas as pd
import requests
import streamlit as st
from PIL import Image

# 고정 기준(대상·분야·정보유형)으로 분류하고 키워드를 붙이는 부분 — remap_fixed.py 와 함께 씀
from classifier import (
    FIXED_CRITERIA, GEMINI_MODEL, TAG_AXES, call_gemini, classify_items, split_names,
)
# 게시물 URL 을 크롤링해 AI 로 제목·본문·요약을 정리하는 부분
from crawler import MAX_POSTS, CrawlError, crawl

st.set_page_config(page_title="AI 기반 데이터 자동분류·가공 플랫폼", page_icon="💬", layout="wide")

ENV_PATH = Path(__file__).parent / ".env"
PRD_PATH = Path(__file__).parent / "PRD.md"
TABLE = "classification_results"
# keywords·summary 칸이 없는 표에 저장·조회할 때 안내할 SQL 파일
NEW_COLUMNS_SQL = "data/new_columns.sql"

XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


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
GEMINI_API_KEY = env.get("GEMINI_API_KEY", "")


def error_text(e):
    if e.response is not None and e.response.text:
        text = f"{e} — {e.response.text}"
        if "keywords" in e.response.text or "summary" in e.response.text:
            text += f"\n\n→ Supabase 표에 keywords·summary 칸이 없습니다. {NEW_COLUMNS_SQL} 을 SQL Editor 에서 한 번 실행해 주세요."
        return text
    return str(e)


def save_rows(rows):
    res = requests.post(BASE_URL, headers=HEADERS, json=rows, timeout=10)
    res.raise_for_status()


ROW_FIELDS = "item_order,source_name,title,content,lifecycle,field,info_type,keywords,summary"


def fetch_rows(batch_id):
    params = {"select": ROW_FIELDS, "batch_id": f"eq.{batch_id}", "order": "item_order.asc"}
    res = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_all_rows():
    # 지금까지 쌓인 모든 결과 (최근 분류부터) — 화면 3 필터를 누를 때마다 다시 부르지 않게 1분간 기억해 둠
    rows, start, page = [], 0, 1000
    while True:
        params = {"select": ROW_FIELDS, "order": "created_at.desc,item_order.asc"}
        headers = {**HEADERS, "Range": f"{start}-{start + page - 1}"}
        res = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        rows += res.json()
        if len(res.json()) < page:
            return rows
        start += page


def rename_criterion(batch_id, axis, old_name, new_name):
    params = {"batch_id": f"eq.{batch_id}", axis: f"eq.{old_name}"}
    res = requests.patch(BASE_URL, headers=HEADERS, params=params, json={axis: new_name}, timeout=10)
    res.raise_for_status()


# ---------- 이번 분류에 쓰인 고정 기준·키워드 정리하기 ----------

def used_criteria(items, tag_list):
    # 이번 분류에 쓰인 기준 목록: [{"axis", "name", "status", "count", "titles"}, ...]
    # status 는 고정 기준이면 "기존", 고정 기준에 없는 이름(AI 가 목록 밖 이름을 답한 경우)이면 "신규"
    # titles 는 그 기준이 붙은 소재 제목 (올린 순서대로, 제목이 없으면 화면 3 카드처럼 내용 앞부분)
    titles = {}
    for (title, content), tags in zip(items, tag_list):
        for axis in TAG_AXES:
            # 대상은 여러 개를 고를 수 있어, 고른 대상마다 따로 셈
            for name in split_names(tags[axis]) if axis == "lifecycle" else [tags[axis]]:
                titles.setdefault((axis, name), []).append(title or content[:50])
    rows = [
        {
            "axis": axis, "name": name, "status": "기존" if name in FIXED_CRITERIA[axis] else "신규",
            "count": len(names), "titles": names,
        }
        for (axis, name), names in titles.items()
    ]
    # 고정 기준 목록 순서대로 보여 줌
    order = list(TAG_AXES)
    rank = lambda r: FIXED_CRITERIA[r["axis"]].index(r["name"]) if r["name"] in FIXED_CRITERIA[r["axis"]] else 99
    return sorted(rows, key=lambda r: (order.index(r["axis"]), rank(r), r["name"]))


def used_keywords(items, tag_list):
    # 이번 분류에 붙은 키워드 목록: [{"name", "count", "titles"}, ...] (많이 붙은 키워드부터)
    titles = {}
    for (title, content), tags in zip(items, tag_list):
        for word in split_names(tags["keywords"]):
            titles.setdefault(word, []).append(title or content[:50])
    rows = [{"name": word, "count": len(names), "titles": names} for word, names in titles.items()]
    return sorted(rows, key=lambda r: (-r["count"], r["name"]))


# ---------- 올린 파일을 소재 목록(제목, 내용)으로 바꾸기 ----------

def decode_text(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949")


def read_csv(data):
    return pd.read_csv(io.StringIO(decode_text(data)), dtype=str).fillna("")


def read_xlsx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))
        sheet = "xl/worksheets/sheet1.xml"
        if sheet not in z.namelist():
            sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
        root = ET.fromstring(z.read(sheet))

    rows = []
    for row in root.iter(f"{XLSX_NS}row"):
        values = {}
        for c in row.iter(f"{XLSX_NS}c"):
            letters = re.match(r"[A-Z]+", c.get("r", "A")).group()
            col = 0
            for ch in letters:
                col = col * 26 + ord(ch) - 64
            if c.get("t") == "s":
                text = shared[int(c.findtext(f"{XLSX_NS}v"))]
            elif c.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{XLSX_NS}t"))
            else:
                text = c.findtext(f"{XLSX_NS}v") or ""
            values[col - 1] = text
        if values:
            rows.append([values.get(i, "") for i in range(max(values) + 1)])

    if not rows:
        return pd.DataFrame()
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = [h or f"열{i + 1}" for i, h in enumerate(rows[0])]
    return pd.DataFrame(rows[1:], columns=header)


def read_docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paragraphs = ["".join(t.text or "" for t in p.iter(f"{DOCX_NS}t")) for p in root.iter(f"{DOCX_NS}p")]
    return "\n".join(paragraphs)


def table_items(df):
    # 표 파일은 한 줄이 소재 1건 — 제목은 "제목" 열(없으면 첫 열), 내용은 모든 열을 "열: 값"으로 이음
    title_col = "제목" if "제목" in df.columns else df.columns[0]
    items = []
    for _, row in df.iterrows():
        content = " / ".join(f"{col}: {row[col]}" for col in df.columns if str(row[col]).strip())
        if content:
            items.append((str(row[title_col]).strip(), content))
    return items


def document_items(text):
    # 글 파일은 문서 하나가 소재 1건 — 제목은 내용이 있는 첫 줄
    text = text.strip()
    if not text:
        return []
    first_line = next(line.strip() for line in text.splitlines() if line.strip())
    return [(first_line[:100], text)]


def file_items(uploaded_file):
    data = uploaded_file.getvalue()
    ext = Path(uploaded_file.name).suffix.lower()
    if ext == ".csv":
        return table_items(read_csv(data))
    if ext == ".xlsx":
        df = read_xlsx(data)
        return table_items(df) if not df.empty else []
    if ext == ".docx":
        return document_items(read_docx(data))
    return document_items(decode_text(data))


def make_rows(batch_id, source_type, sources, items, tag_list, summaries):
    # sources: 소재마다의 출처 (파일은 파일 이름, URL 크롤링은 게시물마다의 주소)
    # summaries: 소재마다의 AI 요약 (파일은 빈 글자)
    return [
        {
            "batch_id": batch_id,
            "source_type": source_type,
            "source_name": source,
            "item_order": i,
            "title": title or None,
            "content": content,
            "summary": summary or None,
            **tags,
        }
        for i, ((title, content), tags, source, summary) in enumerate(
            zip(items, tag_list, sources, summaries), start=1
        )
    ]


# 0 = 첫 화면(랜딩 페이지), 1~3 = 기능 화면 — streamlit run app.py 로 열면 첫 화면부터 뜸
if "page" not in st.session_state:
    st.session_state.page = 0


def go(page):
    st.session_state.page = page
    st.rerun()


STEPS = ["① 데이터 올리기", "② 기준 확인", "③ 분류 결과"]


# 화면 위계 (세 화면 모두 같음)
# 0 서비스 이름(작은 회색) → 1 화면 제목(st.title) → 2 브레드크럼(작은 글씨) → 3 화면 설명(본문)
# → 4 구역 제목(st.subheader) → 5 구역 안 소제목(####) → 6 표 머리줄·도움말(st.caption)
# 버튼은 다음 단계로 가는 버튼 하나만 type="primary" 로 강조
SERVICE_NAME = "AI 기반 데이터 자동분류·가공 플랫폼"


def page_header(title, description):
    st.caption(SERVICE_NAME)
    st.title(title)
    # 타이틀 바로 밑에 지금 어느 화면인지 보여 줌 (지금 화면만 굵게)
    st.caption(" › ".join(
        f"**{name}**" if i == st.session_state.page else name for i, name in enumerate(STEPS, start=1)
    ))
    st.write(description)
    st.divider()


def screen_1():
    st.subheader("분류할 자료 넣기")
    # 파일 선택(왼쪽) · "또는"(가운데) · URL 입력(오른쪽)을 한 줄에 나란히 둠
    col_file, col_or, col_url = st.columns([5, 1, 5], vertical_alignment="center")
    with col_file:
        uploaded_file = st.file_uploader(
            "파일 선택 (표 또는 글 파일 1개)",
            type=["xlsx", "csv", "docx", "txt"],
        )
        st.caption(".xlsx · .csv · .docx · .txt")

    col_or.markdown("<p style='text-align:center'>또는</p>", unsafe_allow_html=True)

    with col_url:
        url = st.text_input("게시물 URL 입력", placeholder="URL을 입력하세요 (예: https://...)")
        st.caption(
            f"게시판 목록 주소를 넣으면 게시물을 최대 {MAX_POSTS}건까지 읽어 와, AI가 제목·본문·요약을 정리한 뒤 분류합니다. "
            "글 하나의 주소를 넣으면 그 글만 읽습니다."
        )

    # [분류하기]는 세 열 아래 한가운데
    if st.columns([2, 1, 2])[1].button("분류하기", type="primary", width="stretch"):
        if uploaded_file is None and not url.strip():
            st.warning("업로드된 파일이 없습니다. 파일을 업로드 후 이용해주세요.")
            return

        if not GEMINI_API_KEY:
            st.error(".env 에 GEMINI_API_KEY 를 먼저 채워 주세요.")
            return

        if uploaded_file is not None:
            try:
                items = file_items(uploaded_file)
            except (ValueError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError, UnicodeDecodeError) as e:
                st.error(f"파일을 읽지 못했습니다: {e}")
                return
            source_type, source_name = "file", uploaded_file.name
            sources, summaries = [source_name] * len(items), [""] * len(items)
        else:
            if not re.match(r"https?://", url.strip()):
                st.warning("URL은 http:// 또는 https:// 로 시작해야 합니다.")
                return
            source_type, source_name = "url", url.strip()
            # 사이트를 크롤링하고 AI 가 게시물마다 제목·본문·요약을 정리함 (진행 상황을 한 줄씩 보여 줌)
            with st.status("사이트에서 게시물을 읽어 오는 중입니다...") as status:
                try:
                    posts = crawl(source_name, GEMINI_API_KEY, progress=lambda text: status.update(label=text))
                except CrawlError as e:
                    status.update(label="읽어 오지 못했습니다", state="error")
                    st.warning(str(e))
                    return
                except requests.RequestException as e:
                    status.update(label="읽어 오지 못했습니다", state="error")
                    st.error(f"페이지를 읽어 오지 못했습니다: {error_text(e)}")
                    return
                except (ValueError, KeyError, TypeError) as e:
                    status.update(label="정리하지 못했습니다", state="error")
                    st.error(f"AI 정리 답을 읽지 못했습니다: {e}")
                    return
                status.update(label=f"게시물 {len(posts)}건을 읽어 정리했습니다", state="complete")
            items = [(p["title"], p["body"]) for p in posts]
            sources = [p["url"] for p in posts]
            summaries = [p["summary"] for p in posts]

        if not items:
            if source_type == "url":
                st.warning("페이지에서 읽어 온 내용이 없습니다. 다른 URL을 넣어 주세요.")
            else:
                st.warning("파일에서 읽어 온 내용이 없습니다. 다른 파일을 올려 주세요.")
            return

        # AI 는 고정 기준(대상·분야·정보유형) 안에서만 고르고, 부족한 내용은 키워드로 붙임
        with st.spinner(f"AI가 소재 {len(items)}건을 분류하는 중입니다..."):
            try:
                tag_list = classify_items(GEMINI_API_KEY, items)
            except requests.RequestException as e:
                st.error(f"AI 분류를 받지 못했습니다: {error_text(e)}")
                return
            except (ValueError, KeyError, TypeError) as e:
                st.error(f"AI 답을 읽지 못했습니다: {e}")
                return

        batch_id = str(uuid.uuid4())
        try:
            save_rows(make_rows(batch_id, source_type, sources, items, tag_list, summaries))
            fetch_all_rows.clear()  # 새 결과가 '지금까지 쌓인 전체'에 바로 보이게
        except requests.RequestException as e:
            st.error(f"Supabase에 저장하지 못했습니다: {error_text(e)}")
            return

        st.session_state.batch_id = batch_id
        st.session_state.source_name = source_name  # 화면 3 맨 위에 보여 줄, 넣은 파일 이름·URL
        st.session_state.criteria = used_criteria(items, tag_list)
        st.session_state.keywords = used_keywords(items, tag_list)
        go(2)


def screen_2_description():
    if any(c["status"] == "신규" for c in st.session_state.get("criteria", [])):
        return "고정 기준에 없는 이름이 나왔습니다. 고정 기준 이름으로 고쳐 저장해 주세요."
    return "이번 분류는 모두 고정 기준으로 나뉘었습니다. 고정 기준에 담기지 않은 내용은 키워드로 붙였습니다."


def titles_text(titles):
    # 그 기준이 붙은 소재 제목을 앞에서부터 3개까지 보여 주고, 나머지는 "외 N건"으로 줄임
    shown = " · ".join(titles[:3])
    if len(titles) > 3:
        shown += f" 외 {len(titles) - 3}건"
    return shown


def screen_2():
    criteria = st.session_state.get("criteria", [])
    existing_rows = [c for c in criteria if c["status"] == "기존"]
    new_rows = [c for c in criteria if c["status"] == "신규"]

    keywords = st.session_state.get("keywords", [])

    st.subheader("이번 분류에 쓰인 기준 목록")
    st.caption("고정 기준(대상·분야·정보유형)과, 고정 기준에 담기지 않아 붙인 키워드가 구분되어 있습니다.")

    # 고정 기준은 왼쪽 열, 키워드는 오른쪽 열에 표로 나란히 둠
    col_old, col_new = st.columns(2, gap="large")

    with col_old:
        st.markdown("#### 고정 기준")
        if not existing_rows:
            st.caption("이번 분류에 쓰인 고정 기준이 없습니다.")
        else:
            # 고정 기준은 읽기 전용 표
            st.dataframe(pd.DataFrame([
                {"구분": "고정", "분류 축": TAG_AXES[c["axis"]], "기준 이름": c["name"], "쓰인 소재 수": f"{c['count']}건"}
                for c in existing_rows
            ]), hide_index=True)

    edited = []
    with col_new:
        st.markdown("#### 키워드")
        if not keywords:
            st.caption("이번 분류에 붙은 키워드가 없습니다.")
        else:
            # 키워드는 읽기 전용 표
            st.dataframe(pd.DataFrame([
                {"키워드": k["name"], "쓰인 소재 수": f"{k['count']}건", "붙은 소재": titles_text(k["titles"])}
                for k in keywords
            ]), hide_index=True)

        # AI 가 고정 기준 목록 밖의 이름을 답했을 때만 나옴 — 고정 기준 이름으로 고쳐 저장할 수 있음
        if new_rows:
            st.markdown("#### 고정 기준에 없는 이름")
            # 신규 기준 표는 "기준 이름" 칸만 고칠 수 있고, 나머지 칸은 잠가 둠
            table = st.data_editor(
                pd.DataFrame([
                    {
                        "구분": "신규", "분류 축": TAG_AXES[c["axis"]], "기준 이름": c["name"],
                        "쓰인 소재 수": f"{c['count']}건", "붙은 소재": titles_text(c["titles"]),
                    }
                    for c in new_rows
                ]),
                hide_index=True,
                disabled=["구분", "분류 축", "쓰인 소재 수", "붙은 소재"],
                column_config={"기준 이름": st.column_config.TextColumn("기준 이름 ✏️")},
                key=f"new_criteria_{st.session_state.get('batch_id', '')}",
            )
            edited = [str(name or "").strip() for name in table["기준 이름"]]
            st.caption("기준 이름 ✏️ 칸을 두 번 눌러 이름을 고칩니다.")

    # 맨 아래 한 줄: 안내 · [저장] · [결과화면 보기]
    col_note, col_save, col_next = st.columns([4, 1, 1], vertical_alignment="center")
    col_note.caption("고정 기준과 키워드는 읽기 전용 · 고정 기준에 없는 이름만 고칠 수 있음")
    if col_save.button("저장", width="stretch"):
        if any(not name for name in edited):
            st.warning("기준 이름을 비워 둘 수 없습니다.")
        else:
            try:
                for c, name in zip(new_rows, edited):
                    if name != c["name"]:
                        rename_criterion(st.session_state.batch_id, c["axis"], c["name"], name)
                        fetch_all_rows.clear()
                        c["name"] = name
            except requests.RequestException as e:
                st.error(f"Supabase에 저장하지 못했습니다: {error_text(e)}")
            else:
                st.success("고친 기준 이름을 저장했습니다." if new_rows else "고칠 기준 이름이 없습니다.")
    if col_next.button("결과화면 보기", type="primary", width="stretch"):
        go(3)


SCOPE_THIS, SCOPE_ALL = "이번 분류 결과", "지금까지 쌓인 전체"
FILTER_KEYS = {axis: f"filter_{axis}" for axis in TAG_AXES}


def row_names(row, axis):
    # 소재 하나에 붙은 기준 이름들 (대상은 여러 개일 수 있음)
    return split_names(row.get(axis)) if axis == "lifecycle" else [row.get(axis) or ""]


def filter_options(rows, axis):
    # 이 범위의 결과에 실제로 나온 기준 이름과 건수 — 고정 기준 순서대로, 고정 기준에 없는 이름(예전 결과)은 뒤에
    counts = {}
    for row in rows:
        for name in row_names(row, axis):
            if name:
                counts[name] = counts.get(name, 0) + 1
    fixed = FIXED_CRITERIA[axis]
    return dict(sorted(counts.items(), key=lambda kv: (fixed.index(kv[0]) if kv[0] in fixed else 99, kv[0])))


def clear_filters():
    for key in FILTER_KEYS.values():
        st.session_state[key] = []


def to_csv(rows):
    df = pd.DataFrame(
        rows,
        columns=["item_order", "title", "summary", "content", "lifecycle", "field", "info_type", "keywords", "source_name"],
    )
    df.columns = ["순서", "제목", "요약", "내용", "대상", "분야", "정보유형", "키워드", "출처"]
    return df.to_csv(index=False).encode("utf-8-sig")


def screen_3():
    # 맨 위 줄(카드 수·다운로드)은 필터 결과를 알아야 채울 수 있어, 자리만 먼저 잡아 두고 아래에서 채움
    top = st.container()

    # ---- 카드 바로 위 한 줄: 보는 범위 · 대상 · 분야 · 정보유형 필터 · 초기화 ----
    filter_cols = st.columns([1.3, 2, 2, 2, 0.8], vertical_alignment="bottom")
    scope = filter_cols[0].selectbox("보는 범위", [SCOPE_THIS, SCOPE_ALL], key="filter_scope")

    try:
        if scope == SCOPE_THIS:
            rows = fetch_rows(st.session_state.get("batch_id", ""))
        else:
            rows = fetch_all_rows()
    except requests.RequestException as e:
        st.error(f"Supabase에서 결과를 불러오지 못했습니다: {error_text(e)}")
        rows = []

    picked = {}
    for col, (axis, label) in zip(filter_cols[1:4], TAG_AXES.items()):
        options = filter_options(rows, axis)
        key = FILTER_KEYS[axis]
        # 범위를 바꿔 목록에서 사라진 이름은 고른 값에서 뺌 (없는 이름이 남아 있으면 칸이 오류를 냄)
        st.session_state[key] = [v for v in st.session_state.get(key, []) if v in options]
        picked[axis] = col.multiselect(
            label, list(options), key=key, placeholder="전체",
            format_func=lambda name, options=options: f"{name} ({options[name]})",
        )
    filter_cols[4].button("초기화", on_click=clear_filters, width="stretch")

    # 한 칸 안에서 여러 개를 고르면 "이 중 하나라도", 여러 칸을 함께 고르면 "모두 만족"
    shown = [
        row for row in rows
        if all(not names or set(row_names(row, axis)) & set(names) for axis, names in picked.items())
    ]
    filtered = any(picked.values())

    # ---- 맨 위 줄: 카드 수·출처 · [전체 다운로드] · [거른 것만 다운로드] · [처음 화면으로] ----
    col_count, col_all, col_some, col_home = top.columns([3.4, 1, 1, 1], vertical_alignment="center")
    if rows:
        if scope == SCOPE_THIS:
            # URL 크롤링은 소재마다 출처(게시물 주소)가 달라, 넣은 파일 이름·URL 을 보여 줌
            where = st.session_state.get("source_name") or rows[0]["source_name"]
        else:
            where = SCOPE_ALL
        count = f"{len(rows)}장 중 {len(shown)}장" if filtered else f"{len(rows)}장"
        col_count.subheader(f"소재 카드 {count} ({where})")
    else:
        col_count.subheader("Supabase에 저장된 결과가 없습니다.")
    col_all.download_button(
        "전체 다운로드", data=to_csv(rows), file_name="분류결과.csv", mime="text/csv",
        type="primary", width="stretch",
    )
    col_some.download_button(
        "거른 것만 다운로드", data=to_csv(shown), file_name="분류결과_필터.csv", mime="text/csv",
        width="stretch", disabled=not filtered, help=None if filtered else "필터를 하나 이상 고르면 누를 수 있습니다.",
    )
    if col_home.button("처음 화면으로", width="stretch"):
        go(1)
    top.caption("Supabase classification_results 표에 저장된 내용입니다. 태그는 AI가 분류했고, 고치는 기능은 없습니다.")

    if rows and not shown:
        st.info("고른 조건에 맞는 소재가 없습니다. 필터를 바꾸거나 [초기화]를 눌러 주세요.")

    # 카드는 3열 격자로, 높이를 고정한 상자 안에서만 스크롤되게 함
    with st.container(height=520, border=False):
        grid = st.columns(3)
        for i, row in enumerate(shown):
            with grid[i % 3].container(border=True):
                st.markdown(f"**{row['title'] or row['content'][:50]}**")
                # URL 크롤링 소재는 AI 요약을, 파일 소재(요약 없음)는 내용 앞부분을 보여 줌
                st.caption(row.get("summary") or row["content"][:200])
                st.markdown(
                    f"`대상 : {row['lifecycle']}` `분야 : {row['field']}` `정보유형 : {row['info_type']}`"
                )
                # 고정 기준에 담기지 않은 내용은 키워드로 (예전 결과처럼 키워드가 없으면 안 보임)
                words = split_names(row.get("keywords"))
                if words:
                    st.caption("키워드 " + " ".join(f"#{w}" for w in words))


# ---------- 첫 화면(랜딩 페이지) — 서비스 소개 (근거: PRD.md 목표·넣을 기능 1~3) ----------

LANDING_BG, LANDING_ACCENT, LANDING_TEXT = "#242627", "#3fb645", "#ebeceb"
# 강조색이 배경색과 같으면 강조색 자리(제목·카드 테두리·버튼 바탕)가 배경에 묻히므로 글자색으로 대신 보이게 하고,
# 버튼 글자는 강조색으로 씀
_SAME = LANDING_ACCENT.lower() == LANDING_BG.lower()
LANDING_MARK = LANDING_TEXT if _SAME else LANDING_ACCENT  # 제목·카드 제목·카드 테두리·버튼 바탕
LANDING_BUTTON_TEXT = LANDING_ACCENT if _SAME else LANDING_BG
# 카드 바탕은 글자색을 아주 옅게 깔아, 배경 위에서 카드 구역이 구분되게 함
LANDING_CARD_BG = "rgba({}, {}, {}, 0.10)".format(*(int(LANDING_TEXT[i:i + 2], 16) for i in (1, 3, 5)))
LANDING_FEATURES = [
    ("📂", "자동으로 나누기",
     "표·글 파일이나 게시물 URL을 넣으면, 정해 둔 고정 기준으로 나누고 기준에 담기지 않는 내용은 키워드로 붙입니다."),
    ("🏷️", "세 기준으로 한눈에 보기",
     "나뉜 결과를 소재 카드마다 대상·분야·정보유형 태그와 키워드로 함께 보여 줍니다."),
    ("⬇️", "바로 내려받기",
     "나뉜 결과를 콘텐츠를 만들 때 바로 쓸 수 있는 정리된 파일로 내려받습니다."),
]
# 히어로 영역 배경 영상 — make_hero.py 로 만듦. 파일이 없으면 같은 자리에 초록 빛 번짐 그림을 대신 깜
HERO_VIDEO_PATH = Path(__file__).parent / "hero.mp4"


def hero_background():
    if HERO_VIDEO_PATH.exists():
        # st.video 는 영상을 파일로 따로 보내 주므로 페이지가 무거워지지 않음
        # muted 여야 브라우저가 자동 재생을 허락함, loop 로 끝없이 반복
        st.video(str(HERO_VIDEO_PATH), autoplay=True, muted=True, loop=True)
    else:
        st.markdown("<div class='landing-hero-media landing-hero-fallback'></div>", unsafe_allow_html=True)
    # 영상 위에 배경색을 반쯤 덮어, 위에 올린 제목·버튼 글자가 잘 읽히게 함
    st.markdown("<div class='landing-hero-shade'></div>", unsafe_allow_html=True)


def landing():
    # 색은 이 첫 화면에만 씀 — 기능 화면(1~3)에는 이 스타일이 들어가지 않음
    st.markdown(f"""
<style>
.stApp {{ background: {LANDING_BG}; }}
[data-testid="stHeader"] {{ background: transparent; color: {LANDING_TEXT}; }}
[data-testid="stHeader"] button, [data-testid="stHeader"] svg {{ color: {LANDING_TEXT}; fill: {LANDING_TEXT}; }}
.st-key-landing p, .st-key-landing li {{ color: {LANDING_TEXT}; }}
/* 히어로 영역: 배경 영상을 깔고 그 위에 제목·소개·버튼을 가운데로 올림 */
.st-key-landing_hero {{
    position: relative; overflow: hidden; border-radius: 20px; min-height: 440px;
    justify-content: center; padding: 48px 24px; margin-bottom: 32px;
}}
/* 배경 상자(와 그것을 감싼 틀)는 히어로 영역 전체를 채우고 맨 뒤에, 나머지(제목·소개·버튼)는 그 위에 */
.st-key-landing_hero > div:has(.st-key-landing_hero_bg) {{ position: absolute; inset: 0; z-index: 0; }}
.st-key-landing_hero_bg {{ position: absolute; inset: 0; }}
.st-key-landing_hero_bg [data-testid="stElementContainer"] {{ position: static; }}
.st-key-landing_hero > div:not(:has(.st-key-landing_hero_bg)) {{ position: relative; z-index: 1; }}
.landing-hero-media, .st-key-landing_hero_bg video {{
    position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
}}
/* 배경 영상은 보기만 하는 것 — 재생 단추 등 조작 막대를 숨기고 눌리지 않게 함 */
.st-key-landing_hero_bg video {{ pointer-events: none; }}
.st-key-landing_hero_bg video::-webkit-media-controls {{ display: none !important; }}
.landing-hero-fallback {{
    background:
        radial-gradient(circle at 30% 40%, rgba(63, 182, 69, 0.28), transparent 45%),
        radial-gradient(circle at 72% 60%, rgba(63, 182, 69, 0.22), transparent 50%),
        {LANDING_BG};
}}
.landing-hero-shade {{ position: absolute; inset: 0; background: rgba(36, 38, 39, 0.45); }}
.landing-title {{
    color: {LANDING_MARK}; font-size: 2.8rem; font-weight: 800; line-height: 1.25;
    text-align: center; margin: 0 0 12px;
}}
.landing-intro {{ color: {LANDING_TEXT}; font-size: 1.3rem; text-align: center; margin-bottom: 8px; }}
[class*="st-key-landing_card"] {{
    background: {LANDING_CARD_BG}; border: 1px solid {LANDING_MARK}; border-radius: 14px;
    padding: 24px; height: 100%;
}}
.landing-card-icon {{ font-size: 2rem; }}
.landing-card-title {{ color: {LANDING_MARK}; font-size: 1.25rem; font-weight: 800; margin: 8px 0; }}
/* 버튼: 꽉 찬 바탕 + 대비되는 글자, 마우스를 올리면 테두리만 남는 모양으로 바뀜 */
.st-key-landing_start button {{
    background: {LANDING_MARK}; border: 2px solid {LANDING_MARK};
    font-size: 1.1rem; padding: 12px 0; margin-top: 32px;
}}
.st-key-landing_start button:hover {{ background: transparent; border-color: {LANDING_TEXT}; }}
.st-key-landing .st-key-landing_start button p {{ color: {LANDING_BUTTON_TEXT}; font-size: 1.1rem; font-weight: 700; }}
.st-key-landing .st-key-landing_start button:hover p {{ color: {LANDING_TEXT}; }}
</style>
""", unsafe_allow_html=True)

    with st.container(key="landing"):
        # 히어로 영역: 배경 영상 위에 큰 제목 · 한 줄 소개 · [시작하기]
        with st.container(key="landing_hero"):
            with st.container(key="landing_hero_bg"):
                hero_background()
            st.markdown(f"<div class='landing-title'>{SERVICE_NAME}</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='landing-intro'>표·글 파일이나 게시물 URL을 넣으면 대상·분야·정보유형 고정 기준으로 자동 분류해, "
                "콘텐츠 제작에 바로 쓸 수 있는 데이터로 만들어 줍니다.</div>",
                unsafe_allow_html=True,
            )
            if st.columns([2, 1, 2])[1].button("시작하기", key="landing_start", width="stretch"):
                go(1)

        # 하는 일 세 가지를 카드 세 장으로 나란히 둠 (히어로 영역 아래)
        for i, (col, (icon, title, text)) in enumerate(zip(st.columns(3, gap="large"), LANDING_FEATURES)):
            with col.container(key=f"landing_card_{i}"):
                st.markdown(
                    f"<div class='landing-card-icon'>{icon}</div><div class='landing-card-title'>{title}</div>",
                    unsafe_allow_html=True,
                )
                st.write(text)


# ---------- 오른쪽 아래 상담 챗봇 (PRD.md 를 읽고 서비스에 대해 답함) ----------

CHAT_GREETING = "안녕하세요! 저는 데이터 올리기 도우미예요. 이 서비스에 대해 궁금한 걸 물어보세요."
# 챗봇 고정 지시문(역할·PRD 근거 규칙·답 형식)은 이 글 파일에 있음 — 고치면 다음 질문부터 바로 반영됨
CHAT_INSTRUCTION_PATH = Path(__file__).parent / "chatbot_instruction.txt"
# 답 형식 고정 지시문은 이 글 파일에 있음 — 시스템 지시문 맨 끝과 매 질문 바로 앞에 함께 넣고, 고치면 다음 질문부터 바로 반영됨
CHAT_FORMAT_LOCK_PATH = Path(__file__).parent / "chatbot_format_lock.txt"
# 챗봇 창의 말투 고르는 칸 — 보이는 이름: AI에게 넘기는 말투 지시 (첫 번째가 기본값)
CHAT_TONES = {
    "부드러운 존댓말": "모든 문장을 '~요'로 끝나는 부드러운 존댓말로 쓴다. (예: 화면 3에서 확인할 수 있어요.)",
    "격식 있는 존댓말": "모든 문장을 '~습니다'로 끝나는 격식 있는 존댓말로 쓴다. (예: 화면 3에서 확인할 수 있습니다.)",
    "친근한 반말": "모든 문장을 '~야', '~해'처럼 친근한 반말로 쓴다. (예: 화면 3에서 확인할 수 있어.)",
}
# 챗봇에 넣은 질문·고른 말투·받은 답을 한 줄씩 남기는 Supabase 표
CHAT_LOG_URL = f"{env['SUPABASE_URL'].rstrip('/')}/rest/v1/chat_logs"
CHAT_LOG_LIMIT = 20  # 지난 기록 목록에 보여 주는 최근 줄 수
CHAT_MEMORY_TURNS = 10  # 새로고침 뒤 이어서 기억할 최근 질문·답 쌍 수
BOT_PATH = Path(__file__).parent / "bot.png"
BOT_CROP = (420, 130, 1060, 770)  # bot.png 가운데 로봇 얼굴 부분 (정사각형)


@st.cache_data
def bot_icon_png():
    # 원본 bot.png(1536×1024)는 그대로 두고, 로봇 얼굴만 잘라 작게 줄인 PNG 를 만듦
    img = Image.open(BOT_PATH).convert("RGB").crop(BOT_CROP).resize((160, 160), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def copy_button(text, dark):
    # Streamlit 에는 복사 기능이 없어, 작은 HTML 조각 안에서 클립보드에 씀
    # AI 답을 JS 문자열로 넣을 때 "<" 를 모두 유니코드 표기(역슬래시 u003c)로 바꿔, 답 안의 글자가 HTML·스크립트로 실행되지 않게 함
    payload = json.dumps(text).replace("<", "\\u003c")
    fg, bg, line = ("#FAFAFA", "#33353f", "#4a4c58") if dark else ("#31333F", "#F5F6F8", "#D8DAE0")
    st.iframe(f"""
<style>
body {{ margin: 0; background: transparent; font-family: sans-serif; }}
button {{ font-size: 12px; padding: 3px 10px; border-radius: 8px; cursor: pointer;
         color: {fg}; background: {bg}; border: 1px solid {line}; }}
</style>
<div style="display:flex; justify-content:flex-end;">
  <button id="copy">📋 복사</button>
</div>
<script>
const text = {payload};
const btn = document.getElementById("copy");
btn.onclick = async () => {{
  try {{
    await navigator.clipboard.writeText(text);
  }} catch (e) {{
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }}
  btn.textContent = "✅ 복사됨";
  setTimeout(() => (btn.textContent = "📋 복사"), 1500);
}};
</script>
""", height=32)


def save_chat_log(question, tone, answer):
    row = {"question": question, "tone": tone, "answer": answer}
    res = requests.post(CHAT_LOG_URL, headers=HEADERS, json=row, timeout=10)
    res.raise_for_status()


def fetch_chat_logs(limit=CHAT_LOG_LIMIT):
    params = {"select": "created_at,question,tone,answer", "order": "created_at.desc", "limit": limit}
    res = requests.get(CHAT_LOG_URL, headers=HEADERS, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def restore_chat_history():
    # 새로고침·앱 재시작으로 대화가 비었을 때, chat_logs 의 최근 대화를 오래된 것부터 다시 채워 이어서 기억하게 함
    try:
        logs = fetch_chat_logs(CHAT_MEMORY_TURNS)
    except requests.RequestException:
        return []  # 불러오지 못하면 빈 대화로 시작 (챗봇 사용은 그대로 가능)
    history = []
    for log in reversed(logs):
        history += [{"role": "user", "text": log["question"]}, {"role": "assistant", "text": log["answer"]}]
    return history


def chat_log_list():
    try:
        logs = fetch_chat_logs()
    except requests.RequestException as e:
        st.error(f"지난 기록을 불러오지 못했어요: {error_text(e)}")
        return
    if not logs:
        st.caption("아직 남은 기록이 없어요.")
        return
    with st.container(height=240):
        for log in logs:
            # Supabase 는 UTC 로 돌려주므로 한국 시간으로 바꿔 보여 줌
            when = pd.to_datetime(log["created_at"]).tz_convert("Asia/Seoul").strftime("%Y-%m-%d %H:%M")
            st.caption(f"{when} · {log['tone']}")
            st.markdown(f"**넣은 말** : {log['question']}")
            st.markdown("**받은 답** :  \n" + log["answer"].replace("\n", "  \n"))
            st.divider()


def ask_chatbot(api_key, history, tone):
    # 공식 문서의 stateless 여러 차례 대화 방식: store=false + 지금까지의 대화를 input 배열로 보냄
    # 고른 말투를 형식 고정 지시문 바로 뒤에 붙여, 시스템 지시문 끝과 마지막 질문 앞 두 곳에 함께 들어가게 함
    format_lock = (
        CHAT_FORMAT_LOCK_PATH.read_text(encoding="utf-8").strip() + "\n\n"
        f"[말투 — 사용자가 화면에서 고름: {tone}]\n- " + CHAT_TONES[tone] + "\n"
        "- 앞선 답들이 다른 말투였더라도 이번 답은 이 말투로 쓴다."
    )
    system = (
        CHAT_INSTRUCTION_PATH.read_text(encoding="utf-8").strip() + "\n\n"
        "[PRD.md]\n" + PRD_PATH.read_text(encoding="utf-8") + "\n\n"
        + format_lock
    )
    # content 는 [{"type": "text", "text": ...}] 배열로 보내야 함 (model_output 에 글자만 보내면 400 오류)
    steps = [
        {
            "type": "user_input" if m["role"] == "user" else "model_output",
            "content": [{"type": "text", "text": m["text"]}],
        }
        for m in history
    ]
    # 마지막 질문 바로 앞에도 고정 지시문을 붙여 보냄 (화면의 대화 기록에는 붙이지 않음)
    last = steps[-1]["content"][0]
    last["text"] = format_lock + "\n\n[사용자 질문]\n" + last["text"]
    return call_gemini(api_key, {
        "model": GEMINI_MODEL,
        "store": False,
        "system_instruction": system,
        "input": steps,
    })


def chatbot():
    st.session_state.setdefault("chat_open", False)
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = restore_chat_history()

    dark = st.context.theme.type == "dark"
    panel_bg, panel_fg, border = ("#262730", "#FAFAFA", "#3d3f4b") if dark else ("#FFFFFF", "#262730", "#E6E6EA")
    icon = bot_icon_png()
    icon_url = "data:image/png;base64," + base64.b64encode(icon).decode()
    open_ = st.session_state.chat_open
    # 닫혀 있을 땐 bot.png 아이콘, 열려 있을 땐 ✕ 버튼
    fab_face = (
        "background: #FFD84D;" if open_
        else f"background: url('{icon_url}') center / cover no-repeat; border: 3px solid #FFFFFF !important;"
    )
    st.markdown(f"""
<style>
.st-key-chat_fab {{
    position: fixed; right: 24px; bottom: 24px; z-index: 1000; width: auto !important;
}}
.st-key-chat_fab button {{
    width: 68px; height: 68px; border-radius: 50%; border: none;
    {fab_face}
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.3);
    transition: transform 0.15s;
}}
.st-key-chat_fab button:hover {{ transform: scale(1.08); }}
.st-key-chat_fab button p {{ font-size: {"28px" if open_ else "0"}; line-height: 1; color: #31333F; }}
.st-key-chat_panel {{
    position: fixed; right: 24px; bottom: 100px; z-index: 1000;
    width: 360px !important; max-width: calc(100vw - 32px);
    padding: 16px; border-radius: 16px; border: 1px solid {border};
    background: {panel_bg}; color: {panel_fg};
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
}}
</style>
""", unsafe_allow_html=True)

    with st.container(key="chat_fab"):
        # 닫혀 있을 땐 글자는 CSS 로 숨기고 그림만 보임
        if st.button("✕" if open_ else "챗봇 열기", key="chat_toggle", help="데이터 올리기 도우미"):
            st.session_state.chat_open = not open_
            st.rerun()

    if not open_:
        return

    with st.container(key="chat_panel"):
        st.markdown(
            f"<img src='{icon_url}' style='width:28px;height:28px;border-radius:50%;vertical-align:middle;margin-right:6px'>"
            "<b>데이터 올리기 도우미</b>",
            unsafe_allow_html=True,
        )
        st.caption("PRD.md 를 읽고 서비스에 대해 답해 드려요")
        # 고른 말투는 다음 질문의 답부터 반영됨 (key 로 창을 닫았다 열어도 고른 값이 남음)
        tone = st.selectbox("말투", list(CHAT_TONES), key="chat_tone")

        with st.container(height=320):
            with st.chat_message("assistant", avatar=icon):
                st.markdown(CHAT_GREETING)
            for m in st.session_state.chat_history:
                if m["role"] == "assistant":
                    with st.chat_message("assistant", avatar=icon):
                        # 마크다운은 줄바꿈 하나를 띄어쓰기로 합치므로, 줄 끝에 공백 두 칸을 붙여 줄이 바뀌게 함
                        st.markdown(m["text"].replace("\n", "  \n"))
                        copy_button(m["text"], dark)
                else:
                    with st.chat_message("user"):
                        st.markdown(m["text"])

        with st.form("chat_form", clear_on_submit=True, border=False):
            cols = st.columns([4, 1])
            question = cols[0].text_input("물어볼 말", placeholder="무엇이든 물어보세요", label_visibility="collapsed")
            sent = cols[1].form_submit_button("보내기")

        # 기록 저장 실패는 답을 보여 준 뒤(rerun 뒤)에 한 번만 알림
        if "chat_log_error" in st.session_state:
            st.warning(f"답은 받았지만 기록을 남기지 못했어요: {st.session_state.pop('chat_log_error')}")

        # 켰을 때만 Supabase 에서 지난 기록을 불러옴
        if st.toggle("지난 기록 보기", key="chat_log_open"):
            chat_log_list()

        if sent and question.strip():
            if not GEMINI_API_KEY:
                st.warning(".env 에 GEMINI_API_KEY 를 먼저 채워 주세요.")
                return
            history = st.session_state.chat_history + [{"role": "user", "text": question.strip()}]
            with st.spinner("답을 쓰는 중이에요..."):
                try:
                    answer = ask_chatbot(GEMINI_API_KEY, history, tone)
                except requests.RequestException as e:
                    st.error(f"답을 받지 못했어요: {error_text(e)}")
                    return
                except OSError as e:
                    st.error(f"지시문 파일(chatbot_instruction.txt·chatbot_format_lock.txt)이나 PRD.md 를 읽지 못했어요: {e}")
                    return
            answer = answer or "(빈 답이 왔어요)"
            try:
                save_chat_log(question.strip(), tone, answer)
            except requests.RequestException as e:
                st.session_state.chat_log_error = error_text(e)
            st.session_state.chat_history = history + [{"role": "assistant", "text": answer}]
            st.rerun()



if st.session_state.page == 0:
    landing()
else:
    # 기능 화면 사이드바에는 첫 화면으로 돌아가는 버튼 하나만 둠
    if st.sidebar.button("첫 화면으로", width="stretch"):
        go(0)

    if st.session_state.page == 1:
        page_header("데이터 올리기", "표·글 파일을 넣으면 태그가 붙은 소재 카드가 만들어집니다.")
        screen = screen_1
    elif st.session_state.page == 2:
        page_header("쓰인 분류 기준 확인", screen_2_description())
        screen = screen_2
    else:
        page_header("분류 결과", "소재 카드마다 대상·분야·정보유형 태그와 키워드가 붙어 있습니다.")
        screen = screen_3

    # 화면마다 바뀌는 내용은 테두리 상자 안에 둠 (머리 부분은 상자 밖에서 세 화면 공통)
    with st.container(border=True):
        screen()

chatbot()
