"""고정 기준(대상·분야·정보유형)으로 소재를 분류하고 키워드를 붙이는 부분.

app.py(화면)와 remap_fixed.py(지난 분류 결과를 고정 기준으로 옮겨 담기)가 함께 씀.
고정 기준 근거: data/sortly-data.png
"""
import copy
import json
import threading
from collections import namedtuple
from datetime import datetime

import anthropic
import requests

# Claude API (공식 SDK: pip install anthropic) — 키는 .env 의 ANTHROPIC_API_KEY
CLAUDE_MODEL = "claude-opus-5-5"
# 안전 분류기가 요청을 거절하면 서버가 Anthropic 권장 모델로 다시 돌려 주는 기능 (거절 대신 답을 받음)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Claude Opus 5.5 가격 (100만 토큰당 달러) — 화면의 추정 비용 계산에만 씀
CLAUDE_PRICE_PER_MTOK = {"input": 4.00, "output": 20.00, "cache_read": 0.20}

# Claude 가 소진·한도·장애로 안 될 때 이어서 쓰는 예비 AI (키는 .env 의 GEMINI_API_KEY)
# helper.py 와 같은 방식 — 공식 문서(ai.google.dev/gemini-api/docs/interactions, structured-output)의 REST 호출
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MODEL = "gemini-3.5-flash-lite"

# 두 AI 의 키 묶음 (화면·스크립트에서 .env 를 읽어 만들어 넘김)
AIKeys = namedtuple("AIKeys", ["claude", "gemini"])
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

# 구조화된 출력(JSON 스키마)으로 받음 — enum 으로 고정 기준 이름만 고를 수 있게 함
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "no": {"type": "integer"},
                    "target": {"type": "array", "items": {"type": "string", "enum": FIXED_CRITERIA["lifecycle"]}},
                    "field": {"type": "string", "enum": FIXED_CRITERIA["field"]},
                    "info_type": {"type": "string", "enum": FIXED_CRITERIA["info_type"]},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["no", "target", "field", "info_type", "keywords"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


class AIRefusal(ValueError):
    """Claude 가 (대체 모델까지) 답을 거절했을 때"""


class AIUnavailable(RuntimeError):
    """Claude 도 Gemini 도 쓸 수 없을 때 — 화면에 그대로 보여 줄 문구"""


# ---------- Claude 상태 (화면 사이드바에 보여 줌) ----------
# 크레딧 잔액을 알려 주는 API 는 없어서, 응답마다 알 수 있는 것만 모아 둠:
#   분당 남은 한도(응답 머리글 anthropic-ratelimit-*), 이 앱이 쓴 토큰, 마지막으로 Gemini 로 넘긴 까닭
_lock = threading.Lock()
STATUS = {
    "state": "unused",        # unused(아직 안 부름) / ok(정상) / fallback(Claude 가 안 돼 Gemini 사용)
    "reason": "",             # Gemini 로 넘긴 까닭
    "limits": {},             # 마지막 응답의 분당 한도: {"requests": (남음, 한도), "tokens": (...), ...}
    "tokens": {"input": 0, "output": 0, "cache_read": 0},
    "claude_calls": 0,
    "gemini_calls": 0,
    "updated": None,
}


def status_snapshot():
    with _lock:
        return copy.deepcopy(STATUS)


def estimated_cost(tokens):
    """이 앱이 쓴 Claude 토큰 × 가격 (계산한 값, 달러)"""
    return sum(tokens[k] * CLAUDE_PRICE_PER_MTOK[k] for k in tokens) / 1_000_000


def _record_claude(raw, response):
    limits = {}
    for name in ("requests", "tokens", "input-tokens", "output-tokens"):
        left = raw.headers.get(f"anthropic-ratelimit-{name}-remaining")
        cap = raw.headers.get(f"anthropic-ratelimit-{name}-limit")
        if left is not None and cap is not None:
            limits[name] = (int(left), int(cap))
    usage = response.usage
    with _lock:
        STATUS["state"], STATUS["reason"] = "ok", ""
        if limits:
            STATUS["limits"] = limits
        STATUS["tokens"]["input"] += (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        STATUS["tokens"]["output"] += usage.output_tokens or 0
        STATUS["tokens"]["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        STATUS["claude_calls"] += 1
        STATUS["updated"] = datetime.now()


def _record_fallback(reason):
    with _lock:
        STATUS["state"], STATUS["reason"] = "fallback", reason
        STATUS["gemini_calls"] += 1
        STATUS["updated"] = datetime.now()


def exhausted_reason(e):
    """Claude 를 지금 쓸 수 없어 Gemini 로 넘겨야 하면 그 까닭(글), 넘기지 않을 오류면 None.
    넘김: 크레딧 소진·사용 한도(월 한도 포함)·서버 장애·연결 실패 / 안 넘김: 키 틀림 등 고쳐야 하는 오류"""
    if isinstance(e, anthropic.RateLimitError):
        body = getattr(e, "body", None)
        code = None
        if isinstance(body, dict):
            details = (body.get("error") or {}).get("details") or {}
            code = details.get("error_code")
        if code == "enforced_spend_limit_reached":
            return "Claude 이번 달 사용 한도에 도달했습니다"
        return "Claude 분당 사용 한도에 걸렸습니다"
    if isinstance(e, anthropic.APIStatusError):
        message = str(getattr(e, "message", "") or e).lower()
        if e.status_code == 400 and "credit balance" in message:
            return "Claude 크레딧이 다 소진되었습니다"
        if e.status_code == 400 and "usage limits" in message:
            return "직접 정한 Claude 사용 한도에 도달했습니다"
        if e.status_code >= 500:
            return f"Claude 서버가 응답하지 않습니다 ({e.status_code})"
        return None
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude 에 연결하지 못했습니다"
    return None


def _client(api_key):
    return anthropic.Anthropic(api_key=api_key)


def _text(response):
    # 거절이면 content 를 읽지 않고 알림
    if response.stop_reason == "refusal":
        raise AIRefusal("AI가 이 내용에 대한 답을 거절했습니다. 다른 자료로 다시 해 주세요.")
    if response.stop_reason == "max_tokens":
        raise ValueError("AI 답이 너무 길어 중간에 끊겼습니다. 소재 수를 줄여 다시 해 주세요.")
    return "".join(block.text for block in response.content if block.type == "text")


def _claude(api_key, **params):
    """Claude 한 번 부르기 → (답 글, 실제로 답한 모델 이름). 분당 한도 머리글을 읽으려고 raw 응답으로 받음"""
    raw = _client(api_key).beta.messages.with_raw_response.create(
        model=CLAUDE_MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default", **params,
    )
    response = raw.parse()
    _record_claude(raw, response)
    return _text(response), response.model


# ---------- 예비 AI: Gemini ----------

def _call_gemini(api_key, body):
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    res = requests.post(GEMINI_URL, headers=headers, json=body, timeout=120)
    res.raise_for_status()
    # 답은 steps 중 type 이 "model_output" 인 단계의 content 안 text 에 들어 있음
    return "".join(
        part.get("text", "")
        for step in res.json().get("steps", [])
        if step.get("type") == "model_output"
        for part in step.get("content", [])
        if part.get("type") == "text"
    )


def _gemini_schema(schema):
    # Claude 용 스키마에서 additionalProperties·enum 을 뺀 모양 (예전에 Gemini 에서 쓰던 그대로)
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k not in ("additionalProperties", "enum")}
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


def _with_fallback(keys, claude_call, gemini_call):
    """Claude 를 먼저 부르고(요청마다), 소진·한도·장애면 Gemini 로 같은 요청을 보냄 → (답 글, 모델 이름)"""
    try:
        return claude_call()
    except anthropic.APIError as e:
        reason = exhausted_reason(e)
        if reason is None:
            raise  # 키 틀림 등은 Gemini 로 넘기지 않고 화면에 알림
        if not keys.gemini:
            raise AIUnavailable(f"{reason}. .env 에 GEMINI_API_KEY 가 없어 Gemini 로 이어서 하지 못했습니다.")
        _record_fallback(reason)
        try:
            return gemini_call(), GEMINI_MODEL
        except requests.RequestException as ge:
            raise AIUnavailable(f"{reason}. Gemini 로 이어서 하려 했지만 실패했습니다: {ge}")


def ask_json(keys, message, schema, effort="low"):
    """질문 하나를 보내고 schema 모양의 JSON 답을 받음 → (dict, 답한 모델 이름)"""
    text, model = _with_fallback(
        keys,
        lambda: _claude(
            keys.claude,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": message}],
        ),
        lambda: _call_gemini(keys.gemini, {
            "model": GEMINI_MODEL,
            "input": message,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": _gemini_schema(schema)},
        }),
    )
    return json.loads(text), model


def chat(keys, system, messages):
    """도우미 챗봇: system 지시문 + 지금까지의 대화([{"role": "user"/"assistant", "content": 글}, ...])
    → (답 글, 답한 모델 이름)"""
    return _with_fallback(
        keys,
        lambda: _claude(keys.claude, output_config={"effort": "low"}, system=system, messages=messages),
        # Gemini 는 대화를 저장하지 않는 방식(store=false)으로 지금까지의 대화를 input 배열로 보냄
        lambda: _call_gemini(keys.gemini, {
            "model": GEMINI_MODEL,
            "store": False,
            "system_instruction": system,
            "input": [
                {
                    "type": "user_input" if m["role"] == "user" else "model_output",
                    "content": [{"type": "text", "text": m["content"]}],
                }
                for m in messages
            ],
        }),
    )


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


def classify_items(keys, items):
    """items: [(제목, 내용), ...] → 같은 순서로 [{"lifecycle", "field", "info_type", "keywords", "ai_model"}, ...]
    keys: AIKeys(claude=..., gemini=...) — Claude 가 안 되면 그 묶음부터 Gemini 로 분류함"""
    numbered = list(enumerate(items, start=1))
    tags = {}
    for start in range(0, len(numbered), CHUNK_SIZE):
        chunk = numbered[start:start + CHUNK_SIZE]
        answer, model = ask_json(keys, classify_prompt(chunk), CLASSIFY_SCHEMA)
        for r in answer.get("results", []):
            tags[r["no"]] = {**normalize(r), "ai_model": model}
    missing = [no for no, _ in numbered if no not in tags]
    if missing:
        raise ValueError(f"AI 답에 {missing} 번 소재의 분류가 빠졌습니다. 다시 눌러 주세요.")
    return [tags[no] for no, _ in numbered]


def split_names(value):
    """한 칸에 이어 저장한 대상·키워드를 다시 목록으로"""
    return [v.strip() for v in (value or "").split(",") if v.strip()]
