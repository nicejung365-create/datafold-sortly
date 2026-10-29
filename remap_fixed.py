"""지난 분류 결과를 고정 기준(대상·분야·정보유형)으로 옮겨 담고 키워드를 붙이는 스크립트.

예전에는 AI 가 기준 이름을 그때그때 지어 Supabase classification_results 표에 저장했음.
이 스크립트는 표의 모든 소재를 classifier.py 로 다시 분류해 고정 기준 이름과 키워드로 바꿈.

실행 (먼저 data/new_columns.sql 을 Supabase SQL Editor 에서 한 번 실행해 keywords 칸을 만들어 둘 것)
  python remap_fixed.py           미리보기만: 바뀔 내용을 backup/remap_preview_*.csv 로 남기고 Supabase 는 그대로 둠
  python remap_fixed.py --apply   실제로 바꿈: 바꾸기 전 값을 backup/remap_before_*.csv 로 먼저 남긴 뒤 바꿈

backup/ 폴더는 .gitignore 에 있어 GitHub 에 올라가지 않음 (소재 제목·내용이 들어 있으므로).
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

import requests

from classifier import TAG_AXES, classify_items

ROOT = Path(__file__).parent
TABLE = "classification_results"
COLUMNS = ["batch_id", "item_order", "title", "content", *TAG_AXES, "keywords"]
PAGE = 1000  # Supabase 가 한 번에 돌려주는 줄 수


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


env = load_env()
BASE_URL = f"{env['SUPABASE_URL'].rstrip('/')}/rest/v1/{TABLE}"
HEADERS = {"apikey": env["SUPABASE_KEY"], "Authorization": f"Bearer {env['SUPABASE_KEY']}"}


def fetch_all():
    rows, start = [], 0
    while True:
        params = {"select": ",".join(COLUMNS), "order": "batch_id.asc,item_order.asc"}
        headers = {**HEADERS, "Range": f"{start}-{start + PAGE - 1}"}
        res = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        page = res.json()
        rows += page
        if len(page) < PAGE:
            return rows
        start += PAGE


def update_row(row, tags):
    params = {"batch_id": f"eq.{row['batch_id']}", "item_order": f"eq.{row['item_order']}"}
    res = requests.patch(BASE_URL, headers=HEADERS, params=params, json=tags, timeout=10)
    res.raise_for_status()


def write_csv(path, header, lines):
    path.parent.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(lines)


def main():
    apply = "--apply" in sys.argv
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    rows = fetch_all()
    print(f"Supabase 에서 소재 {len(rows)}건을 읽었습니다.")
    if not rows:
        return

    items = [((r["title"] or "").strip(), r["content"] or "") for r in rows]
    print("AI 로 고정 기준 분류와 키워드를 받는 중입니다...")
    tag_list = classify_items(env.get("GEMINI_API_KEY", ""), items)

    preview = []
    for r, tags in zip(rows, tag_list):
        old = [r.get(a) or "" for a in (*TAG_AXES, "keywords")]
        new = [tags[a] for a in (*TAG_AXES, "keywords")]
        preview.append([r["batch_id"], r["item_order"], r["title"] or r["content"][:50], *old, *new])
    header = ["batch_id", "item_order", "제목",
              "예전 대상", "예전 분야", "예전 정보유형", "예전 키워드",
              "새 대상", "새 분야", "새 정보유형", "새 키워드"]
    preview_path = ROOT / "backup" / f"remap_preview_{stamp}.csv"
    write_csv(preview_path, header, preview)
    changed = sum(1 for line in preview if line[3:7] != line[7:11])
    print(f"바뀔 소재 {changed}건 / 전체 {len(preview)}건 — 미리보기: {preview_path}")

    if not apply:
        print("미리보기만 했습니다. 실제로 바꾸려면: python remap_fixed.py --apply")
        return

    before_path = ROOT / "backup" / f"remap_before_{stamp}.csv"
    write_csv(before_path, COLUMNS, [[r.get(c) for c in COLUMNS] for r in rows])
    print(f"바꾸기 전 값을 남겼습니다: {before_path}")
    for i, (r, tags) in enumerate(zip(rows, tag_list), start=1):
        update_row(r, tags)
        if i % 50 == 0:
            print(f"{i}/{len(rows)}건 바꿈")
    print(f"고정 기준으로 {len(rows)}건을 옮겨 담았습니다.")


if __name__ == "__main__":
    main()
