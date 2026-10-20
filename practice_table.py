from pathlib import Path

import pandas as pd
import streamlit as st

CSV_PATH = Path(__file__).parent / "data" / "56_practice-inquiries.csv"

st.title("문의 내역 표")

df = pd.read_csv(CSV_PATH, encoding="utf-8")
st.caption(f"{CSV_PATH.name} · {len(df)}건")

selected_type = st.selectbox("type", ["전체"] + sorted(df["type"].dropna().unique().tolist()))
shown = df if selected_type == "전체" else df[df["type"] == selected_type]

selected_status = st.selectbox("status", ["전체"] + sorted(df["status"].dropna().unique().tolist()))
if selected_status != "전체":
    shown = shown[shown["status"] == selected_status]

st.write(f"보이는 줄 수: {len(shown)}")
st.dataframe(shown, hide_index=True)
