from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import folium
import pandas as pd
import streamlit as st
from folium.plugins import Fullscreen, MarkerCluster
from streamlit_folium import st_folium

from real_demo_app import (
    Factory,
    agent_comment,
    build_weather_rows,
    compute_risks,
    env_value,
    fetch_openweather,
)

_ROOT = Path(__file__).resolve().parent
DEFAULT_VESSELS_CSV = _ROOT / "maritime_vessels_positions.csv"
DEFAULT_CONTRACTS_CSV = _ROOT / "maritime_contracts_demo.csv"


def _normalize_vessels_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "imo" not in out.columns or "vessel_name" not in out.columns:
        raise ValueError("선박 데이터에 imo, vessel_name 컬럼이 필요합니다.")
    out["imo"] = out["imo"].astype(str).str.strip()
    out["vessel_name"] = out["vessel_name"].astype(str).str.strip()
    out["lat"] = pd.to_numeric(out["lat"], errors="coerce")
    out["lon"] = pd.to_numeric(out["lon"], errors="coerce")
    out = out.dropna(subset=["lat", "lon"])
    if "last_position_at" in out.columns:
        out["last_position_at"] = pd.to_datetime(out["last_position_at"], errors="coerce")
    else:
        out["last_position_at"] = pd.Timestamp.now(tz=None)
    return out


def load_vessel_positions_from_csv(path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            df = None
    if df is None:
        raise RuntimeError(f"CSV를 읽을 수 없습니다: {path}")
    return _normalize_vessels_df(df)


@st.cache_data(show_spinner=False)
def load_marine_contracts_csv(path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            df = None
    if df is None:
        raise RuntimeError(f"계약 CSV를 읽을 수 없습니다: {path}")
    df = df.copy()
    df["imo"] = df["imo"].astype(str).str.strip()
    for col in ["cover_hull", "cover_cargo", "cover_p_i", "cover_delay"]:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.strip()
                .str.lower()
                .isin(["true", "1", "y", "yes"])
            )
    return df


def _attach_weather_risk(vessels: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    logs: List[str] = []
    key = env_value("OPENWEATHER_API_KEY")
    if not key:
        logs.append("OpenWeather 키 없음 → 기상 위험점수 생략")
        return vessels, logs

    rows: List[pd.DataFrame] = []
    v2 = vessels.reset_index(drop=True)
    for pos, r in v2.iterrows():
        fac = Factory(
            factory_id=int(pos) + 1,
            factory_name=str(r["vessel_name"]),
            industry="선박",
            address=f"IMO {r['imo']}",
            lat=float(r["lat"]),
            lon=float(r["lon"]),
        )
        payload = fetch_openweather(fac.lat, fac.lon, key)
        wdf = build_weather_rows(fac, payload)
        rows.append(wdf)

    weather_df = pd.concat(rows, ignore_index=True)
    risk_df = compute_risks(weather_df)
    now_ts = pd.Timestamp.now()
    ut = pd.to_datetime(risk_df["timestamp"]).dropna().drop_duplicates().sort_values()
    pn = ut[ut <= now_ts]
    ts = pn.max() if not pn.empty else ut.min()
    snap = risk_df[risk_df["timestamp"] == ts].copy()
    v2["factory_id"] = range(1, len(v2) + 1)
    merged = v2.merge(
        snap[
            [
                "factory_id",
                "Risk_Score",
                "Risk_Level",
                "temp_c",
                "humidity_pct",
                "wind_m_s",
                "rain_6h_mm",
                "FloodRisk",
                "HeatRisk",
                "QualityRisk",
                "LogisticsRisk",
            ]
        ],
        on="factory_id",
        how="left",
    ).drop(columns=["factory_id"], errors="ignore")
    logs.append(f"기상 위험 스냅샷 시각: {ts}")
    return merged, logs


def render_fleet_map(
    vessels: pd.DataFrame,
    selected_imo: str | None,
) -> None:
    c_lat = float(vessels["lat"].mean())
    c_lon = float(vessels["lon"].mean())
    m = folium.Map(location=[c_lat, c_lon], zoom_start=6, tiles="OpenStreetMap", control_scale=True)
    Fullscreen(position="topright").add_to(m)
    cluster = MarkerCluster(name="fleet").add_to(m)

    for _, r in vessels.iterrows():
        imo = str(r["imo"])
        sel = selected_imo and imo == selected_imo
        risk_txt = ""
        if "Risk_Score" in r and pd.notna(r.get("Risk_Score")):
            risk_txt = f"<br/>위험: {float(r['Risk_Score']):.1f} ({r.get('Risk_Level', '')})"
        t = r.get("last_position_at")
        tstr = pd.Timestamp(t).strftime("%Y-%m-%d %H:%M") if pd.notna(t) else "-"
        sog = r.get("last_position_sog_kn", "")
        sog_txt = f"<br/>SOG: {sog} kn" if sog != "" and pd.notna(sog) else ""
        popup = (
            f"<b>{r['vessel_name']}</b><br/>IMO {imo}<br/>위치 시각: {tstr}{sog_txt}{risk_txt}"
        )
        risk_level = str(r.get("Risk_Level") or "")
        color = (
            "#c0392b"
            if risk_level in ("HIGH", "SEVERE")
            else ("#f39c12" if risk_level in ("MID", "MEDIUM") else "#2980b9")
        )
        icon_color = "red" if sel else ("orange" if color == "#f39c12" else "blue")
        folium.Marker(
            location=[float(r["lat"]), float(r["lon"])],
            popup=popup,
            tooltip=f"{r['vessel_name']} (IMO {imo})",
            icon=folium.Icon(color=icon_color, icon="ship", prefix="fa"),
        ).add_to(cluster)

    if selected_imo:
        sel_rows = vessels[vessels["imo"].astype(str) == str(selected_imo)]
        if not sel_rows.empty:
            sr = sel_rows.iloc[0]
            folium.Circle(
                location=[float(sr["lat"]), float(sr["lon"])],
                radius=18000,
                color="#e67e22",
                weight=2,
                fill=True,
                fill_opacity=0.08,
                tooltip="선택 선박 강조",
            ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, use_container_width=True, height=640)


def contract_summary_for_imo(contracts: pd.DataFrame, imo: str) -> str:
    sub = contracts[contracts["imo"] == imo]
    if sub.empty:
        return "연결된 증권이 없습니다."
    lines = [f"- {r.contract_id}: {r.product_name} ({int(r.insured_amount_krw):,}원)" for r in sub.itertuples()]
    return "\n".join(lines)


def _assess_vessel_alert(row: pd.Series, contracts_for_imo: pd.DataFrame) -> Dict[str, Any]:
    triggers: List[str] = []
    gaps: List[str] = []
    actions: List[str] = []
    severity = "정상"

    cover_hull = bool(contracts_for_imo.get("cover_hull", pd.Series(dtype=bool)).fillna(False).any())
    cover_cargo = bool(contracts_for_imo.get("cover_cargo", pd.Series(dtype=bool)).fillna(False).any())
    cover_pi = bool(contracts_for_imo.get("cover_p_i", pd.Series(dtype=bool)).fillna(False).any())
    cover_delay = bool(contracts_for_imo.get("cover_delay", pd.Series(dtype=bool)).fillna(False).any())

    wind = pd.to_numeric(row.get("wind_m_s"), errors="coerce")
    rain = pd.to_numeric(row.get("rain_6h_mm"), errors="coerce")
    sog = pd.to_numeric(row.get("last_position_sog_kn"), errors="coerce")
    pos_ts = pd.to_datetime(row.get("last_position_at"), errors="coerce")
    age_h = (pd.Timestamp.now() - pos_ts).total_seconds() / 3600 if pd.notna(pos_ts) else None

    if pd.notna(wind) and wind >= 14:
        triggers.append(f"강풍 감지 ({wind:.1f} m/s)")
        severity = "주의"
        if not cover_hull:
            gaps.append("선체 손상 리스크 대비 선체 담보 부족")
        actions.append("선박 속력 감속 및 우회 항로 검토")

    if pd.notna(rain) and rain >= 20:
        triggers.append(f"강수 위험 ({rain:.1f} mm/6h)")
        severity = "주의"
        if not cover_cargo:
            gaps.append("화물 손상 리스크 대비 화물 담보 부족")
        actions.append("화물 고정 상태 점검 및 방수 조치")

    if pd.notna(sog) and sog >= 16:
        triggers.append(f"고속 항해 ({sog:.1f} kn)")
        severity = "주의"
        if not cover_pi:
            gaps.append("충돌/제3자 배상 리스크 대비 P&I 담보 부족")
        actions.append("혼잡 수역 접근 시 감속 및 경계 인원 강화")

    if age_h is not None and age_h >= 4:
        triggers.append(f"위치 수신 지연 ({age_h:.1f}시간)")
        severity = "경고"
        if not cover_delay:
            gaps.append("지연 손실 리스크 대비 지연 담보 부족")
        actions.append("AIS/GPS 통신 상태 점검 및 최신 위치 재수집")

    risk_level = str(row.get("Risk_Level") or "")
    if risk_level in ("HIGH", "SEVERE"):
        severity = "경고"

    if not triggers:
        triggers.append("특이 위험 신호 없음")
        actions.append("현 항로 유지, 30분 간격 모니터링")

    if not gaps:
        gaps.append("현재 감지 리스크 기준 담보 공백 없음")

    actions = list(dict.fromkeys(actions))
    return {"severity": severity, "triggers": triggers, "gaps": gaps, "actions": actions}


def render_marine_insurance_demo(*, embedded: bool = False) -> None:
    if not embedded:
        st.title("선박 실시간 위치 · 증권 조회 데모")
        st.caption("AIS/사내 시스템이 넣어주는 좌표 + IMO 기준 해상·선박 보험 증권을 한 화면에서 확인")

    with st.sidebar:
        st.subheader("해상 관제")
        csv_override = env_value("MARINE_VESSELS_CSV")
        st.caption("지도에서 계약 선박 위치와 IMO별 담보 현황을 확인합니다.")
        show_weather = st.checkbox("해당 좌표 기상 위험(OpenWeather)", value=True, key="marine_weather")
        contracts_path = env_value("MARINE_CONTRACTS_CSV") or str(DEFAULT_CONTRACTS_CSV)

        if st.button("위치 데이터 새로고침", key="marine_refresh_pos"):
            st.cache_data.clear()

    logs: List[str] = []
    try:
        p = csv_override.strip() if csv_override else str(DEFAULT_VESSELS_CSV)
        vessels = load_vessel_positions_from_csv(p)
        logs.append(f"위치 소스: 더미 선박 위치 ({p})")
    except Exception as e:
        st.error(f"선박 위치 로드 실패: {e}")
        st.info("`maritime_vessels_positions.csv` 파일을 확인하세요.")
        return

    if vessels.empty:
        st.warning("표시할 선박이 없습니다.")
        return

    try:
        contracts = load_marine_contracts_csv(contracts_path)
    except Exception as e:
        st.error(f"계약 CSV 로드 실패: {e}")
        return

    if show_weather:
        vessels, wlogs = _attach_weather_risk(vessels)
        logs.extend(wlogs)
    else:
        for col in ["Risk_Score", "Risk_Level", "temp_c", "wind_m_s", "rain_6h_mm", "FloodRisk", "HeatRisk"]:
            if col not in vessels.columns:
                vessels[col] = pd.NA

    st.subheader("요약")
    c1, c2, c3 = st.columns(3)
    c1.metric("추적 선박", f"{len(vessels)}척")
    n_pol = len(contracts[contracts["imo"].isin(vessels["imo"].unique())])
    c2.metric("연결 증권(샘플)", f"{n_pol}건")
    last_fix = pd.to_datetime(vessels["last_position_at"], errors="coerce").max()
    c3.metric("최신 위치 시각", last_fix.strftime("%m-%d %H:%M") if pd.notna(last_fix) else "-")

    with st.expander("수집 로그"):
        for x in logs:
            st.write(f"- {x}")

    imo_list = vessels["imo"].tolist()
    labels = [f"{r.vessel_name} (IMO {r.imo})" for r in vessels.itertuples()]
    label_to_imo = dict(zip(labels, imo_list))
    pick_label = st.selectbox("선박 선택 (지도 강조 · 증권 조회)", options=labels, key="marine_vessel_pick")
    selected_imo = label_to_imo[pick_label]

    left, right = st.columns([2.5, 1.0])
    with left:
        st.subheader("선박 위치 관제 지도")
        render_fleet_map(vessels, selected_imo)

    with right:
        st.subheader("선박 목록")
        disp = vessels[
            ["imo", "vessel_name", "lat", "lon", "last_position_at"]
            + ([c for c in ["last_position_sog_kn", "Risk_Score", "Risk_Level"] if c in vessels.columns])
        ].copy()
        if "last_position_at" in disp.columns:
            disp["last_position_at"] = pd.to_datetime(disp["last_position_at"]).dt.strftime("%m-%d %H:%M")
        st.dataframe(disp.rename(columns={"last_position_at": "위치시각"}), use_container_width=True, height=500)

    st.divider()
    st.subheader(f"증권 목록 — IMO {selected_imo}")
    pol = contracts[contracts["imo"] == selected_imo].copy()
    if pol.empty:
        st.warning("이 선박 IMO에 매칭된 샘플 증권이 없습니다. `maritime_contracts_demo.csv`를 확인하세요.")
    else:
        st.dataframe(
            pol.rename(
                columns={
                    "contract_id": "증권번호",
                    "product_name": "상품",
                    "insured_amount_krw": "보험가액(원)",
                    "deductible_krw": "자기부담(원)",
                    "cover_hull": "선체",
                    "cover_cargo": "화물",
                    "cover_p_i": "P&I",
                    "cover_delay": "지연",
                    "policy_note": "비고",
                }
            ),
            use_container_width=True,
        )

    st.divider()
    st.subheader("위험 알림 · 대응 제안")
    row = vessels[vessels["imo"] == selected_imo].iloc[0]
    st.markdown("**연결 증권 요약**")
    st.markdown(contract_summary_for_imo(contracts, selected_imo))

    alert = _assess_vessel_alert(row, pol)
    if alert["severity"] == "경고":
        st.error(f"알림 수준: {alert['severity']}")
    elif alert["severity"] == "주의":
        st.warning(f"알림 수준: {alert['severity']}")
    else:
        st.success(f"알림 수준: {alert['severity']}")

    st.markdown("**감지 위험**")
    for x in alert["triggers"]:
        st.write(f"- {x}")
    st.markdown("**담보 공백(부족)**")
    for x in alert["gaps"]:
        st.write(f"- {x}")
    st.markdown("**권고 행동**")
    for x in alert["actions"]:
        st.write(f"- {x}")

    if show_weather and pd.notna(row.get("Risk_Score")):
        comment = agent_comment(row)
        st.markdown("**기상 연계 요약(참고)**")
        st.write(comment["summary"])
        for a in comment["actions"]:
            st.write(f"- {a}")
    else:
        st.info("기상 위험을 쓰려면 사이드바에서 「해당 좌표 기상 위험」을 켜고 `OPENWEATHER_API_KEY`를 설정하세요.")

    st.caption("위험 발생 시 IMO 기준 담보 공백을 식별하고, 관제센터 행동 가이드를 즉시 알림으로 제공합니다.")
