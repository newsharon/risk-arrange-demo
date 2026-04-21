"""
TCS 통합 데모: 공장(산업)·태풍 + 선박(해상) 모드 전환.
기존 Cloud 배포(streamlit_app.py → insurance_typhoon_demo_app만)는 변경하지 않음.
로컬/별도 배포: streamlit run tcs_combined_app.py
"""

from __future__ import annotations

import streamlit as st

from insurance_typhoon_demo_app import inject_tcs_styles, render_factory_typhoon_demo
from marine_insurance_demo_app import render_marine_insurance_demo


def main() -> None:
    st.set_page_config(page_title="TCS 통합 데모", layout="wide")
    inject_tcs_styles()

    st.title("TCS (Total Consulting System) 통합 데모")
    st.caption("공장 재난(태풍) · 해상 선박(풍랑) 위험·계약 관리 프로토타입")

    mode = st.radio(
        "업무 모드",
        ["공장(산업) · 태풍", "선박(해상) · 풍랑"],
        horizontal=True,
        key="tcs_mode",
    )
    st.divider()

    if mode.startswith("공장"):
        render_factory_typhoon_demo(embedded=True)
    else:
        render_marine_insurance_demo(embedded=True)


if __name__ == "__main__":
    main()
