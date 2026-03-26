from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import folium
import pandas as pd
import requests
import streamlit as st
from dotenv import dotenv_values, load_dotenv
from streamlit_folium import st_folium


ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)


def env_value(key: str, default: str = "") -> str:
    # Streamlit Cloud secrets
    try:
        if key in st.secrets and str(st.secrets[key]).strip() != "":
            return str(st.secrets[key]).strip()
    except Exception:
        pass

    # Streamlit 재실행/핫리로드 상황에서도 .env 파일 값을 우선 읽도록 보강
    file_vals = dotenv_values(ENV_PATH)
    v = file_vals.get(key)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    return os.getenv(key, default).strip()


@dataclass
class Factory:
    factory_id: int
    factory_name: str
    industry: str
    address: str
    lat: float
    lon: float


def _pick_first(d: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def discover_local_factory_csv() -> Optional[str]:
    here = Path(__file__).resolve().parent
    candidates = sorted(here.glob("*.csv"))
    if not candidates:
        return None
    return str(candidates[0])


def fetch_factory_rows_from_csv(csv_path: str, sido: str, sigungu: str) -> List[Dict[str, Any]]:
    last_err: Optional[Exception] = None
    df = None
    for enc in ("cp949", "euc-kr", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(csv_path, encoding=enc)
            break
        except Exception as e:  # noqa: PERF203
            last_err = e
    if df is None:
        raise RuntimeError(f"CSV 읽기 실패: {last_err}")

    # 파일마다 컬럼명이 다를 수 있어 후보 키를 넓게 잡음
    name_col = next((c for c in ["회사명", "업체명", "사업장명", "company_name"] if c in df.columns), None)
    addr_col = next((c for c in ["공장주소", "소재지", "주소", "소재지주소", "address"] if c in df.columns), None)
    ind_col = next((c for c in ["업종", "업종명", "생산품", "생산품명", "industry"] if c in df.columns), None)
    danji_col = next((c for c in ["단지명", "산업단지명"] if c in df.columns), None)

    if not name_col or not addr_col:
        raise RuntimeError(
            f"필수 컬럼(회사명/공장주소)을 찾지 못했습니다. 현재 컬럼: {list(df.columns)}"
        )

    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        name = str(r.get(name_col, "")).strip()
        addr = str(r.get(addr_col, "")).strip()
        if not name or not addr or name == "nan" or addr == "nan":
            continue
        if sido and sido not in addr:
            continue
        if sigungu and sigungu not in addr:
            continue
        industry = str(r.get(ind_col, "")).strip() if ind_col else ""
        danji = str(r.get(danji_col, "")).strip() if danji_col else ""
        rows.append(
            {
                "companyNm": name,
                "indutyNm": industry if industry else "미분류",
                "addrRoad": addr,
                "irsttNm": danji,
            }
        )
    return rows


def fetch_factory_rows(sido: str, sigungu: str, limit: int) -> List[Dict[str, Any]]:
    api_key = env_value("KICOX_API_KEY")
    base_url = (
        env_value("KICOX_FACTORY_API_URL")
        or "https://apis.data.go.kr/B550624/fctryRegistInfo"
    )
    endpoint = env_value("KICOX_FACTORY_API_ENDPOINT") or "getFctryListInIrsttService_v2"
    if not api_key:
        raise RuntimeError("KICOX_API_KEY 또는 KICOX_FACTORY_API_URL이 .env에 없습니다.")

    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    params = {
        "serviceKey": api_key,
        "type": "json",
        "pageNo": 1,
        "numOfRows": max(limit, 10),
        # API 문서별 지역 파라미터 키가 다를 수 있어 후보를 함께 전달
        "sido": sido,
        "sigungu": sigungu,
        "ctpvNm": sido,
        "sggNm": sigungu,
        "sigunguNm": sigungu,
        # 일부 엔드포인트는 산업단지명 기반 조회라서 시군구명을 보조로 전달
        "irsttNm": sigungu,
    }
    res = requests.get(url, params=params, timeout=25)
    res.raise_for_status()
    payload = res.json()

    cur: Any = payload
    for key in ("response", "body", "items", "item"):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            break

    if isinstance(cur, dict):
        return [cur]
    if isinstance(cur, list):
        return cur
    raise ValueError("공장 API 응답에서 items/item 리스트를 찾지 못했습니다.")


def geocode_kakao(address: str, kakao_rest_key: str) -> Optional[Tuple[float, float]]:
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {kakao_rest_key}"}
    params = {"query": address}
    res = requests.get(url, headers=headers, params=params, timeout=15)
    res.raise_for_status()
    docs = res.json().get("documents", [])
    if not docs:
        return None
    first = docs[0]
    return float(first["y"]), float(first["x"])  # lat, lon


def normalize_factories(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _pick_first(row, ["companyNm", "corpNm", "entrprsNm", "cmpnyNm", "업체명", "회사명", "사업장명"])
        ind = _pick_first(row, ["industry", "induty", "indutyNm", "업종", "업종명", "업태", "업태명"]) or "미분류"
        addr = _pick_first(row, ["address", "addr", "adres", "addrRoad", "addrJibun", "소재지", "소재지주소", "도로명주소", "지번주소"])
        if not name or not addr:
            continue
        key = (name, addr)
        if key in seen:
            continue
        seen.add(key)
        out.append({"factory_name": name, "industry": ind, "address": addr})
        if len(out) >= limit:
            break
    return out


def fetch_openweather(lat: float, lon: float, openweather_key: str) -> Dict[str, Any]:
    # 1) One Call 3.0 우선 시도
    onecall_url = "https://api.openweathermap.org/data/3.0/onecall"
    onecall_params = {
        "lat": lat,
        "lon": lon,
        "exclude": "minutely,daily,alerts",
        "units": "metric",
        "appid": openweather_key,
    }
    onecall_res = requests.get(onecall_url, params=onecall_params, timeout=20)
    if onecall_res.ok:
        return {"mode": "onecall", "current": onecall_res.json().get("current", {}), "hourly": onecall_res.json().get("hourly", [])}

    # 2) 실패 시 2.5 weather + forecast 폴백
    weather_url = "https://api.openweathermap.org/data/2.5/weather"
    forecast_url = "https://api.openweathermap.org/data/2.5/forecast"
    common_params = {"lat": lat, "lon": lon, "units": "metric", "appid": openweather_key}

    weather_res = requests.get(weather_url, params=common_params, timeout=20)
    forecast_res = requests.get(forecast_url, params=common_params, timeout=20)
    weather_res.raise_for_status()
    forecast_res.raise_for_status()

    current_src = weather_res.json()
    forecast_src = forecast_res.json()

    current = {
        "dt": current_src.get("dt"),
        "temp": (current_src.get("main") or {}).get("temp", 0),
        "humidity": (current_src.get("main") or {}).get("humidity", 0),
        "wind_speed": (current_src.get("wind") or {}).get("speed", 0),
    }
    hourly: List[Dict[str, Any]] = []
    for item in (forecast_src.get("list") or [])[:8]:
        rain_3h = float(((item.get("rain") or {}).get("3h", 0)) or 0)
        hourly.append(
            {
                "dt": item.get("dt"),
                "temp": (item.get("main") or {}).get("temp", 0),
                "humidity": (item.get("main") or {}).get("humidity", 0),
                "wind_speed": (item.get("wind") or {}).get("speed", 0),
                # 3시간 누적을 시간당 평균으로 환산
                "rain": {"1h": rain_3h / 3.0},
            }
        )
    return {"mode": "forecast_2_5_fallback", "current": current, "hourly": hourly}


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
    return {"green": "#2ecc71", "yellow": "#f1c40f", "orange": "#e67e22", "red": "#e74c3c"}[level]


def build_weather_rows(factory: Factory, weather_payload: Dict[str, Any]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    current = weather_payload.get("current", {})
    now_ts = datetime.fromtimestamp(current.get("dt", int(datetime.now().timestamp())))
    rows.append(
        {
            "factory_id": factory.factory_id,
            "timestamp": now_ts,
            "temp_c": float(current.get("temp", 0)),
            "humidity_pct": float(current.get("humidity", 0)),
            "rain_mm_h": 0.0,
            "wind_m_s": float(current.get("wind_speed", 0)),
        }
    )
    for h in weather_payload.get("hourly", [])[:24]:
        rain_val = 0.0
        rain = h.get("rain")
        if isinstance(rain, dict):
            rain_val = float(rain.get("1h", 0) or 0)
        elif isinstance(rain, (int, float)):
            rain_val = float(rain)
        rows.append(
            {
                "factory_id": factory.factory_id,
                "timestamp": datetime.fromtimestamp(int(h.get("dt", 0))),
                "temp_c": float(h.get("temp", 0)),
                "humidity_pct": float(h.get("humidity", 0)),
                "rain_mm_h": rain_val,
                "wind_m_s": float(h.get("wind_speed", 0)),
            }
        )
    return pd.DataFrame(rows).drop_duplicates(subset=["factory_id", "timestamp"]).sort_values("timestamp")


def compute_risks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_values(["factory_id", "timestamp"])
    out["rain_6h_mm"] = (
        out.groupby("factory_id")["rain_mm_h"].rolling(window=6, min_periods=1).sum().reset_index(level=0, drop=True)
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
        0.35 * out["HeatRisk"] + 0.30 * out["FloodRisk"] + 0.20 * out["QualityRisk"] + 0.15 * out["LogisticsRisk"]
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
    summary = (
        f"현재 종합 위험도는 {row['Risk_Score']:.1f}점({row['Risk_Level']})이며 "
        f"주요 위험은 {top2[0][0]}({top2[0][1]:.1f}) / {top2[1][0]}({top2[1][1]:.1f})입니다."
    )
    causes = []
    if row["rain_6h_mm"] >= 20:
        causes.append(f"최근 6시간 누적 강수량 {row['rain_6h_mm']:.1f}mm")
    if row["temp_c"] >= 30:
        causes.append(f"고온 상태(기온 {row['temp_c']:.1f}C)")
    if row["humidity_pct"] >= 75:
        causes.append(f"고습 상태(습도 {row['humidity_pct']:.1f}%)")
    if row["wind_m_s"] >= 9:
        causes.append(f"강한 바람(풍속 {row['wind_m_s']:.1f}m/s)")
    if not causes:
        causes.append("현재 주요 변수는 임계치 이내이지만 단기 변동 모니터링 필요")
    actions = [
        "향후 6시간 예측을 기준으로 출하/하역 일정을 재점검하세요.",
        "배수로, 외부 적치물, 냉각 설비를 우선 점검하세요.",
        "orange 이상이면 현장 담당자 알림을 즉시 발송하세요.",
    ]
    return {"summary": summary, "causes": causes, "actions": actions}


@st.cache_data(ttl=1800, show_spinner=False)
def load_live_data(sido: str, sigungu: str, factory_limit: int) -> Tuple[pd.DataFrame, List[str]]:
    logs: List[str] = []
    local_csv = env_value("KICOX_LOCAL_CSV_PATH") or discover_local_factory_csv()
    kakao_key = env_value("KAKAO_REST_API_KEY")
    openweather_key = env_value("OPENWEATHER_API_KEY")
    if not kakao_key:
        raise RuntimeError("KAKAO_REST_API_KEY가 없습니다.")
    if not openweather_key:
        raise RuntimeError("OPENWEATHER_API_KEY가 없습니다.")

    if local_csv:
        raw_rows = fetch_factory_rows_from_csv(local_csv, sido=sido, sigungu=sigungu)
        logs.append(f"공장 데이터 소스: 로컬 CSV ({local_csv})")
    else:
        raw_rows = fetch_factory_rows(sido=sido, sigungu=sigungu, limit=factory_limit * 3)
        logs.append("공장 데이터 소스: 공공데이터 API")

    factory_rows = normalize_factories(raw_rows, limit=factory_limit * 2)
    logs.append(f"공장 원본 후보 {len(raw_rows)}건 / 정규화 {len(factory_rows)}건")

    factories: List[Factory] = []
    fid = 1
    for fr in factory_rows:
        geo = geocode_kakao(fr["address"], kakao_key)
        if not geo:
            continue
        factories.append(
            Factory(
                factory_id=fid,
                factory_name=fr["factory_name"],
                industry=fr["industry"],
                address=fr["address"],
                lat=geo[0],
                lon=geo[1],
            )
        )
        fid += 1
        if len(factories) >= factory_limit:
            break

    if not factories:
        raise RuntimeError("지오코딩 가능한 공장을 찾지 못했습니다. 주소 키/응답 포맷을 확인하세요.")
    logs.append(f"지오코딩 성공 {len(factories)}건")

    all_weather: List[pd.DataFrame] = []
    for f in factories:
        weather_payload = fetch_openweather(f.lat, f.lon, openweather_key)
        wdf = build_weather_rows(f, weather_payload)
        all_weather.append(wdf)
    weather_df = pd.concat(all_weather, ignore_index=True)
    risk_df = compute_risks(weather_df)
    meta_df = pd.DataFrame([f.__dict__ for f in factories])
    merged = risk_df.merge(meta_df, on="factory_id", how="left")
    logs.append(f"날씨 행 {len(weather_df)}건 / 위험 행 {len(merged)}건")
    return merged, logs


def render_map(lat_center: float, lon_center: float, latest_rows: pd.DataFrame) -> None:
    m = folium.Map(location=[lat_center, lon_center], zoom_start=11, tiles="cartodbpositron")
    for _, r in latest_rows.iterrows():
        popup_html = (
            f"<b>{r['factory_name']}</b><br/>"
            f"업종: {r['industry']}<br/>"
            f"주소: {r['address']}<br/>"
            f"위험도: {r['Risk_Score']:.1f} ({r['Risk_Level']})"
        )
        folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=9,
            color=level_color(r["Risk_Level"]),
            fill=True,
            fill_opacity=0.9,
            popup=popup_html,
        ).add_to(m)
    st_folium(m, use_container_width=True, height=500)


def main() -> None:
    st.set_page_config(page_title="Live Factory Risk Demo", layout="wide")
    st.title("실데이터 기반 공장 기상 리스크 데모")
    st.caption("KICOX + Kakao Geocoding + OpenWeather 실시간 호출")

    with st.sidebar:
        st.subheader("조회 옵션")
        sido = st.text_input("시/도", value="경기도")
        sigungu = st.text_input("시/군/구", value="평택시")
        factory_limit = st.slider("공장 수", min_value=3, max_value=15, value=8, step=1)
        if st.button("실데이터 새로고침"):
            st.cache_data.clear()
        st.markdown("---")
        st.markdown("**필수 .env 키**")
        st.code(
            "KICOX_API_KEY=...\n"
            "KICOX_FACTORY_API_URL=https://apis.data.go.kr/B550624/fctryRegistInfo\n"
            "KICOX_FACTORY_API_ENDPOINT=getFctryListInIrsttService_v2\n"
            "KICOX_LOCAL_CSV_PATH=./한국산업단지공단_전국등록공장현황_등록공장현황자료_20241231.csv\n"
            "KAKAO_REST_API_KEY=...\n"
            "OPENWEATHER_API_KEY=...",
            language="bash",
        )

    try:
        with st.spinner("실데이터 수집 중... (수 초 ~ 수십 초)"):
            df, logs = load_live_data(sido=sido, sigungu=sigungu, factory_limit=factory_limit)
    except Exception as e:
        st.error(f"실데이터 로딩 실패: {e}")
        st.info("API 키/엔드포인트/요청 제한을 확인하세요.")
        return

    for line in logs:
        st.caption(f"- {line}")

    latest_ts = df["timestamp"].max()
    latest = df[df["timestamp"] == latest_ts].copy()
    center_lat = float(latest["lat"].mean())
    center_lon = float(latest["lon"].mean())

    left, right = st.columns([2.0, 1.1])
    with left:
        st.subheader(f"지도 (기준 시각: {latest_ts:%Y-%m-%d %H:%M})")
        render_map(center_lat, center_lon, latest)

    with right:
        factory_names = latest.sort_values("factory_name")["factory_name"].tolist()
        selected_name = st.selectbox("공장 선택", options=factory_names, index=0)
        selected_id = int(latest.loc[latest["factory_name"] == selected_name, "factory_id"].iloc[0])
        now_row = latest[latest["factory_id"] == selected_id].iloc[0]
        future = df[(df["factory_id"] == selected_id) & (df["timestamp"] > latest_ts)]
        score_6h = float(future.head(6)["Risk_Score"].max()) if not future.empty else float(now_row["Risk_Score"])
        score_24h = float(future.head(24)["Risk_Score"].max()) if not future.empty else float(now_row["Risk_Score"])

        st.metric("현재 위험도", f"{now_row['Risk_Score']:.1f}", now_row["Risk_Level"])
        st.metric("향후 6시간 위험도(최대)", f"{score_6h:.1f}")
        st.metric("향후 24시간 위험도(최대)", f"{score_24h:.1f}")
        st.write(f"업종: {now_row['industry']}")
        st.write(f"주소: {now_row['address']}")

        comment = agent_comment(now_row)
        st.subheader("에이전트 요약")
        st.write(comment["summary"])
        st.markdown("**주요 원인**")
        for x in comment["causes"]:
            st.write(f"- {x}")
        st.markdown("**대응 권고**")
        for x in comment["actions"]:
            st.write(f"- {x}")

    st.divider()
    st.subheader("선택 공장 위험 추이 (현재 + 향후 24시간)")
    trend = df[df["factory_id"] == selected_id][
        ["timestamp", "Risk_Score", "HeatRisk", "FloodRisk", "QualityRisk", "LogisticsRisk"]
    ]
    st.line_chart(trend.set_index("timestamp"))


if __name__ == "__main__":
    main()
