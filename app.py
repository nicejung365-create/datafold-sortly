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

st.set_page_config(page_title="데이터 마이그레이션")

ENV_PATH = Path(__file__).parent / ".env"
PRD_PATH = Path(__file__).parent / "PRD.md"
TABLE = "classification_results"
NOT_CLASSIFIED = "분류 전"

# helper.py 와 같은 방식 — 공식 문서(ai.google.dev/gemini-api/docs/interactions, structured-output)의 REST 호출
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# 문서 예시 모델(gemini-3.8-flash)은 이 키로 응답이 오지 않아, 응답이 확인된 모델을 씀
GEMINI_MODEL = "gemini-3.5-flash-lite"
CHUNK_SIZE = 20  # 한 번에 AI 에게 보내는 소재 수

TAG_AXES = {"lifecycle": "생애주기", "field": "분야", "info_type": "정보유형"}
# Supabase 에 아직 결과가 없을 때 참고할 처음 기준
BASE_CRITERIA = {"lifecycle": ["전체", "청년"], "field": ["일자리"], "info_type": []}

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
        return f"{e} — {e.response.text}"
    return str(e)


def save_rows(rows):
    res = requests.post(BASE_URL, headers=HEADERS, json=rows, timeout=10)
    res.raise_for_status()


def fetch_rows(batch_id):
    params = {
        "select": "item_order,source_name,title,content,lifecycle,field,info_type",
        "batch_id": f"eq.{batch_id}",
        "order": "item_order.asc",
    }
    res = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def fetch_existing_criteria():
    # 이전에 Supabase 에 저장된 태그 이름 = 기존 기준
    res = requests.get(BASE_URL, headers=HEADERS, params={"select": ",".join(TAG_AXES)}, timeout=10)
    res.raise_for_status()
    criteria = {axis: set(names) for axis, names in BASE_CRITERIA.items()}
    for row in res.json():
        for axis in TAG_AXES:
            if row[axis] and row[axis] != NOT_CLASSIFIED:
                criteria[axis].add(row[axis])
    return criteria


def rename_criterion(batch_id, axis, old_name, new_name):
    params = {"batch_id": f"eq.{batch_id}", axis: f"eq.{old_name}"}
    res = requests.patch(BASE_URL, headers=HEADERS, params=params, json={axis: new_name}, timeout=10)
    res.raise_for_status()


# ---------- Gemini 로 생애주기·분야·정보유형 분류하기 ----------

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "no": {"type": "integer"},
                    "lifecycle": {"type": "string"},
                    "field": {"type": "string"},
                    "info_type": {"type": "string"},
                },
                "required": ["no", "lifecycle", "field", "info_type"],
            },
        }
    },
    "required": ["results"],
}


def ask_gemini(api_key, message):
    return call_gemini(api_key, {
        "model": GEMINI_MODEL,
        "input": message,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": CLASSIFY_SCHEMA},
    })


def call_gemini(api_key, body):
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    res = requests.post(GEMINI_URL, headers=headers, json=body, timeout=120)
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


def classify_prompt(items, criteria):
    lines = [
        "너는 콘텐츠 제작용 데이터를 분류하는 도우미다.",
        "아래 소재마다 생애주기(lifecycle)·분야(field)·정보유형(info_type)을 하나씩 정해라.",
        "- 기존 기준은 소재 내용과 정확히 들어맞을 때만 그 이름을 글자 그대로 써라.",
        "- 비슷해 보인다는 이유로 기존 기준을 억지로 고르지 마라. 들어맞는 기존 기준이 없거나 애매하면, "
        "내용을 잘 나타내는 짧은 한국어 이름(2~8글자)을 새로 지어라.",
        "- 생애주기는 글에 특정 연령·대상(예: 영유아, 청소년, 청년, 어르신)이 드러날 때만 그 대상으로 하고, "
        "드러나지 않으면 '전체'로 하라.",
        "- 분야는 소재가 다루는 주제(예: 체육, 문화, 환경, 주거, 교통)로 정하라.",
        "- 모든 소재에 대해 no 를 그대로 붙여 답하라.",
        "",
        "[기존 기준]",
    ]
    for axis, label in TAG_AXES.items():
        names = ", ".join(sorted(criteria[axis])) or "(없음)"
        lines.append(f"{label}({axis}): {names}")
    lines += ["", "[소재]"]
    for no, (title, content) in items:
        lines.append(f"no {no}. 제목: {title} / 내용: {content[:1000]}")
    return "\n".join(lines)


def classify_items(api_key, items, criteria):
    # items: [(제목, 내용), ...] → 같은 순서로 [{"lifecycle":..., "field":..., "info_type":...}, ...]
    criteria = {axis: set(names) for axis, names in criteria.items()}
    numbered = list(enumerate(items, start=1))
    tags = {}
    for start in range(0, len(numbered), CHUNK_SIZE):
        chunk = numbered[start:start + CHUNK_SIZE]
        answer = json.loads(ask_gemini(api_key, classify_prompt(chunk, criteria)))
        # "행사 안내"/"행사안내"처럼 띄어쓰기만 다른 이름은 기존 기준 이름으로 맞춤
        same_name = {axis: {name.replace(" ", ""): name for name in criteria[axis]} for axis in TAG_AXES}
        for r in answer.get("results", []):
            tags[r["no"]] = {}
            for axis in TAG_AXES:
                name = (r.get(axis) or "").strip() or NOT_CLASSIFIED
                tags[r["no"]][axis] = same_name[axis].setdefault(name.replace(" ", ""), name)
        # 앞 묶음에서 새로 만든 기준을 다음 묶음에서도 참고하게 함
        for no, _ in chunk:
            for axis in TAG_AXES:
                if no in tags:
                    criteria[axis].add(tags[no][axis])
    missing = [no for no, _ in numbered if no not in tags]
    if missing:
        raise ValueError(f"AI 답에 {missing} 번 소재의 분류가 빠졌습니다. 다시 눌러 주세요.")
    return [tags[no] for no, _ in numbered]


def used_criteria(tag_list, existing):
    # 이번 분류에 쓰인 기준 목록: [{"axis", "name", "status", "count"}, ...]
    counts = {}
    for tags in tag_list:
        for axis in TAG_AXES:
            counts[(axis, tags[axis])] = counts.get((axis, tags[axis]), 0) + 1
    rows = [
        {"axis": axis, "name": name, "status": "기존" if name in existing[axis] else "신규", "count": n}
        for (axis, name), n in counts.items()
    ]
    order = list(TAG_AXES)
    return sorted(rows, key=lambda r: (order.index(r["axis"]), r["name"]))


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


def make_rows(batch_id, source_type, source_name, items, tag_list):
    return [
        {
            "batch_id": batch_id,
            "source_type": source_type,
            "source_name": source_name,
            "item_order": i,
            "title": title or None,
            "content": content,
            **tags,
        }
        for i, ((title, content), tags) in enumerate(zip(items, tag_list), start=1)
    ]


if "page" not in st.session_state:
    st.session_state.page = 1


def go(page):
    st.session_state.page = page
    st.rerun()


def screen_1():
    st.title("AI 기반 데이터 자동분류·가공 플랫폼")
    st.write("표·글 파일을 넣으면 태그가 붙은 소재 카드가 만들어집니다.")

    uploaded_file = st.file_uploader(
        "파일 선택 (표 또는 글 파일 1개)",
        type=["xlsx", "csv", "docx", "txt"],
    )
    st.caption(".xlsx · .csv · .docx · .txt")

    st.markdown("<p style='text-align:center'>또는</p>", unsafe_allow_html=True)

    url = st.text_input("게시물 URL 입력", placeholder="URL을 입력하세요 (예: https://...)")
    st.caption("게시물 페이지 내용을 읽어와 파일처럼 분류합니다.")

    if st.button("분류하기"):
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
        else:
            # URL 페이지 내용 읽기 방법은 아직 정하지 않아, 지금은 URL 자체를 소재 1건으로 분류
            items = [(url.strip(), url.strip())]
            source_type, source_name = "url", url.strip()

        if not items:
            st.warning("파일에서 읽어 온 내용이 없습니다. 다른 파일을 올려 주세요.")
            return

        try:
            existing = fetch_existing_criteria()
        except requests.RequestException as e:
            st.error(f"Supabase에서 기존 기준을 불러오지 못했습니다: {error_text(e)}")
            return

        with st.spinner(f"AI가 소재 {len(items)}건을 분류하는 중입니다..."):
            try:
                tag_list = classify_items(GEMINI_API_KEY, items, existing)
            except requests.RequestException as e:
                st.error(f"AI 분류를 받지 못했습니다: {error_text(e)}")
                return
            except (ValueError, KeyError, TypeError) as e:
                st.error(f"AI 답을 읽지 못했습니다: {e}")
                return

        batch_id = str(uuid.uuid4())
        try:
            save_rows(make_rows(batch_id, source_type, source_name, items, tag_list))
        except requests.RequestException as e:
            st.error(f"Supabase에 저장하지 못했습니다: {error_text(e)}")
            return

        st.session_state.batch_id = batch_id
        st.session_state.criteria = used_criteria(tag_list, existing)
        go(2)


def screen_2():
    st.title("쓰인 분류 기준 확인")

    criteria = st.session_state.get("criteria", [])
    existing_rows = [c for c in criteria if c["status"] == "기존"]
    new_rows = [c for c in criteria if c["status"] == "신규"]

    if new_rows:
        st.write("새로운 분류가 발견되었습니다. 이름을 확인하고 저장해 주세요.")
    else:
        st.write("이번 분류는 모두 기존 기준으로 나뉘었습니다.")

    st.subheader("이번 분류에 쓰인 기준 목록")
    st.caption("원래 기준과 새로 생긴 기준이 구분되어 있습니다.")

    widths = [1, 4, 1]
    head = st.columns(widths)
    head[0].markdown("**구분**")
    head[1].markdown("**기준 이름**")
    head[2].markdown("**쓰인 소재 수**")

    st.markdown("**원래 기준**")
    if not existing_rows:
        st.caption("이번 분류에 쓰인 원래 기준이 없습니다.")
    for c in existing_rows:
        row = st.columns(widths)
        row[0].write("기존")
        row[1].write(f"{TAG_AXES[c['axis']]} : {c['name']}")
        row[2].write(f"{c['count']}건")

    st.markdown("**새로 생긴 기준**")
    if not new_rows:
        st.caption("새로 생긴 기준이 없습니다.")
    edited = []
    for c in new_rows:
        row = st.columns(widths)
        row[0].write("신규")
        # 앞의 "생애주기 :" 같은 구분은 고정이고, 이름 부분만 고칠 수 있음
        name_col = row[1].columns([1, 3])
        name_col[0].write(f"{TAG_AXES[c['axis']]} :")
        edited.append(name_col[1].text_input(
            "이름 고치는 칸", value=c["name"], key=f"new_{c['axis']}_{c['name']}", label_visibility="collapsed"
        ).strip())
        row[2].write(f"{c['count']}건")

    st.caption("신규 기준만 이름을 고칠 수 있음 · 기존 기준은 읽기 전용")

    col_save, col_next = st.columns([1, 3])
    if col_save.button("저장"):
        if any(not name for name in edited):
            st.warning("기준 이름을 비워 둘 수 없습니다.")
        else:
            try:
                for c, name in zip(new_rows, edited):
                    if name != c["name"]:
                        rename_criterion(st.session_state.batch_id, c["axis"], c["name"], name)
                        c["name"] = name
            except requests.RequestException as e:
                st.error(f"Supabase에 저장하지 못했습니다: {error_text(e)}")
            else:
                st.success("새로 생긴 기준의 이름을 저장했습니다.")
    if col_next.button("결과화면 보기"):
        go(3)


def screen_3():
    st.title("분류 결과")

    try:
        rows = fetch_rows(st.session_state.get("batch_id", ""))
    except requests.RequestException as e:
        st.error(f"Supabase에서 결과를 불러오지 못했습니다: {error_text(e)}")
        rows = []

    if rows:
        st.write(f"소재 카드 {len(rows)}장 ({rows[0]['source_name']})")
    else:
        st.write("Supabase에 저장된 결과가 없습니다.")
    st.caption("Supabase classification_results 표에 저장된 내용입니다. 태그는 AI가 분류했고, 고치는 기능은 없습니다.")

    for row in rows:
        with st.container(border=True):
            st.markdown(f"**{row['title'] or row['content'][:50]}**")
            st.caption(row["content"][:200])
            st.markdown(
                f"`생애주기 : {row['lifecycle']}` `분야 : {row['field']}` `정보유형 : {row['info_type']}`"
            )

    df = pd.DataFrame(rows, columns=["item_order", "title", "content", "lifecycle", "field", "info_type", "source_name"])
    df.columns = ["순서", "제목", "내용", "생애주기", "분야", "정보유형", "출처"]
    csv_data = df.to_csv(index=False).encode("utf-8-sig")

    col_down, col_home = st.columns([1, 3])
    col_down.download_button("다운로드", data=csv_data, file_name="분류결과.csv", mime="text/csv")
    if col_home.button("처음 화면으로"):
        go(1)


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


st.sidebar.write(
    "표·글 파일이나 게시물 URL을 넣으면 생애주기·분야·정보유형 기준으로 자동 분류해, "
    "콘텐츠 제작에 바로 쓸 수 있는 데이터로 만들어 줍니다."
)

if st.session_state.page == 1:
    screen_1()
elif st.session_state.page == 2:
    screen_2()
else:
    screen_3()

chatbot()
