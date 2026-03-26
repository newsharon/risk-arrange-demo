from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Dict, List, Tuple

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from real_demo_app import agent_comment, level_color, load_live_data


@dataclass
class TyphoonPoint:
    timestamp: datetime
    lat: float
    lon: float
    intensity: str


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def create_typhoon_track(scenario: str) -> List[TyphoonPoint]:
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    if scenario == "서해 북상형":
        base = [(33.6, 125.8), (34.4, 126.2), (35.1, 126.6), (35.9, 126.9), (36.6, 127.1), (37.2, 127.4)]
    elif scenario == "남해 상륙형":
        base = [(33.1, 127.3), (34.0, 127.6), (34.9, 128.0), (35.7, 128.4), (36.5, 128.8), (37.1, 129.2)]
    else:  # 동해 통과형
        base = [(33.9, 129.5), (34.8, 129.3), (35.7, 129.2), (36.5, 129.0), (37.3, 128.9), (38.0, 128.7)]

    intensity_levels = ["강", "강", "중", "중", "약", "약"]
    out: List[TyphoonPoint] = []
    for i, (lat, lon) in enumerate(base):
        out.append(TyphoonPoint(timestamp=now + timedelta(hours=i * 4), lat=lat, lon=lon, intensity=intensity_levels[i]))
    return out


@st.cache_data
def build_contracts(factory_meta: pd.DataFrame) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for _, r in factory_meta.iterrows():
        fid = int(r["factory_id"])
        coverage_type = "종합재산보험"
        if fid % 3 == 0:
            coverage_type = "재산종합 + 물류지연 특약"
        elif fid % 3 == 1:
            coverage_type = "화재/침수 패키지"
        peril_typhoon = True
        peril_flood = True if fid % 2 == 0 else False
        peril_wind = True
        peril_bi = True if fid % 3 != 2 else False
        insured_amount = 2_000_000_000 + fid * 350_000_000
        deductible = 30_000_000 + fid * 2_000_000
        rows.append(
            {
                "factory_id": fid,
                "contract_id": f"POL-{2026}{fid:04d}",
                "policy_holder": r["factory_name"],
                "product_name": coverage_type,
                "insured_amount_krw": insured_amount,
                "deductible_krw": deductible,
                "cover_typhoon": peril_typhoon,
                "cover_flood": peril_flood,
                "cover_wind": peril_wind,
                "cover_business_interruption": peril_bi,
                "policy_note": "풍수해 위험 발생 시 긴급복구비/휴업손해 일부 보장(약관 기준)",
            }
        )
    return pd.DataFrame(rows)


def evaluate_impact(lat: float, lon: float, track: List[TyphoonPoint], corridor_km: float) -> Tuple[bool, float]:
    distances = [haversine_km(lat, lon, p.lat, p.lon) for p in track]
    min_d = min(distances) if distances else 9999.0
    return min_d <= corridor_km, min_d


def make_action_guidance(row: pd.Series) -> str:
    guide = []
    if row["Risk_Score"] >= 75:
        guide.append("현장 비상대응 단계 상향")
    if row["FloodRisk"] >= 60:
        guide.append("배수로/저지대 설비 사전 보호")
    if row["LogisticsRisk"] >= 55:
        guide.append("출하 일정 재조정 및 운송사 사전 협의")
    if row["cover_business_interruption"]:
        guide.append("휴업손해 특약 청구 필요서류 사전 안내")
    else:
        guide.append("휴업손해 담보 제외 여부 사전 고지")
    return " / ".join(guide[:3])


def render_map(lat_center: float, lon_center: float, latest_df: pd.DataFrame, impacted_ids: set[int], track: List[TyphoonPoint]) -> None:
    m = folium.Map(location=[lat_center, lon_center], zoom_start=7, tiles="cartodbpositron")

    # typhoon track
    track_coords = [(p.lat, p.lon) for p in track]
    folium.PolyLine(locations=track_coords, color="#2c3e50", weight=4, tooltip="태풍 예측 경로").add_to(m)
    for p in track:
        folium.CircleMarker(
            location=[p.lat, p.lon],
            radius=6,
            color="#34495e",
            fill=True,
            fill_opacity=0.9,
            popup=f"{p.timestamp:%m-%d %H:%M} / 강도:{p.intensity}",
        ).add_to(m)

    # factories
    for _, r in latest_df.iterrows():
        fid = int(r["factory_id"])
        impacted = fid in impacted_ids
        border_color = "#8e44ad" if impacted else level_color(r["Risk_Level"])
        popup = (
            f"<b>{r['factory_name']}</b><br/>"
            f"위험도: {r['Risk_Score']:.1f} ({r['Risk_Level']})<br/>"
            f"영향권: {'YES' if impacted else 'NO'}"
        )
        folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=11 if impacted else 8,
            color=border_color,
            weight=3 if impacted else 2,
            fill=True,
            fill_color=level_color(r["Risk_Level"]),
            fill_opacity=0.85,
            popup=popup,
        ).add_to(m)
    st_folium(m, use_container_width=True, height=560)


def main() -> None:
    st.set_page_config(page_title="보험 계약관리 위험알림 데모", layout="wide")
    st.title("TCS (Total Consulting System) 데모")
    st.caption("태풍 경로 예측 -> 영향 공장 식별 -> 계약조건 점검 -> 행동지침 알림")
    st.markdown(
        """
        <style>
        /* 너무 과한 축소로 잘리는 문제를 피한 컴팩트 레이아웃 */
        html, body { font-size: 13px !important; }
        [data-testid="stSidebar"] {
            min-width: 210px !important;
            max-width: 210px !important;
        }
        .block-container {
            padding-top: 1.0rem !important;
            padding-bottom: 1.0rem !important;
            padding-left: 1.8rem !important;
            padding-right: 1.8rem !important;
            max-width: 1700px !important;
        }
        h1 { font-size: 2.1rem !important; margin-bottom: 0.4rem !important; }
        h2, h3 { margin-top: 0.55rem !important; margin-bottom: 0.45rem !important; }
        [data-testid="stMetricValue"] { font-size: 1.55rem !important; }
        [data-testid="stMetricLabel"] { font-size: 0.9rem !important; }
        [data-testid="stDataFrame"] { font-size: 0.84rem !important; }

        .alert-box {
            border: 2px solid #e74c3c;
            border-radius: 10px;
            padding: 14px 16px;
            background: #fff5f5;
            margin-bottom: 8px;
        }
        .alert-title {
            color: #c0392b;
            font-weight: 700;
            font-size: 1.05rem;
            margin-bottom: 6px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("데모 설정")
        scenario = st.selectbox("태풍 시나리오", ["서해 북상형", "남해 상륙형", "동해 통과형"])
        corridor_km = st.slider("영향권 반경 (km)", min_value=30, max_value=180, value=90, step=10)
        sido = st.text_input("시/도", value="경기도")
        sigungu = st.text_input("시/군/구", value="평택시")
        factory_limit = st.slider("조회 공장 수", min_value=3, max_value=20, value=10, step=1)
        if st.button("새로고침"):
            st.cache_data.clear()

    try:
        with st.spinner("실데이터 수집 및 위험 계산 중..."):
            df, logs = load_live_data(sido=sido, sigungu=sigungu, factory_limit=factory_limit)
    except Exception as e:
        st.error(f"데이터 로딩 실패: {e}")
        return

    latest_ts = df["timestamp"].max()
    latest = df[df["timestamp"] == latest_ts].copy()
    if latest.empty:
        st.warning("조회된 공장 데이터가 없습니다.")
        return

    st.subheader("데이터 브리핑")
    b1, b2, b3 = st.columns(3)
    b1.info(
        f"**공장 표본**\n\n"
        f"- 조회 지역: {sido} {sigungu}\n"
        f"- 현재 분석 대상: {len(latest)}개 공장\n"
        f"- 좌표/위치 기반으로 지도 시각화"
    )
    b2.info(
        f"**기상 리스크 계산**\n\n"
        f"- 기준 시각: {latest_ts:%Y-%m-%d %H:%M}\n"
        f"- 현재 + 예측(최대 24시간) 위험 반영\n"
        f"- 침수/과열/품질/물류 위험 종합 점수화"
    )
    b3.info(
        f"**계약 대응 연계**\n\n"
        f"- 공장별 더미 증권 자동 매칭\n"
        f"- 영향권 계약 우선순위 자동 산출\n"
        f"- 현장 행동지침/고객 알림 초안 생성"
    )
    with st.expander("수집 로그 자세히 보기"):
        for msg in logs:
            st.write(f"- {msg}")

    track = create_typhoon_track(scenario)
    impacted_flags: List[Tuple[bool, float]] = []
    for _, r in latest.iterrows():
        impacted, min_dist = evaluate_impact(float(r["lat"]), float(r["lon"]), track, float(corridor_km))
        impacted_flags.append((impacted, min_dist))
    latest["is_impacted"] = [x[0] for x in impacted_flags]
    latest["distance_to_track_km"] = [x[1] for x in impacted_flags]
    impacted_ids = set(latest[latest["is_impacted"]]["factory_id"].astype(int).tolist())

    contracts = build_contracts(latest[["factory_id", "factory_name"]])
    merged = latest.merge(contracts, on="factory_id", how="left")
    impacted_df = merged[merged["is_impacted"]].copy().sort_values(["Risk_Score", "distance_to_track_km"], ascending=[False, True])
    impacted_df["action_guidance"] = impacted_df.apply(make_action_guidance, axis=1)

    total_sum = int(impacted_df["insured_amount_krw"].sum()) if not impacted_df.empty else 0
    high_risk_count = int((impacted_df["Risk_Score"] >= 60).sum()) if not impacted_df.empty else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("영향권 공장 수", f"{len(impacted_df)}개")
    k2.metric("고위험 계약 수", f"{high_risk_count}개")
    k3.metric("영향권 총 가입금액", f"{total_sum:,.0f} 원")
    k4.metric("기준 시각", f"{latest_ts:%m-%d %H:%M}")

    left, right = st.columns([1.9, 1.1])
    with left:
        st.subheader("태풍 경로 + 공장 위험 지도")
        center_lat = float(latest["lat"].mean())
        center_lon = float(latest["lon"].mean())
        render_map(center_lat, center_lon, latest, impacted_ids, track)

    with right:
        st.subheader("영향권 계약 리스트")
        if impacted_df.empty:
            st.info("현재 설정한 경로/반경 기준 영향권 공장이 없습니다.")
        else:
            show_cols = [
                "factory_name",
                "contract_id",
                "product_name",
                "Risk_Score",
                "Risk_Level",
                "distance_to_track_km",
                "insured_amount_krw",
            ]
            st.dataframe(
                impacted_df[show_cols].rename(
                    columns={
                        "factory_name": "공장명",
                        "contract_id": "증권번호",
                        "product_name": "상품",
                        "Risk_Score": "위험점수",
                        "Risk_Level": "등급",
                        "distance_to_track_km": "경로거리(km)",
                        "insured_amount_krw": "가입금액(원)",
                    }
                ),
                use_container_width=True,
                height=360,
            )

    st.divider()
    st.subheader("선택 계약 상세 및 알림 메시지")
    if impacted_df.empty:
        st.caption("영향권 공장이 생기도록 반경을 넓히거나 시나리오를 변경해보세요.")
        return

    option_map = {
        f"{r.factory_name} | {r.contract_id} | 위험 {r.Risk_Score:.1f}": int(r.factory_id)
        for r in impacted_df.itertuples()
    }
    selected_label = st.selectbox("상세 확인 대상", options=list(option_map.keys()))
    selected_id = option_map[selected_label]
    row = impacted_df[impacted_df["factory_id"] == selected_id].iloc[0]

    d1, d2 = st.columns([1.1, 1.2])
    with d1:
        st.markdown("**계약 정보**")
        st.write(f"- 공장: {row['factory_name']}")
        st.write(f"- 증권번호: {row['contract_id']}")
        st.write(f"- 상품: {row['product_name']}")
        st.write(f"- 가입금액: {int(row['insured_amount_krw']):,} 원")
        st.write(f"- 자기부담금: {int(row['deductible_krw']):,} 원")
        st.write(
            f"- 담보: 태풍({row['cover_typhoon']}), 침수({row['cover_flood']}), "
            f"강풍({row['cover_wind']}), 휴업손해({row['cover_business_interruption']})"
        )
        st.write(f"- 약관메모: {row['policy_note']}")

    with d2:
        st.markdown("**알림/행동지침(발송 초안)**")
        comment = agent_comment(row)
        st.markdown(
            f"""
            <div class="alert-box">
                <div class="alert-title">긴급 알림 대상 계약</div>
                <div><b>공장:</b> {row['factory_name']}</div>
                <div><b>증권번호:</b> {row['contract_id']}</div>
                <div><b>현재 위험:</b> {row['Risk_Score']:.1f}점 ({row['Risk_Level']})</div>
                <div><b>태풍 경로 거리:</b> {row['distance_to_track_km']:.1f}km</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.warning(f"핵심 조치: {row['action_guidance']}", icon="🚨")
        st.write(f"요약: {comment['summary']}")
        st.write("추가 권고:")
        for a in comment["actions"]:
            st.write(f"- {a}")

    st.divider()
    st.subheader("영향권 계약 우선순위")
    priority = impacted_df[
        [
            "factory_name",
            "Risk_Score",
            "distance_to_track_km",
            "insured_amount_krw",
            "cover_business_interruption",
            "action_guidance",
        ]
    ].copy()
    priority["priority_score"] = (
        0.5 * priority["Risk_Score"]
        + 0.3 * (100 - priority["distance_to_track_km"].clip(0, 100))
        + 0.2 * (priority["insured_amount_krw"] / priority["insured_amount_krw"].max() * 100)
    )
    priority = priority.sort_values("priority_score", ascending=False)
    st.dataframe(
        priority.rename(
            columns={
                "factory_name": "공장명",
                "Risk_Score": "위험점수",
                "distance_to_track_km": "경로거리(km)",
                "insured_amount_krw": "가입금액(원)",
                "cover_business_interruption": "휴업손해담보",
                "action_guidance": "핵심조치",
                "priority_score": "우선순위점수",
            }
        ),
        use_container_width=True,
        height=320,
    )


if __name__ == "__main__":
    main()
