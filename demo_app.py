from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List

import folium
import numpy as np
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium


@dataclass
class Factory:
    factory_id: int
    factory_name: str
    industry: str
    lat: float
    lon: float


def scale_linear(x: float, low: float, high: float) -> float:
    if x <= low:
        return 0.0
    if x >= high:
        return 1.0
    return (x - low) / (high - low)


def risk_level(score: float) -> str:
    if score < 25:
        return "green"
    if score < 50:
        return "yellow"
    if score < 75:
        return "orange"
    return "red"


def level_color(level: str) -> str:
    return {
        "green": "#2ecc71",
        "yellow": "#f1c40f",
        "orange": "#e67e22",
        "red": "#e74c3c",
    }[level]


def create_demo_factories() -> List[Factory]:
    return [
        Factory(1, "평택 A전자", "반도체", 36.9942, 127.0887),
        Factory(2, "평택 B화학", "화학", 36.9998, 127.1124),
        Factory(3, "평택 C물류", "물류", 36.9809, 127.1260),
        Factory(4, "평택 D정밀", "정밀", 37.0033, 127.0828),
        Factory(5, "평택 E식품", "식품", 36.9713, 127.1022),
    ]


def generate_forecast(factory: Factory) -> pd.DataFrame:
    seed = factory.factory_id * 100
    rng = np.random.default_rng(seed)
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    rows = []
    for h in range(0, 25):
        ts = now + timedelta(hours=h)
        temp = 24 + factory.factory_id + 5 * np.sin(h / 6.0) + rng.normal(0, 0.8)
        humidity = np.clip(58 + 8 * np.cos(h / 7.0) + rng.normal(0, 4), 35, 96)
        rain = max(0.0, rng.normal(2.2 if 5 <= h <= 12 else 0.8, 2.8))
        wind = max(0.0, rng.normal(4.5 + (h / 8), 1.2))
        rows.append(
            {
                "factory_id": factory.factory_id,
                "timestamp": ts,
                "temp_c": float(temp),
                "humidity_pct": float(humidity),
                "rain_mm_h": float(rain),
                "wind_m_s": float(wind),
            }
        )
    return pd.DataFrame(rows)


def compute_risks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out.sort_values(["factory_id", "timestamp"])
    out["rain_6h_mm"] = (
        out.groupby("factory_id")["rain_mm_h"]
        .rolling(window=6, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
    )

    out["temp_scaled"] = out["temp_c"].apply(lambda v: scale_linear(v, 25, 35))
    out["humidity_scaled"] = out["humidity_pct"].apply(lambda v: scale_linear(v, 60, 90))
    out["rain_scaled"] = out["rain_mm_h"].apply(lambda v: scale_linear(v, 0, 30))
    out["rain_6h_scaled"] = out["rain_6h_mm"].apply(lambda v: scale_linear(v, 0, 80))
    out["wind_scaled"] = out["wind_m_s"].apply(lambda v: scale_linear(v, 5, 15))

    out["HeatRisk"] = 100 * (0.7 * out["temp_scaled"] + 0.3 * out["humidity_scaled"])
    out["FloodRisk"] = 100 * (0.6 * out["rain_scaled"] + 0.4 * out["rain_6h_scaled"])
    out["QualityRisk"] = 100 * (0.6 * out["humidity_scaled"] + 0.4 * out["temp_scaled"])
    out["LogisticsRisk"] = 100 * (0.7 * out["wind_scaled"] + 0.3 * out["rain_scaled"])

    out["Risk_Score"] = (
        0.35 * out["HeatRisk"]
        + 0.30 * out["FloodRisk"]
        + 0.20 * out["QualityRisk"]
        + 0.15 * out["LogisticsRisk"]
    )
    out["Risk_Level"] = out["Risk_Score"].apply(risk_level)
    return out


def agent_comment(row: pd.Series) -> Dict[str, List[str] | str]:
    risks = {
        "침수": float(row["FloodRisk"]),
        "과열": float(row["HeatRisk"]),
        "품질": float(row["QualityRisk"]),
        "물류": float(row["LogisticsRisk"]),
    }
    top2 = sorted(risks.items(), key=lambda x: x[1], reverse=True)[:2]
    trend = "상승"
    summary = (
        f"현재 종합 위험도는 {row['Risk_Score']:.1f}점({row['Risk_Level']})이며, "
        f"주요 위험은 {top2[0][0]}({top2[0][1]:.1f}) / {top2[1][0]}({top2[1][1]:.1f})입니다."
    )

    causes = []
    if row["rain_6h_mm"] >= 20:
        causes.append(f"최근 6시간 누적 강수량이 {row['rain_6h_mm']:.1f}mm로 높습니다.")
    if row["temp_c"] >= 30:
        causes.append(f"기온이 {row['temp_c']:.1f}C로 높아 설비 과열 가능성이 있습니다.")
    if row["humidity_pct"] >= 75:
        causes.append(f"습도가 {row['humidity_pct']:.1f}%로 품질 저하 리스크가 있습니다.")
    if row["wind_m_s"] >= 9:
        causes.append(f"풍속이 {row['wind_m_s']:.1f}m/s로 물류 변동성이 커질 수 있습니다.")
    if not causes:
        causes.append("현재 주요 변수는 임계치 이내이지만 단기 변동을 지속 모니터링해야 합니다.")

    actions = [
        f"향후 6시간 위험 추세({trend})를 기준으로 하역/출하 스케줄을 재점검하세요.",
        "외부 적치물/배수로/냉각 설비를 우선 점검하세요.",
        "orange 이상일 경우 현장 담당자 알림을 즉시 발송하세요.",
    ]
    return {"summary": summary, "causes": causes, "actions": actions}


@st.cache_data
def build_demo_data() -> pd.DataFrame:
    factories = create_demo_factories()
    dfs = [compute_risks(generate_forecast(f)) for f in factories]
    merged = pd.concat(dfs, ignore_index=True)
    meta = pd.DataFrame([f.__dict__ for f in factories])
    return merged.merge(meta, on="factory_id", how="left")


def render_map(lat_center: float, lon_center: float, latest_rows: pd.DataFrame) -> None:
    m = folium.Map(location=[lat_center, lon_center], zoom_start=12, tiles="cartodbpositron")
    for _, r in latest_rows.iterrows():
        popup_html = (
            f"<b>{r['factory_name']}</b><br/>"
            f"업종: {r['industry']}<br/>"
            f"위험도: {r['Risk_Score']:.1f} ({r['Risk_Level']})"
        )
        folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=10,
            color=level_color(r["Risk_Level"]),
            fill=True,
            fill_opacity=0.9,
            popup=popup_html,
        ).add_to(m)
    st_folium(m, use_container_width=True, height=470)


def main() -> None:
    st.set_page_config(page_title="Factory Weather Risk Demo", layout="wide")
    st.title("지도 기반 공장 기상 리스크 데모")
    st.caption("가상 데이터 기반 데모: 현재/6h/24h 위험 + 에이전트 권고")

    df = build_demo_data()
    latest_ts = df["timestamp"].max()
    latest = df[df["timestamp"] == latest_ts].copy()

    c1, c2 = st.columns([2.0, 1.0])
    with c2:
        factory_options = latest.sort_values("factory_name")["factory_name"].tolist()
        selected_name = st.selectbox("공장 선택", options=factory_options, index=0)
        selected_id = int(latest.loc[latest["factory_name"] == selected_name, "factory_id"].iloc[0])

        now_row = df[(df["factory_id"] == selected_id) & (df["timestamp"] == latest_ts)].iloc[0]
        future = df[(df["factory_id"] == selected_id) & (df["timestamp"] > latest_ts)]
        score_6h = float(future.head(6)["Risk_Score"].max()) if not future.empty else float(now_row["Risk_Score"])
        score_24h = float(future.head(24)["Risk_Score"].max()) if not future.empty else float(now_row["Risk_Score"])

        st.metric("현재 위험도", f"{now_row['Risk_Score']:.1f}", now_row["Risk_Level"])
        st.metric("향후 6시간 위험도(최대)", f"{score_6h:.1f}")
        st.metric("향후 24시간 위험도(최대)", f"{score_24h:.1f}")

        comment = agent_comment(now_row)
        st.subheader("에이전트 요약")
        st.write(comment["summary"])
        st.markdown("**주요 원인**")
        for x in comment["causes"]:
            st.write(f"- {x}")
        st.markdown("**대응 권고**")
        for x in comment["actions"]:
            st.write(f"- {x}")

    with c1:
        st.subheader(f"지도 (기준 시각: {latest_ts:%Y-%m-%d %H:%M})")
        render_map(lat_center=36.99, lon_center=127.10, latest_rows=latest)

    st.divider()
    st.subheader("선택 공장 시간대별 위험 추이")
    trend_df = df[df["factory_id"] == selected_id][["timestamp", "Risk_Score", "HeatRisk", "FloodRisk", "QualityRisk", "LogisticsRisk"]]
    st.line_chart(trend_df.set_index("timestamp"))

    st.caption("다음 단계: 실제 공장 API/OpenWeather 데이터로 교체하고, 워커+DB+대시보드를 Docker Compose로 분리 배포")


if __name__ == "__main__":
    main()
