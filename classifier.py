"""고정 기준(대상·분야·정보유형)으로 소재를 분류하고 키워드를 붙이는 부분.

app.py(화면)와 remap_fixed.py(지난 분류 결과를 고정 기준으로 옮겨 담기)가 함께 씀.
고정 기준 근거: data/sortly-data.png
"""
import json

import requests

# helper.py 와 같은 방식 — 공식 문서(ai.google.dev/gemini-api/docs/interactions, structured-output)의 REST 호출
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# 문서 예시 모델(gemini-3.8-flash)은 이 키로 응답이 오지 않아, 응답이 확인된 모델을 씀
GEMINI_MODEL = "gemini-3.5-flash-lite"
CHUNK_SIZE = 20  # 한 번에 AI 에게 보내는 소재 수

NOT_CLASSIFIED = "분류 전"
ALL_CITIZENS = "전체(시민전체)"
SEPARATOR = ", "  # 대상·키워드를 여러 개 고르면 이 글자로 이어서 한 칸에 저장

# Supabase 칸 이름 → 화면에 보이는 이름 (lifecycle 칸에 '대상'을 저장함)
TAG_AXES = {"lifecycle": "대상", "field": "분야", "info_type": "정보유형"}

# ---------- 고정 기준 (근거: data/sortly-data.png) ----------
TARGET_GROUPS = {
    "전체": [ALL_CITIZENS],
    "생애주기": ["임신/출산", "영유아/아동", "청소년", "청년", "중장년", "어르신"],
    "특성/직업": ["장애인", "저소득/취약계층", "다문화/외국인", "구직자/근로자", "농어업인", "기업/소상공인", "관광객"],
}
FIXED_CRITERIA = {
    "lifecycle": [name for names in TARGET_GROUPS.values() for name in names],
    "field": ["행정", "복지", "보건", "교육", "경제", "농업/수산", "문화/관광", "도시/주거",
              "환경", "안전", "행사", "체육", "교통"],
    "info_type": ["보도자료", "신청", "채용", "고시/공고", "입찰/계약"],
}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "no": {"type": "integer"},
                    "target": {"type": "array", "items": {"type": "string"}},
                    "field": {"type": "string"},
                    "info_type": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["no", "target", "field", "info_type", "keywords"],
            },
        }
    },
    "required": ["results"],
}


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


def ask_gemini(api_key, message):
    return call_gemini(api_key, {
        "model": GEMINI_MODEL,
        "input": message,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": CLASSIFY_SCHEMA},
    })


def classify_prompt(items):
    lines = [
        "너는 공공기관 게시물·데이터를 콘텐츠 제작용으로 분류하는 도우미다.",
        "아래 소재마다 대상(target)·분야(field)·정보유형(info_type)을 [고정 기준] 목록 안에서만 골라라.",
        "- 목록에 없는 이름을 새로 짓지 말고, 고른 이름은 목록의 글자 그대로 써라.",
        f"- 대상은 소재가 겨냥하는 대상을 모두 골라라(여러 개 가능). 생애주기와 특성/직업에서 함께 골라도 된다. "
        f"특정 대상이 드러나지 않으면 '{ALL_CITIZENS}' 하나만 골라라.",
        "- 분야와 정보유형은 가장 알맞은 것을 하나씩 골라라. 딱 맞는 것이 없으면 가장 가까운 것을 골라라.",
        "- 키워드(keywords)는 고정 기준만으로 드러나지 않는 구체적 내용(사업·제도 이름, 세부 주제, 장소 등)을 "
        "짧은 한국어 키워드 1~3개(각 2~10글자)로 적어라. 고정 기준 이름과 같은 말은 넣지 마라.",
        "- 모든 소재에 대해 no 를 그대로 붙여 답하라.",
        "",
        "[고정 기준]",
        "대상(target): " + " / ".join(f"[{group}] {', '.join(names)}" for group, names in TARGET_GROUPS.items()),
        "분야(field): " + ", ".join(FIXED_CRITERIA["field"]),
        "정보유형(info_type): " + ", ".join(FIXED_CRITERIA["info_type"]),
        "",
        "[소재]",
    ]
    for no, (title, content) in items:
        lines.append(f"no {no}. 제목: {title} / 내용: {content[:1000]}")
    return "\n".join(lines)


def _key(name):
    # "임신·출산", "임신 / 출산" 처럼 띄어쓰기·구분 기호만 다른 이름을 같은 이름으로 봄
    return "".join(ch for ch in name if ch not in " /·・,()")


_FIXED_KEYS = {axis: {_key(name): name for name in names} for axis, names in FIXED_CRITERIA.items()}
_FIXED_KEYS["lifecycle"].update({_key("전체"): ALL_CITIZENS, _key("시민전체"): ALL_CITIZENS})


def to_fixed(axis, name):
    """AI 가 답한 이름을 고정 기준 이름으로 맞춤. 고정 기준에 없으면 None."""
    return _FIXED_KEYS[axis].get(_key(name or ""))


def normalize(answer):
    """AI 답 한 건 → Supabase 에 저장할 모양 {"lifecycle", "field", "info_type", "keywords"}"""
    targets = {to_fixed("lifecycle", t) for t in answer.get("target") or []} - {None}
    # 특정 대상이 있으면 '전체(시민전체)'는 빼고, 하나도 없으면 '전체(시민전체)'
    if len(targets) > 1:
        targets.discard(ALL_CITIZENS)
    targets = [t for t in FIXED_CRITERIA["lifecycle"] if t in targets] or [ALL_CITIZENS]

    tags = {"lifecycle": SEPARATOR.join(targets)}
    for axis in ("field", "info_type"):
        raw = (answer.get(axis) or "").strip()
        # 고정 기준에 없는 이름은 그대로 두어 화면 2 에서 '고정 기준에 없는 이름'으로 보이고 고칠 수 있게 함
        tags[axis] = to_fixed(axis, raw) or raw or NOT_CLASSIFIED

    fixed_names = {_key(n) for names in FIXED_CRITERIA.values() for n in names}
    keywords = []
    for word in answer.get("keywords") or []:
        word = word.strip().lstrip("#")
        if word and _key(word) not in fixed_names and word not in keywords:
            keywords.append(word)
    tags["keywords"] = SEPARATOR.join(keywords[:3])
    return tags


def classify_items(api_key, items):
    """items: [(제목, 내용), ...] → 같은 순서로 [{"lifecycle", "field", "info_type", "keywords"}, ...]"""
    numbered = list(enumerate(items, start=1))
    tags = {}
    for start in range(0, len(numbered), CHUNK_SIZE):
        chunk = numbered[start:start + CHUNK_SIZE]
        answer = json.loads(ask_gemini(api_key, classify_prompt(chunk)))
        for r in answer.get("results", []):
            tags[r["no"]] = normalize(r)
    missing = [no for no, _ in numbered if no not in tags]
    if missing:
        raise ValueError(f"AI 답에 {missing} 번 소재의 분류가 빠졌습니다. 다시 눌러 주세요.")
    return [tags[no] for no, _ in numbered]


def split_names(value):
    """한 칸에 이어 저장한 대상·키워드를 다시 목록으로"""
    return [v.strip() for v in (value or "").split(",") if v.strip()]
