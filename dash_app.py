"""
TCS Risk Management Platform — Dash 버전
실행: python -m dash_app  또는  python dash_app.py
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from math import acos, asin, atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html
from dotenv import load_dotenv

# ── 환경변수 ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env", override=True)

MAPBOX_TOKEN      = os.getenv("MAPBOX_TOKEN", "")
MAP_STYLE         = "carto-positron"
VF_API_KEY        = os.getenv("VESSELFINIDER_API_KEY", "")
VF_HOST           = os.getenv("VESSELFINIDER_HOST", "ais-vessel-finder.p.rapidapi.com")
VF_POLL_INTERVAL  = int(os.getenv("VF_POLL_INTERVAL", "300"))  # 초, 기본 5분
MARINE_BUILD_TAG  = "MARINE_UI_FIX_4"

# ── VesselFinder 실시간 데이터 스토어 ─────────────────────────────────────────
_ais_store: Dict[str, Dict] = {}   # MMSI → vessel info
_ais_lock  = threading.Lock()
_ais_status: Dict[str, Any] = {
    "connected": False,
    "count": 0,
    "last_msg": None,
    "last_error": "",
    "last_poll_try": None,
}
_vf_thread_started = False
# MMSI 수동 조회 전에는 더미 선박만 표시
_mmsi_tracking_enabled = False


def _fetch_vessel_vf(mmsi: str) -> Optional[dict]:
    """VesselFinder REST API 단건 조회"""
    url = f"https://{VF_HOST}/getAisData?mmsi={mmsi}"
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": VF_API_KEY,
        "X-RapidAPI-Host": VF_HOST,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        _ais_status["last_error"] = f"{type(e).__name__}: {str(e)[:180]}"
        return None


def _poll_loop(mmsi_list: List[str]) -> None:
    """MMSI 목록을 주기적으로 폴링해 _ais_store 갱신"""
    # 더미 데이터 기반 MMSI 메타 조회 (스레드 시작 시 DUMMY_VESSELS가 이미 정의됨)
    mmsi_to_company = {v["mmsi"]: v.get("insured_company","기타") for v in DUMMY_VESSELS}
    mmsi_to_route   = {v["mmsi"]: v.get("route","")               for v in DUMMY_VESSELS}
    while True:
        _ais_status["last_poll_try"] = datetime.now().strftime("%H:%M:%S")
        success = 0
        for mmsi in mmsi_list:
            data = _fetch_vessel_vf(mmsi)
            if data and data.get("latitude") is not None:
                lat = float(data["latitude"])
                lon = float(data["longitude"])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    with _ais_lock:
                        _ais_store[mmsi] = {
                            "mmsi": mmsi,
                            "vessel_name": str(data.get("vesselName") or "").strip(),
                            "imo": str(data.get("imo") or "").strip(),
                            "vessel_type": str(data.get("vesselType") or "기타"),
                            "flag": str(data.get("flag") or ""),
                            "lat": round(lat, 5),
                            "lon": round(lon, 5),
                            "sog": float(data.get("speedKnots") or 0.0),
                            "last_fix": str(data.get("updatedAt") or "")[:16].replace("T", " "),
                            "area": str(data.get("area") or ""),
                            "status": str(data.get("status") or ""),
                            "insured_company": mmsi_to_company.get(mmsi, "기타"),
                            "route": mmsi_to_route.get(mmsi, ""),
                        }
                    success += 1
            time.sleep(0.5)  # rate limit 방지

        with _ais_lock:
            _ais_status["connected"] = success > 0
            _ais_status["count"] = len(_ais_store)
            _ais_status["last_msg"] = datetime.now().strftime("%H:%M:%S")
            if success == 0:
                _ais_status["last_error"] = "응답 선박 없음 (MMSI 미발견 또는 API 한도 초과)"

        time.sleep(VF_POLL_INTERVAL)


def _start_vf_thread(mmsi_list: List[str]) -> None:
    global _vf_thread_started
    if _vf_thread_started:
        return
    t = threading.Thread(target=_poll_loop, args=(mmsi_list,), daemon=True, name="vf-poll")
    t.start()
    _vf_thread_started = True


def get_live_vessels() -> pd.DataFrame:
    """유효 좌표가 있는 실시간 선박 DataFrame 반환"""
    with _ais_lock:
        rows = [v for v in _ais_store.values() if v["lat"] is not None and v["lon"] is not None]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["risk_level"], df["risk_color"] = zip(*[vessel_risk(float(r["sog"])) for _, r in df.iterrows()])
    return df

# ── 더미 데이터 ───────────────────────────────────────────────────────────────
# 보종 색상 매핑
LOB_COLORS = {
    "기업재산보험": "#2980b9",
    "화재보험":     "#e74c3c",
    "배상책임보험": "#8e44ad",
    "해상선박보험": "#27ae60",
    "해상적하보험": "#f39c12",
    "특종보험":     "#16a085",
}

DUMMY_FACTORIES = [
    {"factory_id": 1,  "factory_name": "현대제철 당진공장",      "industry": "철강",     "region": "충남", "lat": 36.89, "lon": 126.62},
    {"factory_id": 2,  "factory_name": "LG화학 여수NCC",         "industry": "석유화학", "region": "전남", "lat": 34.76, "lon": 127.66},
    {"factory_id": 3,  "factory_name": "삼성SDI 천안공장",       "industry": "전자",     "region": "충남", "lat": 36.81, "lon": 127.11},
    {"factory_id": 4,  "factory_name": "한화솔루션 울산공장",    "industry": "화학",     "region": "울산", "lat": 35.54, "lon": 129.31},
    {"factory_id": 5,  "factory_name": "포스코 광양제철소",      "industry": "철강",     "region": "전남", "lat": 34.93, "lon": 127.70},
    {"factory_id": 6,  "factory_name": "SK이노베이션 울산CLX",   "industry": "정유",     "region": "울산", "lat": 35.49, "lon": 129.37},
    {"factory_id": 7,  "factory_name": "롯데케미칼 대산공장",    "industry": "석유화학", "region": "충남", "lat": 36.97, "lon": 126.35},
    {"factory_id": 8,  "factory_name": "OCI 군산공장",           "industry": "화학",     "region": "전북", "lat": 35.98, "lon": 126.71},
    {"factory_id": 9,  "factory_name": "두산에너빌리티 창원",    "industry": "기계",     "region": "경남", "lat": 35.24, "lon": 128.68},
    {"factory_id": 10, "factory_name": "효성첨단소재 전주공장",  "industry": "섬유",     "region": "전북", "lat": 35.82, "lon": 127.15},
    {"factory_id": 11, "factory_name": "삼성전자 평택캠퍼스",    "industry": "반도체",   "region": "경기", "lat": 36.99, "lon": 127.11},
    {"factory_id": 12, "factory_name": "현대자동차 아산공장",    "industry": "자동차",   "region": "충남", "lat": 36.79, "lon": 127.00},
    {"factory_id": 13, "factory_name": "SK하이닉스 이천",        "industry": "반도체",   "region": "경기", "lat": 37.27, "lon": 127.44},
    {"factory_id": 14, "factory_name": "LG에너지솔루션 오창",    "industry": "배터리",   "region": "충북", "lat": 36.67, "lon": 127.43},
    {"factory_id": 15, "factory_name": "코오롱인더 구미공장",    "industry": "섬유",     "region": "경북", "lat": 36.11, "lon": 128.34},
    {"factory_id": 16, "factory_name": "한국타이어 대전공장",    "industry": "고무",     "region": "대전", "lat": 36.35, "lon": 127.38},
    {"factory_id": 17, "factory_name": "GS칼텍스 여수공장",      "industry": "정유",     "region": "전남", "lat": 34.72, "lon": 127.74},
    {"factory_id": 18, "factory_name": "현대제철 포항공장",      "industry": "철강",     "region": "경북", "lat": 35.98, "lon": 129.36},
    {"factory_id": 19, "factory_name": "금호석유화학 여수",      "industry": "석유화학", "region": "전남", "lat": 34.81, "lon": 127.76},
    {"factory_id": 20, "factory_name": "LS전선 안양공장",        "industry": "전선",     "region": "경기", "lat": 37.39, "lon": 126.92},
    {"factory_id": 21, "factory_name": "동국제강 부산공장",      "industry": "철강",     "region": "부산", "lat": 35.09, "lon": 128.97},
    {"factory_id": 22, "factory_name": "KCC 군포공장",           "industry": "화학",     "region": "경기", "lat": 37.36, "lon": 126.93},
    {"factory_id": 23, "factory_name": "포스코인터 인천창고",    "industry": "물류",     "region": "인천", "lat": 37.45, "lon": 126.71},
    {"factory_id": 24, "factory_name": "한화에어로스페이스 창원","industry": "방산",     "region": "경남", "lat": 35.21, "lon": 128.58},
    {"factory_id": 25, "factory_name": "롯데케미칼 울산공장",    "industry": "석유화학", "region": "울산", "lat": 35.51, "lon": 129.41},
]

DUMMY_CONTRACTS_FACTORY = [
    # 기업재산보험
    {"factory_id":1, "contract_id":"TCS-2026-PR-00101","lob":"기업재산보험","product_name":"종합재산보험(All Risk)","insured_amount_krw":48_000_000_000,"deductible_krw":150_000_000,"expiry":"2027-03-31","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    {"factory_id":3, "contract_id":"TCS-2026-PR-00103","lob":"기업재산보험","product_name":"종합재산보험(All Risk)","insured_amount_krw":35_000_000_000,"deductible_krw":100_000_000,"expiry":"2027-01-31","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":False},
    {"factory_id":4, "contract_id":"TCS-2026-PR-00104","lob":"기업재산보험","product_name":"종합재산보험+BI","insured_amount_krw":29_000_000_000,"deductible_krw":80_000_000,"expiry":"2026-12-31","cover_typhoon":True,"cover_flood":False,"cover_wind":True,"cover_bi":True},
    {"factory_id":7, "contract_id":"TCS-2026-PR-00107","lob":"기업재산보험","product_name":"종합재산보험(All Risk)","insured_amount_krw":41_000_000_000,"deductible_krw":120_000_000,"expiry":"2027-02-28","cover_typhoon":True,"cover_flood":True,"cover_wind":False,"cover_bi":False},
    {"factory_id":11,"contract_id":"TCS-2026-PR-00111","lob":"기업재산보험","product_name":"종합재산보험+BI","insured_amount_krw":120_000_000_000,"deductible_krw":500_000_000,"expiry":"2027-06-30","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    {"factory_id":13,"contract_id":"TCS-2026-PR-00113","lob":"기업재산보험","product_name":"종합재산보험(All Risk)","insured_amount_krw":95_000_000_000,"deductible_krw":350_000_000,"expiry":"2027-04-30","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    {"factory_id":14,"contract_id":"TCS-2026-PR-00114","lob":"기업재산보험","product_name":"종합재산+물류지연","insured_amount_krw":55_000_000_000,"deductible_krw":180_000_000,"expiry":"2026-11-30","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    {"factory_id":20,"contract_id":"TCS-2026-PR-00120","lob":"기업재산보험","product_name":"종합재산보험(All Risk)","insured_amount_krw":28_000_000_000,"deductible_krw":90_000_000,"expiry":"2027-05-31","cover_typhoon":True,"cover_flood":False,"cover_wind":True,"cover_bi":False},
    {"factory_id":24,"contract_id":"TCS-2026-PR-00124","lob":"기업재산보험","product_name":"종합재산+BI 특약","insured_amount_krw":67_000_000_000,"deductible_krw":200_000_000,"expiry":"2027-03-31","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    # 화재보험
    {"factory_id":2, "contract_id":"TCS-2026-FI-00201","lob":"화재보험","product_name":"화재/폭발 패키지","insured_amount_krw":62_000_000_000,"deductible_krw":200_000_000,"expiry":"2026-09-30","cover_typhoon":True,"cover_flood":False,"cover_wind":True,"cover_bi":True},
    {"factory_id":5, "contract_id":"TCS-2026-FI-00205","lob":"화재보험","product_name":"화재/침수 패키지","insured_amount_krw":75_000_000_000,"deductible_krw":250_000_000,"expiry":"2027-01-31","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":True},
    {"factory_id":8, "contract_id":"TCS-2026-FI-00208","lob":"화재보험","product_name":"화재/폭발 패키지","insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000,"expiry":"2026-10-31","cover_typhoon":True,"cover_flood":False,"cover_wind":True,"cover_bi":True},
    {"factory_id":17,"contract_id":"TCS-2026-FI-00217","lob":"화재보험","product_name":"화재/폭발 패키지","insured_amount_krw":88_000_000_000,"deductible_krw":280_000_000,"expiry":"2027-02-28","cover_typhoon":True,"cover_flood":False,"cover_wind":True,"cover_bi":True},
    {"factory_id":19,"contract_id":"TCS-2026-FI-00219","lob":"화재보험","product_name":"화재/침수 패키지","insured_amount_krw":45_000_000_000,"deductible_krw":140_000_000,"expiry":"2026-12-31","cover_typhoon":True,"cover_flood":True,"cover_wind":False,"cover_bi":False},
    {"factory_id":25,"contract_id":"TCS-2026-FI-00225","lob":"화재보험","product_name":"화재보험 단독","insured_amount_krw":38_000_000_000,"deductible_krw":110_000_000,"expiry":"2027-04-30","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    # 배상책임보험
    {"factory_id":6, "contract_id":"TCS-2026-LI-00306","lob":"배상책임보험","product_name":"생산물배상책임","insured_amount_krw":10_000_000_000,"deductible_krw":30_000_000,"expiry":"2026-12-31","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    {"factory_id":9, "contract_id":"TCS-2026-LI-00309","lob":"배상책임보험","product_name":"시설소유자배상책임","insured_amount_krw":5_000_000_000,"deductible_krw":10_000_000,"expiry":"2027-03-31","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    {"factory_id":12,"contract_id":"TCS-2026-LI-00312","lob":"배상책임보험","product_name":"생산물배상책임","insured_amount_krw":15_000_000_000,"deductible_krw":50_000_000,"expiry":"2027-06-30","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    {"factory_id":15,"contract_id":"TCS-2026-LI-00315","lob":"배상책임보험","product_name":"임원배상책임(D&O)","insured_amount_krw":8_000_000_000,"deductible_krw":20_000_000,"expiry":"2026-11-30","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    {"factory_id":22,"contract_id":"TCS-2026-LI-00322","lob":"배상책임보험","product_name":"환경배상책임","insured_amount_krw":12_000_000_000,"deductible_krw":40_000_000,"expiry":"2027-01-31","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    # 특종보험
    {"factory_id":10,"contract_id":"TCS-2026-SP-00410","lob":"특종보험","product_name":"기계보험","insured_amount_krw":22_000_000_000,"deductible_krw":70_000_000,"expiry":"2026-12-31","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":True},
    {"factory_id":16,"contract_id":"TCS-2026-SP-00416","lob":"특종보험","product_name":"기계보험+BI","insured_amount_krw":19_000_000_000,"deductible_krw":60_000_000,"expiry":"2027-05-31","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":True},
    {"factory_id":18,"contract_id":"TCS-2026-SP-00418","lob":"특종보험","product_name":"건설공사보험","insured_amount_krw":34_000_000_000,"deductible_krw":100_000_000,"expiry":"2026-08-31","cover_typhoon":True,"cover_flood":True,"cover_wind":True,"cover_bi":False},
    {"factory_id":21,"contract_id":"TCS-2026-SP-00421","lob":"특종보험","product_name":"기계보험","insured_amount_krw":16_000_000_000,"deductible_krw":55_000_000,"expiry":"2027-02-28","cover_typhoon":False,"cover_flood":False,"cover_wind":False,"cover_bi":False},
    {"factory_id":23,"contract_id":"TCS-2026-SP-00423","lob":"특종보험","product_name":"창고보험","insured_amount_krw":9_000_000_000,"deductible_krw":25_000_000,"expiry":"2027-03-31","cover_typhoon":True,"cover_flood":True,"cover_wind":False,"cover_bi":False},
]

COMPANY_COLORS = {
    "현대글로비스": "#2980b9",
    "기아":         "#e74c3c",
    "POSCO":        "#f39c12",
    "SK이노베이션": "#8e44ad",
}
COMPANIES = ["전체", "현대글로비스", "기아", "POSCO", "SK이노베이션"]

# ── 주요 항구 좌표 ─────────────────────────────────────────────────────────────
PORT_COORDS: Dict[str, Tuple[float, float]] = {
    # 한국
    "부산":     (35.10, 129.05), "인천":     (37.45, 126.70),
    "평택":     (37.00, 126.85), "광양":     (34.93, 127.70),
    "포항":     (36.00, 129.38), "울산":     (35.49, 129.39),
    "여수":     (34.74, 127.74), "대산":     (36.97, 126.35),
    # 일본
    "요코하마": (35.45, 139.65), "도쿄":     (35.65, 139.75),
    "나고야":   (35.00, 136.90), "오사카":   (34.65, 135.20),
    # 중국·동남아
    "상하이":   (31.23, 121.47), "칭다오":   (36.07, 120.38),
    "싱가포르": ( 1.35, 103.82), "필리핀":   (14.60, 120.98),
    "베트남":   (10.78, 106.70),
    # 러시아
    "블라디보스토크": (43.10, 131.90), "러시아": (43.10, 131.90),
    # 북미
    "LA":       (33.75,-118.25), "밴쿠버":   (49.25,-123.10),
    "시애틀":   (47.60,-122.35), "샌프란시스코": (37.75,-122.50),
    "멕시코":   (19.05,-104.32),
    # 유럽
    "로테르담": (51.90,   4.50), "함부르크": (53.55,   9.99),
    "르아브르": (49.50,   0.12),
    # 원자재 원산지
    "브라질":   (-20.27, -40.22), "호주":    (-20.30, 118.57),
    "인도":     ( 17.70,  83.30), "캐나다":  ( 49.25,-123.10),
    "중동":     ( 26.70,  50.10), "멕시코만": (29.75, -95.00),
    "아프리카": (-29.87,  31.05),
}


def _dest_coords(route: str) -> Optional[Tuple[float, float]]:
    """'출발→도착' 형식에서 목적지 항구 좌표 반환"""
    if not route or "→" not in route:
        return None
    dest = route.split("→")[-1].strip()
    return PORT_COORDS.get(dest)


def _origin_coords(route: str) -> Optional[Tuple[float, float]]:
    """'출발→도착' 형식에서 출발지 항구 좌표 반환"""
    if not route or "→" not in route:
        return None
    origin = route.split("→")[0].strip()
    return PORT_COORDS.get(origin)


def _great_circle_path(lat1: float, lon1: float, lat2: float, lon2: float, n: int = 60) -> List[Tuple[float, float]]:
    """두 지점 사이의 대권항로(great circle) 중간 좌표 n+1개 반환."""
    def to_xyz(la: float, lo: float) -> Tuple[float, float, float]:
        la, lo = radians(la), radians(lo)
        return cos(la) * cos(lo), cos(la) * sin(lo), sin(la)

    def to_latlon(x: float, y: float, z: float) -> Tuple[float, float]:
        return degrees(asin(max(-1.0, min(1.0, z)))), degrees(atan2(y, x))

    p1 = to_xyz(lat1, lon1)
    p2 = to_xyz(lat2, lon2)
    dot = max(-1.0, min(1.0, p1[0]*p2[0] + p1[1]*p2[1] + p1[2]*p2[2]))
    omega = acos(dot)
    if omega < 1e-9:
        return [(lat1, lon1)] * (n + 1)
    so = sin(omega)
    pts = []
    for i in range(n + 1):
        t = i / n
        s1 = sin((1 - t) * omega) / so
        s2 = sin(t * omega) / so
        pts.append(to_latlon(
            s1*p1[0] + s2*p2[0],
            s1*p1[1] + s2*p2[1],
            s1*p1[2] + s2*p2[2],
        ))
    return pts


def _route_arc_dashed(
    o_lat: float, o_lon: float,
    d_lat: float, d_lon: float,
    n: int = 60, dash_on: int = 5, dash_off: int = 3,
) -> Tuple[List, List]:
    """
    대권항로 곡선을 점선으로 표현하기 위해 None 구분자로 분절된 lats, lons 반환.
    anti-meridian(날짜변경선) 보정 포함.
    """
    pts = _great_circle_path(o_lat, o_lon, d_lat, d_lon, n)

    # 경도 연속성 보장 (180/-180 경계 점프 제거)
    raw_lons = [p[1] for p in pts]
    cont_lons: List[float] = [raw_lons[0]]
    for lo in raw_lons[1:]:
        diff = lo - cont_lons[-1]
        if diff > 180:
            cont_lons.append(lo - 360)
        elif diff < -180:
            cont_lons.append(lo + 360)
        else:
            cont_lons.append(lo)

    # 태평양 중심 지도(center=172) 기준으로 전체 경로를 +360 이동
    # — 북미 출발(lon < -90) 또는 경로가 음수 영역으로 넘어간 경우
    if cont_lons[0] < -90 or cont_lons[-1] < -90:
        cont_lons = [lo + 360 for lo in cont_lons]

    lats_raw = [p[0] for p in pts]

    # 점선: dash_on 개 포인트 그린 후 None 삽입, dash_off 개 건너뜀
    out_lats: List = []
    out_lons: List = []
    i = 0
    total = len(pts)
    drawing = True
    while i < total:
        seg = dash_on if drawing else dash_off
        if drawing:
            out_lats.extend(lats_raw[i:i+seg])
            out_lons.extend(cont_lons[i:i+seg])
            out_lats.append(None)
            out_lons.append(None)
        i += seg
        drawing = not drawing

    return out_lats, out_lons


DUMMY_VESSELS = [
    # ── 현대글로비스 20척 — 부산 출항 → 태평양 대권항로 → LA·밴쿠버·시애틀 ────
    {"mmsi":"440432000","imo":"9806079","vessel_name":"HMM ALGECIRAS",    "vessel_type":"컨테이너선","flag":"KR","gt":228000,"lat":34.8, "lon":131.2, "sog":17.1,"last_fix":"2026-04-19 09:25","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440630000","imo":"9674726","vessel_name":"HMM GAON",         "vessel_type":"컨테이너선","flag":"KR","gt":228000,"lat":37.5, "lon":141.8, "sog":14.2,"last_fix":"2026-04-19 09:22","route":"부산→밴쿠버",       "insured_company":"현대글로비스"},
    {"mmsi":"440148000","imo":"9312345","vessel_name":"HYUNDAI PRIVILEGE", "vessel_type":"컨테이너선","flag":"KR","gt":65000, "lat":41.5, "lon":154.0, "sog":15.8,"last_fix":"2026-04-19 09:35","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440560000","imo":"9425678","vessel_name":"HYUNDAI COURAGE",   "vessel_type":"컨테이너선","flag":"KR","gt":82000, "lat":44.2, "lon":167.0, "sog":13.8,"last_fix":"2026-04-19 09:30","route":"부산→밴쿠버",       "insured_company":"현대글로비스"},
    {"mmsi":"440480000","imo":"9771122","vessel_name":"HYUNDAI SMART",     "vessel_type":"컨테이너선","flag":"KR","gt":75000, "lat":47.0, "lon":178.5, "sog":15.5,"last_fix":"2026-04-19 09:18","route":"부산→시애틀",       "insured_company":"현대글로비스"},
    {"mmsi":"440350000","imo":"9882201","vessel_name":"HYUNDAI PLUS",      "vessel_type":"컨테이너선","flag":"KR","gt":90000, "lat":46.3, "lon":-165.2,"sog":12.0,"last_fix":"2026-04-19 09:40","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440281000","imo":"9334411","vessel_name":"HYUNDAI DREAM",     "vessel_type":"컨테이너선","flag":"KR","gt":76000, "lat":43.8, "lon":-150.5,"sog":14.5,"last_fix":"2026-04-19 09:45","route":"부산→밴쿠버",       "insured_company":"현대글로비스"},
    {"mmsi":"440390000","imo":"9055666","vessel_name":"HYUNDAI PIONEER",   "vessel_type":"컨테이너선","flag":"KR","gt":51000, "lat":39.4, "lon":-136.8,"sog":13.2,"last_fix":"2026-04-19 09:36","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440520000","imo":"9660112","vessel_name":"HMM ROTTERDAM",     "vessel_type":"컨테이너선","flag":"KR","gt":240000,"lat":34.2, "lon":-121.5,"sog":16.8,"last_fix":"2026-04-19 09:20","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440610000","imo":"9441023","vessel_name":"HMM LE HAVRE",      "vessel_type":"컨테이너선","flag":"KR","gt":240000,"lat":48.5, "lon":-125.8,"sog":18.2,"last_fix":"2026-04-19 09:10","route":"부산→밴쿠버",       "insured_company":"현대글로비스"},
    {"mmsi":"440432100","imo":"9806100","vessel_name":"HMM AMSTERDAM",    "vessel_type":"컨테이너선","flag":"KR","gt":190000,"lat":33.5, "lon":134.0, "sog":16.5,"last_fix":"2026-04-19 08:30","route":"부산→로테르담",     "insured_company":"현대글로비스"},
    {"mmsi":"440432101","imo":"9806101","vessel_name":"HMM HAMBURG",      "vessel_type":"컨테이너선","flag":"KR","gt":185000,"lat":40.0, "lon":148.0, "sog":15.8,"last_fix":"2026-04-19 08:25","route":"부산→함부르크",     "insured_company":"현대글로비스"},
    {"mmsi":"440432102","imo":"9806102","vessel_name":"HMM COPENHAGEN",   "vessel_type":"컨테이너선","flag":"KR","gt":195000,"lat":44.8, "lon":160.5, "sog":17.2,"last_fix":"2026-04-19 08:20","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440432103","imo":"9806103","vessel_name":"HMM OSLO",         "vessel_type":"컨테이너선","flag":"KR","gt":200000,"lat":47.5, "lon":173.0, "sog":16.0,"last_fix":"2026-04-19 08:15","route":"부산→시애틀",       "insured_company":"현대글로비스"},
    {"mmsi":"440432104","imo":"9806104","vessel_name":"HMM HELSINKI",     "vessel_type":"컨테이너선","flag":"KR","gt":188000,"lat":46.0, "lon":-176.5,"sog":15.5,"last_fix":"2026-04-19 08:10","route":"부산→밴쿠버",       "insured_company":"현대글로비스"},
    {"mmsi":"440432105","imo":"9806105","vessel_name":"HMM STOCKHOLM",    "vessel_type":"컨테이너선","flag":"KR","gt":192000,"lat":42.5, "lon":-161.0,"sog":14.8,"last_fix":"2026-04-19 08:05","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440432106","imo":"9806106","vessel_name":"HMM BUSAN",        "vessel_type":"컨테이너선","flag":"KR","gt":175000,"lat":36.0, "lon":-145.5,"sog":16.2,"last_fix":"2026-04-19 08:00","route":"부산→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440432107","imo":"9806107","vessel_name":"HMM INCHEON",      "vessel_type":"컨테이너선","flag":"KR","gt":168000,"lat":31.0, "lon":-131.0,"sog":15.9,"last_fix":"2026-04-19 07:55","route":"인천→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440432108","imo":"9806108","vessel_name":"HMM GWANGYANG",    "vessel_type":"컨테이너선","flag":"KR","gt":180000,"lat":28.5, "lon":-117.0,"sog":13.5,"last_fix":"2026-04-19 07:50","route":"광양→LA",           "insured_company":"현대글로비스"},
    {"mmsi":"440432109","imo":"9806109","vessel_name":"HMM ULSAN",        "vessel_type":"컨테이너선","flag":"KR","gt":172000,"lat":47.2, "lon":-122.5,"sog":11.0,"last_fix":"2026-04-19 07:45","route":"울산→시애틀",       "insured_company":"현대글로비스"},
    # ── 기아 20척 — 평택·울산 출항 → 태평양 → 북미 ───────────────────────────
    {"mmsi":"441100001","imo":"9501001","vessel_name":"KIA ATLANTIC",     "vessel_type":"자동차운반선","flag":"KR","gt":60000, "lat":33.2, "lon":129.8, "sog":14.5,"last_fix":"2026-04-19 08:55","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100002","imo":"9501002","vessel_name":"KIA PACIFIC",      "vessel_type":"자동차운반선","flag":"KR","gt":60000, "lat":36.8, "lon":140.5, "sog":15.2,"last_fix":"2026-04-19 09:00","route":"울산→밴쿠버",       "insured_company":"기아"},
    {"mmsi":"441100003","imo":"9501003","vessel_name":"KIA EUROPE",       "vessel_type":"자동차운반선","flag":"KR","gt":55000, "lat":40.5, "lon":152.8, "sog":13.8,"last_fix":"2026-04-19 09:05","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100004","imo":"9501004","vessel_name":"KIA AUSTRALIA",    "vessel_type":"자동차운반선","flag":"KR","gt":55000, "lat":44.0, "lon":164.5, "sog":12.0,"last_fix":"2026-04-19 09:10","route":"울산→밴쿠버",       "insured_company":"기아"},
    {"mmsi":"441100005","imo":"9501005","vessel_name":"KIA ASIA",         "vessel_type":"자동차운반선","flag":"KR","gt":48000, "lat":46.8, "lon":177.0, "sog":11.5,"last_fix":"2026-04-19 09:15","route":"평택→시애틀",       "insured_company":"기아"},
    {"mmsi":"441100006","imo":"9501006","vessel_name":"KIA GLOBAL",       "vessel_type":"자동차운반선","flag":"KR","gt":65000, "lat":45.5, "lon":-170.8,"sog":10.8,"last_fix":"2026-04-19 09:20","route":"울산→LA",           "insured_company":"기아"},
    {"mmsi":"441100007","imo":"9501007","vessel_name":"KIA LEADER",       "vessel_type":"자동차운반선","flag":"KR","gt":60000, "lat":41.5, "lon":-156.5,"sog":13.2,"last_fix":"2026-04-19 09:25","route":"평택→밴쿠버",       "insured_company":"기아"},
    {"mmsi":"441100008","imo":"9501008","vessel_name":"KIA EXPLORER",     "vessel_type":"자동차운반선","flag":"KR","gt":58000, "lat":36.8, "lon":-142.3,"sog":14.9,"last_fix":"2026-04-19 09:30","route":"울산→LA",           "insured_company":"기아"},
    {"mmsi":"441100009","imo":"9501009","vessel_name":"KIA CHAMPION",     "vessel_type":"자동차운반선","flag":"KR","gt":52000, "lat":30.5, "lon":-128.0,"sog":16.1,"last_fix":"2026-04-19 09:35","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100010","imo":"9501010","vessel_name":"KIA GALAXY",       "vessel_type":"자동차운반선","flag":"KR","gt":63000, "lat":25.0, "lon":-113.5,"sog":12.7,"last_fix":"2026-04-19 09:40","route":"울산→멕시코",       "insured_company":"기아"},
    {"mmsi":"441100011","imo":"9502001","vessel_name":"KIA FRONTIER",     "vessel_type":"자동차운반선","flag":"KR","gt":58000, "lat":32.0, "lon":132.5, "sog":14.2,"last_fix":"2026-04-19 08:30","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100012","imo":"9502002","vessel_name":"KIA SPIRIT",       "vessel_type":"자동차운반선","flag":"KR","gt":62000, "lat":39.0, "lon":145.0, "sog":15.0,"last_fix":"2026-04-19 08:25","route":"울산→밴쿠버",       "insured_company":"기아"},
    {"mmsi":"441100013","imo":"9502003","vessel_name":"KIA HORIZON",      "vessel_type":"자동차운반선","flag":"KR","gt":55000, "lat":43.5, "lon":158.0, "sog":13.5,"last_fix":"2026-04-19 08:20","route":"평택→시애틀",       "insured_company":"기아"},
    {"mmsi":"441100014","imo":"9502004","vessel_name":"KIA VENTURE",      "vessel_type":"자동차운반선","flag":"KR","gt":60000, "lat":46.2, "lon":171.5, "sog":14.8,"last_fix":"2026-04-19 08:15","route":"울산→밴쿠버",       "insured_company":"기아"},
    {"mmsi":"441100015","imo":"9502005","vessel_name":"KIA RANGER",       "vessel_type":"자동차운반선","flag":"KR","gt":57000, "lat":46.5, "lon":-174.0,"sog":12.5,"last_fix":"2026-04-19 08:10","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100016","imo":"9502006","vessel_name":"KIA EAGLE",        "vessel_type":"자동차운반선","flag":"KR","gt":64000, "lat":44.0, "lon":-160.0,"sog":15.5,"last_fix":"2026-04-19 08:05","route":"울산→시애틀",       "insured_company":"기아"},
    {"mmsi":"441100017","imo":"9502007","vessel_name":"KIA FALCON",       "vessel_type":"자동차운반선","flag":"KR","gt":59000, "lat":39.5, "lon":-147.0,"sog":14.0,"last_fix":"2026-04-19 08:00","route":"평택→LA",           "insured_company":"기아"},
    {"mmsi":"441100018","imo":"9502008","vessel_name":"KIA PHOENIX",      "vessel_type":"자동차운반선","flag":"KR","gt":61000, "lat":33.5, "lon":-132.5,"sog":16.0,"last_fix":"2026-04-19 07:55","route":"울산→LA",           "insured_company":"기아"},
    {"mmsi":"441100019","imo":"9502009","vessel_name":"KIA CONDOR",       "vessel_type":"자동차운반선","flag":"KR","gt":56000, "lat":37.5, "lon":-122.0,"sog":10.5,"last_fix":"2026-04-19 07:50","route":"평택→샌프란시스코", "insured_company":"기아"},
    {"mmsi":"441100020","imo":"9502010","vessel_name":"KIA ALBATROSS",    "vessel_type":"자동차운반선","flag":"KR","gt":63000, "lat":27.0, "lon":-110.0,"sog":13.8,"last_fix":"2026-04-19 07:45","route":"울산→멕시코",       "insured_company":"기아"},
    # ── POSCO 15척 — 서태평양 광석 운반 루트 ─────────────────────────────────
    {"mmsi":"441200001","imo":"9601001","vessel_name":"POSCO BRAZIL",     "vessel_type":"벌크선","flag":"KR","gt":180000,"lat":31.5, "lon":127.5, "sog":11.0,"last_fix":"2026-04-19 09:00","route":"브라질→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200002","imo":"9601002","vessel_name":"POSCO AUSTRALIA",  "vessel_type":"벌크선","flag":"KR","gt":180000,"lat":23.5, "lon":132.0, "sog":12.5,"last_fix":"2026-04-19 09:05","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200003","imo":"9601003","vessel_name":"POSCO INDIA",      "vessel_type":"벌크선","flag":"KR","gt":150000,"lat":18.0, "lon":136.5, "sog":9.8, "last_fix":"2026-04-19 09:10","route":"인도→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200004","imo":"9601004","vessel_name":"POSCO CANADA",     "vessel_type":"벌크선","flag":"KR","gt":160000,"lat":37.8, "lon":148.5, "sog":13.3,"last_fix":"2026-04-19 09:15","route":"캐나다→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200005","imo":"9601005","vessel_name":"POSCO CHALLENGER", "vessel_type":"벌크선","flag":"KR","gt":170000,"lat":34.8, "lon":139.0, "sog":10.2,"last_fix":"2026-04-19 09:20","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200006","imo":"9602001","vessel_name":"POSCO PACIFIC",    "vessel_type":"벌크선","flag":"KR","gt":175000,"lat":28.0, "lon":141.0, "sog":11.5,"last_fix":"2026-04-19 08:30","route":"브라질→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200007","imo":"9602002","vessel_name":"POSCO GUAM",       "vessel_type":"벌크선","flag":"KR","gt":165000,"lat":13.0, "lon":144.5, "sog":12.0,"last_fix":"2026-04-19 08:25","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200008","imo":"9602003","vessel_name":"POSCO HOKKAIDO",   "vessel_type":"벌크선","flag":"KR","gt":170000,"lat":42.0, "lon":143.5, "sog":9.5, "last_fix":"2026-04-19 08:20","route":"캐나다→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200009","imo":"9602004","vessel_name":"POSCO PALAU",      "vessel_type":"벌크선","flag":"KR","gt":155000,"lat":8.0,  "lon":135.5, "sog":10.8,"last_fix":"2026-04-19 08:15","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200010","imo":"9602005","vessel_name":"POSCO NAGOYA",     "vessel_type":"벌크선","flag":"KR","gt":168000,"lat":33.0, "lon":137.0, "sog":8.5, "last_fix":"2026-04-19 08:10","route":"브라질→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200011","imo":"9602006","vessel_name":"POSCO OKINAWA",    "vessel_type":"벌크선","flag":"KR","gt":160000,"lat":26.5, "lon":128.5, "sog":11.2,"last_fix":"2026-04-19 08:05","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200012","imo":"9602007","vessel_name":"POSCO MIDPAC",     "vessel_type":"벌크선","flag":"KR","gt":172000,"lat":20.0, "lon":142.0, "sog":12.8,"last_fix":"2026-04-19 08:00","route":"캐나다→광양",           "insured_company":"POSCO"},
    {"mmsi":"441200013","imo":"9602008","vessel_name":"POSCO TOKARA",     "vessel_type":"벌크선","flag":"KR","gt":158000,"lat":38.5, "lon":153.0, "sog":10.5,"last_fix":"2026-04-19 07:55","route":"호주→포항",             "insured_company":"POSCO"},
    {"mmsi":"441200014","imo":"9602009","vessel_name":"POSCO SAKHALIN",   "vessel_type":"벌크선","flag":"KR","gt":162000,"lat":45.0, "lon":145.0, "sog":9.8, "last_fix":"2026-04-19 07:50","route":"러시아→포항",           "insured_company":"POSCO"},
    {"mmsi":"441200015","imo":"9602010","vessel_name":"POSCO MICRONESIA", "vessel_type":"벌크선","flag":"KR","gt":167000,"lat":15.0, "lon":148.0, "sog":11.8,"last_fix":"2026-04-19 07:45","route":"호주→광양",             "insured_company":"POSCO"},
    # ── SK이노베이션 15척 — 중동·멕시코만→한국 원유 루트 ──────────────────────
    {"mmsi":"441300001","imo":"9701001","vessel_name":"SK HARMONY",       "vessel_type":"탱커","flag":"KR","gt":300000,"lat":35.5, "lon":129.2, "sog":13.8,"last_fix":"2026-04-19 09:00","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300002","imo":"9701002","vessel_name":"SK INNOVATION",    "vessel_type":"탱커","flag":"KR","gt":300000,"lat":28.5, "lon":127.8, "sog":14.2,"last_fix":"2026-04-19 09:05","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300003","imo":"9701003","vessel_name":"SK FUTURE",        "vessel_type":"탱커","flag":"KR","gt":280000,"lat":22.0, "lon":124.5, "sog":12.5,"last_fix":"2026-04-19 09:10","route":"멕시코만→울산",           "insured_company":"SK이노베이션"},
    {"mmsi":"441300004","imo":"9701004","vessel_name":"SK PIONEER",       "vessel_type":"탱커","flag":"KR","gt":260000,"lat":14.5, "lon":127.8, "sog":11.0,"last_fix":"2026-04-19 09:15","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300005","imo":"9701005","vessel_name":"SK BRAVE",         "vessel_type":"탱커","flag":"KR","gt":290000,"lat":36.5, "lon":129.5, "sog":15.5,"last_fix":"2026-04-19 09:20","route":"중동→여수",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300006","imo":"9702001","vessel_name":"SK RESOLUTION",    "vessel_type":"탱커","flag":"KR","gt":295000,"lat":32.0, "lon":131.5, "sog":13.5,"last_fix":"2026-04-19 08:30","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300007","imo":"9702002","vessel_name":"SK PROSPERITY",    "vessel_type":"탱커","flag":"KR","gt":285000,"lat":25.5, "lon":131.0, "sog":14.0,"last_fix":"2026-04-19 08:25","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300008","imo":"9702003","vessel_name":"SK INTEGRITY",     "vessel_type":"탱커","flag":"KR","gt":275000,"lat":18.5, "lon":128.5, "sog":12.8,"last_fix":"2026-04-19 08:20","route":"중동→여수",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300009","imo":"9702004","vessel_name":"SK EXCELLENCE",    "vessel_type":"탱커","flag":"KR","gt":310000,"lat":12.0, "lon":130.5, "sog":13.2,"last_fix":"2026-04-19 08:15","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300010","imo":"9702005","vessel_name":"SK SUMMIT",        "vessel_type":"탱커","flag":"KR","gt":320000,"lat":8.0,  "lon":127.5, "sog":11.5,"last_fix":"2026-04-19 08:10","route":"중동→여수",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300011","imo":"9702006","vessel_name":"SK PRIME",         "vessel_type":"탱커","flag":"KR","gt":265000,"lat":34.5, "lon":128.0, "sog":10.8,"last_fix":"2026-04-19 08:05","route":"멕시코만→울산",           "insured_company":"SK이노베이션"},
    {"mmsi":"441300012","imo":"9702007","vessel_name":"SK OCEAN",         "vessel_type":"탱커","flag":"KR","gt":298000,"lat":30.0, "lon":127.2, "sog":13.0,"last_fix":"2026-04-19 08:00","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300013","imo":"9702008","vessel_name":"SK STAR",          "vessel_type":"탱커","flag":"KR","gt":288000,"lat":24.0, "lon":126.5, "sog":14.5,"last_fix":"2026-04-19 07:55","route":"중동→여수",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300014","imo":"9702009","vessel_name":"SK GALAXY",        "vessel_type":"탱커","flag":"KR","gt":302000,"lat":17.0, "lon":125.0, "sog":12.2,"last_fix":"2026-04-19 07:50","route":"중동→울산",               "insured_company":"SK이노베이션"},
    {"mmsi":"441300015","imo":"9702010","vessel_name":"SK UNIVERSE",      "vessel_type":"탱커","flag":"KR","gt":315000,"lat":38.0, "lon":130.5, "sog":9.5, "last_fix":"2026-04-19 07:45","route":"아프리카→울산",           "insured_company":"SK이노베이션"},
]

DUMMY_CONTRACTS_VESSEL = [
    # ── 현대글로비스 ───────────────────────────────────────────────────────────
    {"imo":"9806079","contract_id":"TCS-2026-HL-G001","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":65_000_000_000,"deductible_krw":200_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9674726","contract_id":"TCS-2026-HL-G002","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":65_000_000_000,"deductible_krw":200_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9312345","contract_id":"TCS-2026-HL-G003","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":20_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9312345","contract_id":"TCS-2026-CA-G001","lob":"해상적하보험","product_name":"적하보험 (Cargo)",   "insured_amount_krw":5_000_000_000, "deductible_krw":20_000_000, "cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9425678","contract_id":"TCS-2026-HL-G004","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":25_000_000_000,"deductible_krw":80_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9771122","contract_id":"TCS-2026-HL-G005","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":22_000_000_000,"deductible_krw":70_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9882201","contract_id":"TCS-2026-HL-G006","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":28_000_000_000,"deductible_krw":90_000_000, "cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9334411","contract_id":"TCS-2026-HL-G007","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":23_000_000_000,"deductible_krw":75_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9334411","contract_id":"TCS-2026-CA-G002","lob":"해상적하보험","product_name":"적하보험 (Cargo)",   "insured_amount_krw":7_000_000_000, "deductible_krw":25_000_000, "cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9055666","contract_id":"TCS-2026-HL-G008","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":15_000_000_000,"deductible_krw":50_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":False,"cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9660112","contract_id":"TCS-2026-HL-G009","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":70_000_000_000,"deductible_krw":220_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9441023","contract_id":"TCS-2026-HL-G010","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":70_000_000_000,"deductible_krw":220_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    # ── 기아 ──────────────────────────────────────────────────────────────────
    {"imo":"9501001","contract_id":"TCS-2026-HL-K001","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501002","contract_id":"TCS-2026-HL-K002","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501003","contract_id":"TCS-2026-HL-K003","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":16_000_000_000,"deductible_krw":55_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501004","contract_id":"TCS-2026-HL-K004","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":16_000_000_000,"deductible_krw":55_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501005","contract_id":"TCS-2026-HL-K005","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":14_000_000_000,"deductible_krw":50_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501006","contract_id":"TCS-2026-HL-K006","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":19_000_000_000,"deductible_krw":65_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9501007","contract_id":"TCS-2026-HL-K007","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501008","contract_id":"TCS-2026-HL-K008","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":17_000_000_000,"deductible_krw":58_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9501009","contract_id":"TCS-2026-HL-K009","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":15_000_000_000,"deductible_krw":52_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9501010","contract_id":"TCS-2026-HL-K010","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    # ── POSCO ─────────────────────────────────────────────────────────────────
    {"imo":"9601001","contract_id":"TCS-2026-HL-P001","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":45_000_000_000,"deductible_krw":140_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9601001","contract_id":"TCS-2026-CA-P001","lob":"해상적하보험","product_name":"철광석 적하보험",    "insured_amount_krw":12_000_000_000,"deductible_krw":40_000_000, "cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9601002","contract_id":"TCS-2026-HL-P002","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":45_000_000_000,"deductible_krw":140_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9601002","contract_id":"TCS-2026-CA-P002","lob":"해상적하보험","product_name":"철광석 적하보험",    "insured_amount_krw":12_000_000_000,"deductible_krw":40_000_000, "cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9601003","contract_id":"TCS-2026-HL-P003","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":38_000_000_000,"deductible_krw":120_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9601004","contract_id":"TCS-2026-HL-P004","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":40_000_000_000,"deductible_krw":125_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9601005","contract_id":"TCS-2026-HL-P005","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":42_000_000_000,"deductible_krw":130_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    # ── SK이노베이션 ───────────────────────────────────────────────────────────
    {"imo":"9701001","contract_id":"TCS-2026-HL-S001","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":85_000_000_000,"deductible_krw":250_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9701001","contract_id":"TCS-2026-CA-S001","lob":"해상적하보험","product_name":"원유 적하보험",      "insured_amount_krw":45_000_000_000,"deductible_krw":150_000_000,"cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9701002","contract_id":"TCS-2026-HL-S002","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":85_000_000_000,"deductible_krw":250_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9701002","contract_id":"TCS-2026-CA-S002","lob":"해상적하보험","product_name":"원유 적하보험",      "insured_amount_krw":45_000_000_000,"deductible_krw":150_000_000,"cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9701003","contract_id":"TCS-2026-HL-S003","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":80_000_000_000,"deductible_krw":240_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9701004","contract_id":"TCS-2026-HL-S004","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":75_000_000_000,"deductible_krw":230_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9701005","contract_id":"TCS-2026-HL-S005","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":82_000_000_000,"deductible_krw":245_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9701005","contract_id":"TCS-2026-CA-S003","lob":"해상적하보험","product_name":"원유 적하보험",      "insured_amount_krw":42_000_000_000,"deductible_krw":140_000_000,"cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"SK이노베이션"},
    # ── 현대글로비스 추가 10척 계약 ───────────────────────────────────────────
    {"imo":"9806100","contract_id":"TCS-2026-HL-G011","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":58_000_000_000,"deductible_krw":180_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9806101","contract_id":"TCS-2026-HL-G012","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":55_000_000_000,"deductible_krw":170_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9806102","contract_id":"TCS-2026-HL-G013","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":60_000_000_000,"deductible_krw":185_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9806103","contract_id":"TCS-2026-HL-G014","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":62_000_000_000,"deductible_krw":190_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9806104","contract_id":"TCS-2026-HL-G015","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":56_000_000_000,"deductible_krw":175_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9806105","contract_id":"TCS-2026-HL-G016","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":58_000_000_000,"deductible_krw":180_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9806106","contract_id":"TCS-2026-HL-G017","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":52_000_000_000,"deductible_krw":165_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9806107","contract_id":"TCS-2026-HL-G018","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":50_000_000_000,"deductible_krw":160_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    {"imo":"9806108","contract_id":"TCS-2026-HL-G019","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":54_000_000_000,"deductible_krw":168_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"현대글로비스"},
    {"imo":"9806109","contract_id":"TCS-2026-HL-G020","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":51_000_000_000,"deductible_krw":162_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"현대글로비스"},
    # ── 기아 추가 10척 계약 ───────────────────────────────────────────────────
    {"imo":"9502001","contract_id":"TCS-2026-HL-K011","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":17_000_000_000,"deductible_krw":57_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9502002","contract_id":"TCS-2026-HL-K012","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":18_500_000_000,"deductible_krw":62_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9502003","contract_id":"TCS-2026-HL-K013","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":16_500_000_000,"deductible_krw":56_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9502004","contract_id":"TCS-2026-HL-K014","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":18_000_000_000,"deductible_krw":60_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9502005","contract_id":"TCS-2026-HL-K015","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":17_200_000_000,"deductible_krw":58_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9502006","contract_id":"TCS-2026-HL-K016","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":19_000_000_000,"deductible_krw":64_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9502007","contract_id":"TCS-2026-HL-K017","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":17_800_000_000,"deductible_krw":59_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9502008","contract_id":"TCS-2026-HL-K018","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":18_300_000_000,"deductible_krw":61_000_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    {"imo":"9502009","contract_id":"TCS-2026-HL-K019","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":16_800_000_000,"deductible_krw":56_500_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"기아"},
    {"imo":"9502010","contract_id":"TCS-2026-HL-K020","lob":"해상선박보험","product_name":"선체보험+P&I",       "insured_amount_krw":19_200_000_000,"deductible_krw":64_500_000, "cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"기아"},
    # ── POSCO 추가 10척 계약 ──────────────────────────────────────────────────
    {"imo":"9602001","contract_id":"TCS-2026-HL-P006","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":44_000_000_000,"deductible_krw":138_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602002","contract_id":"TCS-2026-HL-P007","lob":"해상선박보험","product_name":"철광석 적하+선체",   "insured_amount_krw":40_000_000_000,"deductible_krw":125_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9602003","contract_id":"TCS-2026-HL-P008","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":42_000_000_000,"deductible_krw":132_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602004","contract_id":"TCS-2026-HL-P009","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":38_000_000_000,"deductible_krw":120_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602005","contract_id":"TCS-2026-HL-P010","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":41_000_000_000,"deductible_krw":128_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9602006","contract_id":"TCS-2026-HL-P011","lob":"해상선박보험","product_name":"철광석 적하+선체",   "insured_amount_krw":39_000_000_000,"deductible_krw":122_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602007","contract_id":"TCS-2026-HL-P012","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":43_000_000_000,"deductible_krw":135_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602008","contract_id":"TCS-2026-HL-P013","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":40_500_000_000,"deductible_krw":127_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":True, "insured_company":"POSCO"},
    {"imo":"9602009","contract_id":"TCS-2026-HL-P014","lob":"해상선박보험","product_name":"철광석 적하+선체",   "insured_amount_krw":37_000_000_000,"deductible_krw":115_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"POSCO"},
    {"imo":"9602010","contract_id":"TCS-2026-HL-P015","lob":"해상선박보험","product_name":"선체보험+기관",      "insured_amount_krw":41_500_000_000,"deductible_krw":130_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"POSCO"},
    # ── SK이노베이션 추가 10척 계약 ────────────────────────────────────────────
    {"imo":"9702001","contract_id":"TCS-2026-HL-S006","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":84_000_000_000,"deductible_krw":248_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9702001","contract_id":"TCS-2026-CA-S004","lob":"해상적하보험","product_name":"원유 적하보험",      "insured_amount_krw":44_000_000_000,"deductible_krw":145_000_000,"cover_hull":False,"cover_cargo":True, "cover_pi":False,"cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9702002","contract_id":"TCS-2026-HL-S007","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":82_000_000_000,"deductible_krw":244_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9702003","contract_id":"TCS-2026-HL-S008","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":78_000_000_000,"deductible_krw":235_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9702004","contract_id":"TCS-2026-HL-S009","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":86_000_000_000,"deductible_krw":252_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9702005","contract_id":"TCS-2026-HL-S010","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":79_000_000_000,"deductible_krw":237_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9702006","contract_id":"TCS-2026-HL-S011","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":83_000_000_000,"deductible_krw":246_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9702007","contract_id":"TCS-2026-HL-S012","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":80_000_000_000,"deductible_krw":240_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9702008","contract_id":"TCS-2026-HL-S013","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":85_000_000_000,"deductible_krw":250_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
    {"imo":"9702009","contract_id":"TCS-2026-HL-S014","lob":"해상선박보험","product_name":"선체보험 (H&M)",     "insured_amount_krw":76_000_000_000,"deductible_krw":230_000_000,"cover_hull":True,"cover_cargo":False,"cover_pi":True, "cover_delay":False,"insured_company":"SK이노베이션"},
    {"imo":"9702010","contract_id":"TCS-2026-HL-S015","lob":"해상선박보험","product_name":"선체보험+기관 (H&M)","insured_amount_krw":88_000_000_000,"deductible_krw":255_000_000,"cover_hull":True,"cover_cargo":True, "cover_pi":True, "cover_delay":True, "insured_company":"SK이노베이션"},
]

# ── 선박정보 DB (IMO No. / 선박명 검색용, Equasis 연동 예정) ─────────────────
# IMO No.(7자리) 키 기준. special_status="억류" 인 경우 지도에 별도 마커 표시.
VESSEL_INFO_DB: Dict[str, Dict] = {
    "9637222": {
        "imo":"9637222","vessel_name":"HMM DREAM","mmsi":"441981000",
        "vessel_type":"컨테이너선","flag":"KR","flag_full":"대한민국",
        "gt":130_000,"dwt":141_000,"built":2014,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"HMM Co., Ltd.","manager":"HMM",
        "special_status":None,"special_note":"",
    },
    "9637234": {
        "imo":"9637234","vessel_name":"HMM HOPE","mmsi":"440176000",
        "vessel_type":"컨테이너선","flag":"KR","flag_full":"대한민국",
        "gt":130_000,"dwt":141_000,"built":2014,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"HMM Co., Ltd.","manager":"HMM",
        "special_status":None,"special_note":"",
    },
    "9330721": {
        "imo":"9330721","vessel_name":"HYUNDAI GRACE","mmsi":"538007484",
        "vessel_type":"컨테이너선","flag":"MH","flag_full":"마샬군도",
        "gt":54_000,"dwt":67_000,"built":2006,
        "class_soc":"ABS (미국선급)","pi_club":"Gard",
        "owner":"Hyundai Merchant Marine","manager":"HMM",
        "special_status":None,"special_note":"",
    },
    "9637258": {
        "imo":"9637258","vessel_name":"HMM VICTORY","mmsi":"441754000",
        "vessel_type":"컨테이너선","flag":"KR","flag_full":"대한민국",
        "gt":130_000,"dwt":141_000,"built":2014,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"HMM Co., Ltd.","manager":"HMM",
        "special_status":None,"special_note":"",
    },
    "9459527": {
        "imo":"9459527","vessel_name":"SM JAGUAR","mmsi":"440174000",
        "vessel_type":"컨테이너선","flag":"KR","flag_full":"대한민국",
        "gt":93_000,"dwt":100_000,"built":2010,
        "class_soc":"KR (한국선급)","pi_club":"Britannia P&I Club",
        "owner":"SM Line Corp.","manager":"SM Line",
        "special_status":None,"special_note":"",
    },
    "9723899": {
        "imo":"9723899","vessel_name":"SM LION","mmsi":"374382000",
        "vessel_type":"컨테이너선","flag":"PA","flag_full":"파나마",
        "gt":114_000,"dwt":122_000,"built":2016,
        "class_soc":"DNV (노르웨이선급)","pi_club":"UK P&I Club",
        "owner":"SM Line Corp.","manager":"SM Line",
        "special_status":None,"special_note":"",
    },
    "9926738": {
        "imo":"9926738","vessel_name":"VL BREEZE","mmsi":"441345000",
        "vessel_type":"VLCC 탱커","flag":"KR","flag_full":"대한민국",
        "gt":160_000,"dwt":300_000,"built":2023,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"Korea Shipping Corp.","manager":"KSC",
        "special_status":"억류",
        "special_note":"페르시아만(아라비안만) 내 억류 중 (2026-03~현재). 이란 당국 나포. 항행 불가.",
        "lat":26.52,"lon":55.85,
    },
    "9590606": {
        "imo":"9590606","vessel_name":"GLOVIS ADVANCE","mmsi":"441222000",
        "vessel_type":"자동차운반선","flag":"KR","flag_full":"대한민국",
        "gt":72_000,"dwt":24_000,"built":2012,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"Hyundai Glovis Co., Ltd.","manager":"Hyundai Glovis",
        "special_status":None,"special_note":"",
    },
    "9845154": {
        "imo":"9845154","vessel_name":"V.PROGRESS","mmsi":"441099000",
        "vessel_type":"VLCC 탱커","flag":"KR","flag_full":"대한민국",
        "gt":162_000,"dwt":310_000,"built":2024,
        "class_soc":"KR (한국선급)","pi_club":"Korea P&I Club",
        "owner":"SK Shipping Co., Ltd.","manager":"SK Shipping",
        "special_status":None,"special_note":"",
    },
}

TYPHOON_SCENARIOS: Dict[str, List[Tuple[float,float,str]]] = {
    "서해 북상형": [(33.6,125.8,"강"),(34.4,126.2,"강"),(35.1,126.6,"중"),(35.9,126.9,"중"),(36.6,127.1,"약"),(37.2,127.4,"약")],
    "남해 상륙형": [(33.1,127.3,"강"),(34.0,127.6,"강"),(34.9,128.0,"중"),(35.7,128.4,"중"),(36.5,128.8,"약"),(37.1,129.2,"약")],
    "동해 통과형": [(33.9,129.5,"강"),(34.8,129.3,"강"),(35.7,129.2,"중"),(36.5,129.0,"중"),(37.3,128.9,"약"),(38.0,128.7,"약")],
}

INTENSITY_COLOR = {"강": "#c0392b", "중": "#e67e22", "약": "#f1c40f"}
RISK_COLOR = {"red": "#e74c3c", "orange": "#e67e22", "yellow": "#f1c40f", "green": "#27ae60"}

# ── 유틸 함수 ─────────────────────────────────────────────────────────────────
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2 * r * asin(sqrt(a))


def dummy_risk_score(factory_id: int, seed_offset: float = 0.0) -> Tuple[float, str]:
    import math
    score = 30 + 50 * abs(math.sin(factory_id * 1.7 + seed_offset))
    if score >= 75: level = "red"
    elif score >= 50: level = "orange"
    elif score >= 25: level = "yellow"
    else: level = "green"
    return round(score, 1), level


def vessel_risk(sog: float, age_hours: float = 0.0) -> Tuple[str, str]:
    if sog >= 16 or age_hours >= 4:
        return "HIGH", "#e74c3c"
    elif sog >= 13:
        return "MID", "#e67e22"
    return "NORMAL", "#27ae60"


# ── 데이터프레임 빌드 ─────────────────────────────────────────────────────────
def build_factory_df() -> pd.DataFrame:
    df = pd.DataFrame(DUMMY_FACTORIES)
    scores = [dummy_risk_score(r["factory_id"]) for _, r in df.iterrows()]
    df["Risk_Score"] = [s[0] for s in scores]
    df["Risk_Level"] = [s[1] for s in scores]
    contracts = pd.DataFrame(DUMMY_CONTRACTS_FACTORY)
    df = df.merge(contracts, on="factory_id", how="left")
    return df


def build_vessel_df() -> pd.DataFrame:
    df = pd.DataFrame(DUMMY_VESSELS)
    df["risk_level"], df["risk_color"] = zip(*[vessel_risk(float(r["sog"])) for _, r in df.iterrows()])
    if "insured_company" not in df.columns:
        df["insured_company"] = "기타"
    if "route" not in df.columns:
        df["route"] = ""
    return df


FACTORY_DF = build_factory_df()
VESSEL_DF = build_vessel_df()          # 더미 — AIS 없을 때 폴백
VESSEL_CONTRACTS_DF = pd.DataFrame(DUMMY_CONTRACTS_VESSEL)

# ── VesselFinder 폴링 시작 ───────────────────────────────────────────────────
if VF_API_KEY:
    _mmsi_list = [v["mmsi"] for v in DUMMY_VESSELS if v.get("mmsi")]
    _start_vf_thread(_mmsi_list)

# ── Dash 앱 초기화 ────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.FLATLY, dbc.icons.FONT_AWESOME],
    suppress_callback_exceptions=True,
    title="TCS Risk Management Platform",
    serve_locally=True,
)
server = app.server

# ── 공통 색상/스타일 ──────────────────────────────────────────────────────────
NAV_STYLE = {
    "position": "fixed",
    "top": 0,
    "left": 0,
    "bottom": 0,
    "width": "220px",
    "padding": "24px 0",
    "background": "#1a2942",
    "zIndex": 1000,
}
CONTENT_STYLE = {
    "marginLeft": "220px",
    "padding": "24px 32px",
    "background": "#f4f6fb",
    "minHeight": "100vh",
}
CARD_STYLE = {"borderRadius": "12px", "border": "none", "boxShadow": "0 2px 12px rgba(0,0,0,0.08)"}

def kpi_card(title: str, value: str, color: str = "#1a2942", icon: str = "fa-chart-bar") -> dbc.Card:
    return dbc.Card(
        dbc.CardBody([
            html.Div([
                html.I(className=f"fa-solid {icon}", style={"fontSize": "1.4rem", "color": color, "marginRight": "10px"}),
                html.Span(title, style={"fontSize": "0.85rem", "color": "#666", "fontWeight": "600"}),
            ], style={"display": "flex", "alignItems": "center", "marginBottom": "8px"}),
            html.Div(value, style={"fontSize": "1.9rem", "fontWeight": "800", "color": color}),
        ]),
        style=CARD_STYLE,
    )

def section_header(title: str) -> html.Div:
    return html.Div(title, style={
        "fontSize": "1.05rem", "fontWeight": "700", "color": "#1a2942",
        "borderLeft": "4px solid #2980b9", "paddingLeft": "10px",
        "marginBottom": "12px", "marginTop": "4px",
    })

def risk_badge(level: str) -> html.Span:
    labels = {"red":"HIGH","orange":"MID","yellow":"LOW","green":"NORMAL",
              "HIGH":"HIGH","MID":"MID","NORMAL":"NORMAL"}
    colors = {"red":"#e74c3c","orange":"#e67e22","yellow":"#f1c40f","green":"#27ae60",
              "HIGH":"#e74c3c","MID":"#e67e22","NORMAL":"#27ae60"}
    label = labels.get(level, level)
    bg = colors.get(level, "#aaa")
    return html.Span(label, style={
        "background": bg, "color": "#fff" if level != "yellow" else "#333",
        "borderRadius": "6px", "padding": "2px 10px",
        "fontSize": "0.75rem", "fontWeight": "700",
    })

# ── 사이드바 네비게이션 ───────────────────────────────────────────────────────
sidebar = html.Div([
    html.Div([
        html.Div("TCS", style={"color": "#fff", "fontWeight": "900", "fontSize": "1.5rem", "letterSpacing": "2px"}),
        html.Div("Risk Management", style={"color": "#7fb3d3", "fontSize": "0.72rem", "letterSpacing": "1px"}),
    ], style={"padding": "0 24px 28px 24px", "borderBottom": "1px solid #2c3e5a"}),

    html.Div([
        dbc.Nav([
            dbc.NavLink([html.I(className="fa-solid fa-house me-2"), "대시보드"],
                        href="/", active="exact",
                        style={"color": "#b0c4d8", "fontWeight": "600", "padding": "10px 24px"}),
            dbc.NavLink([html.I(className="fa-solid fa-tornado me-2"), "태풍 위험관리"],
                        href="/typhoon", active="exact",
                        style={"color": "#b0c4d8", "fontWeight": "600", "padding": "10px 24px"}),
            dbc.NavLink([html.I(className="fa-solid fa-ship me-2"), "해상 위험관리"],
                        href="/marine", active="exact",
                        style={"color": "#b0c4d8", "fontWeight": "600", "padding": "10px 24px"}),
            dbc.NavLink([html.I(className="fa-solid fa-calendar-days me-2"), "위험 캘린더"],
                        href="/calendar", active="exact",
                        style={"color": "#b0c4d8", "fontWeight": "600", "padding": "10px 24px"}),
        ], vertical=True, pills=True),
    ], style={"marginTop": "16px"}),

    html.Div([
        html.Div("DEMO v1.0", style={"color": "#4a6080", "fontSize": "0.72rem"}),
        html.Div("기업보험부문 리스크관리시스템", style={"color": "#4a6080", "fontSize": "0.68rem"}),
    ], style={"position": "absolute", "bottom": "24px", "left": "24px"}),
], style=NAV_STYLE)

# ── 레이아웃 ─────────────────────────────────────────────────────────────────
app.layout = html.Div([
    dcc.Location(id="url"),
    sidebar,
    html.Div(id="page-content", style=CONTENT_STYLE),
])

# ── 페이지 1: 계약 관리 대시보드 ────────────────────────────────────────────
def _build_home_map(selected_lob: str) -> go.Figure:
    """보종 필터에 따라 계약 위치 지도 생성"""
    fig = go.Figure()
    all_contracts = pd.DataFrame(DUMMY_CONTRACTS_FACTORY)
    factory_df = pd.DataFrame(DUMMY_FACTORIES)
    merged = all_contracts.merge(factory_df, on="factory_id", how="left")

    lobs_to_show = list(LOB_COLORS.keys()) if selected_lob == "전체" else [selected_lob]

    for lob in lobs_to_show:
        sub = merged[merged["lob"] == lob]
        if sub.empty:
            continue
        color = LOB_COLORS.get(lob, "#888")
        lats, lons, texts = [], [], []
        for _, r in sub.iterrows():
            lats.append(r["lat"])
            lons.append(r["lon"])
            amt = int(r["insured_amount_krw"]) / 1e8
            texts.append(
                f"<b>{r['factory_name']}</b><br>"
                f"보종: {r['lob']}<br>"
                f"상품: {r['product_name']}<br>"
                f"증권번호: {r['contract_id']}<br>"
                f"가입금액: {amt:.0f}억 원<br>"
                f"지역: {r['region']}<br>"
                f"만기: {r['expiry']}"
            )
        fig.add_trace(go.Scattermapbox(
            lat=lats, lon=lons,
            mode="markers",
            marker=dict(size=15, color=color, opacity=0.85),
            text=texts,
            hoverinfo="text",
            name=lob,
        ))

    fig.update_layout(
        mapbox=dict(style=MAP_STYLE, center=dict(lat=36.0, lon=128.0), zoom=6),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11), bgcolor="rgba(255,255,255,0.85)"),
        height=500,
    )
    return fig


def layout_home() -> html.Div:
    all_contracts_f = pd.DataFrame(DUMMY_CONTRACTS_FACTORY)
    all_contracts_v = pd.DataFrame(DUMMY_CONTRACTS_VESSEL)
    total_contracts = len(all_contracts_f) + len(all_contracts_v)
    total_exposure  = int(all_contracts_f["insured_amount_krw"].sum() + all_contracts_v["insured_amount_krw"].sum())
    land_exposure   = int(all_contracts_f["insured_amount_krw"].sum())
    marine_exposure = int(all_contracts_v["insured_amount_krw"].sum())

    # 만기 임박 (6개월 이내)
    today = datetime.now()
    expiring = sum(
        1 for c in DUMMY_CONTRACTS_FACTORY
        if (datetime.strptime(c["expiry"], "%Y-%m-%d") - today).days <= 180
    )

    # 보종별 집계
    lob_summary = all_contracts_f.groupby("lob").agg(
        건수=("contract_id", "count"),
        가입금액=("insured_amount_krw", "sum"),
    ).reset_index().sort_values("가입금액", ascending=False)

    # 지역별 집계
    factory_df = pd.DataFrame(DUMMY_FACTORIES)
    region_df  = all_contracts_f.merge(factory_df[["factory_id","region"]], on="factory_id", how="left")
    region_summary = region_df.groupby("region").agg(
        건수=("contract_id","count"),
        가입금액=("insured_amount_krw","sum"),
    ).reset_index().sort_values("가입금액", ascending=False)

    # 알림 피드
    alerts = [
        {"level":"danger",  "icon":"fa-triangle-exclamation","msg":"태풍 KHANUN 북상 — 충남·전남 계약 9건 영향권 (T-36h)"},
        {"level":"danger",  "icon":"fa-ship",                "msg":"HMM ALGECIRAS 고속 항해 17.1kn — P&I 위험"},
        {"level":"warning", "icon":"fa-clock",               "msg":"TCS-2026-FI-00208 (화재/폭발) 만기 D-174 — 갱신 검토"},
        {"level":"warning", "icon":"fa-wind",                "msg":"BLUE HERALD P&I 담보 공백 — 혼잡 수역 접근 중"},
        {"level":"info",    "icon":"fa-file-contract",       "msg":"TCS-2026-SP-00418 (건설공사) 만기 D-143"},
    ]
    alert_colors = {"danger":"#e74c3c","warning":"#e67e22","info":"#2980b9"}
    alert_items = []
    for a in alerts:
        color = alert_colors.get(a["level"], "#888")
        alert_items.append(html.Div([
            html.I(className=f"fa-solid {a['icon']}", style={"color":color,"marginRight":"10px","minWidth":"18px"}),
            html.Span(a["msg"], style={"fontSize":"0.82rem","color":"#333"}),
        ], style={"display":"flex","alignItems":"center","padding":"9px 0","borderBottom":"1px solid #f0f0f0"}))

    # LOB 필터 버튼
    lob_filter_buttons = dbc.ButtonGroup([
        dbc.Button("전체", id={"type":"lob-btn","index":"전체"}, size="sm", outline=True, color="secondary",
                   style={"fontWeight":"700","fontSize":"0.78rem"}),
        *[dbc.Button(lob, id={"type":"lob-btn","index":lob}, size="sm", outline=True,
                     style={"fontWeight":"600","fontSize":"0.78rem","color":LOB_COLORS[lob],
                            "borderColor":LOB_COLORS[lob]})
          for lob in LOB_COLORS],
    ], style={"flexWrap":"wrap","gap":"4px"})

    # 보종별 바 차트
    lob_chart = go.Figure(go.Bar(
        x=lob_summary["lob"],
        y=lob_summary["가입금액"] / 1e8,
        marker_color=[LOB_COLORS.get(l,"#888") for l in lob_summary["lob"]],
        text=[f"{v/1e8:.0f}억" for v in lob_summary["가입금액"]],
        textposition="outside",
        hovertemplate="%{x}<br>%{y:.0f}억 원<extra></extra>",
    ))
    lob_chart.update_layout(
        margin=dict(l=0,r=0,t=10,b=0), paper_bgcolor="white", plot_bgcolor="white",
        height=200, yaxis=dict(showgrid=True,gridcolor="#f0f0f0",title="억 원",tickfont=dict(size=10)),
        xaxis=dict(tickfont=dict(size=10)), bargap=0.3,
    )

    # 지역별 테이블
    region_rows = []
    for _, r in region_summary.iterrows():
        region_rows.append(html.Tr([
            html.Td(r["region"], style={"fontSize":"0.82rem","fontWeight":"600"}),
            html.Td(f"{int(r['건수'])}건", style={"textAlign":"right","fontSize":"0.82rem"}),
            html.Td(f"{int(r['가입금액'])/1e8:.0f}억", style={"textAlign":"right","fontSize":"0.82rem","color":"#2980b9","fontWeight":"700"}),
        ]))

    # 계약 목록 테이블 (전체)
    all_for_table = all_contracts_f.merge(factory_df[["factory_id","factory_name","region"]], on="factory_id", how="left")
    contract_rows = []
    for _, r in all_for_table.iterrows():
        days_to_expiry = (datetime.strptime(r["expiry"],"%Y-%m-%d") - today).days
        expiry_style = {"color":"#e74c3c","fontWeight":"700"} if days_to_expiry<=180 else {"color":"#555"}
        lob_color = LOB_COLORS.get(r["lob"],"#888")
        contract_rows.append(html.Tr([
            html.Td(r["contract_id"], style={"fontSize":"0.78rem","fontWeight":"700","fontFamily":"monospace"}),
            html.Td(r["factory_name"], style={"fontSize":"0.8rem"}),
            html.Td(html.Span(r["lob"], style={"background":lob_color,"color":"#fff","borderRadius":"4px",
                    "padding":"1px 7px","fontSize":"0.72rem","fontWeight":"600"})),
            html.Td(r["product_name"], style={"fontSize":"0.78rem","color":"#555"}),
            html.Td(r["region"], style={"fontSize":"0.78rem","textAlign":"center"}),
            html.Td(f"{int(r['insured_amount_krw'])/1e8:.0f}억", style={"textAlign":"right","fontSize":"0.82rem","color":"#2980b9","fontWeight":"700"}),
            html.Td(r["expiry"], style={"textAlign":"right","fontSize":"0.78rem",**expiry_style}),
        ]))

    return html.Div([
        # 헤더
        html.Div([
            html.Div([
                html.H4("계약 관리 대시보드", style={"fontWeight":"800","color":"#1a2942","margin":0}),
                html.Div(f"기준: {today.strftime('%Y-%m-%d %H:%M')} | 전체 관리 계약 {total_contracts}건",
                         style={"color":"#888","fontSize":"0.82rem"}),
            ]),
            dbc.Badge("● LIVE", color="danger", style={"fontSize":"0.78rem","padding":"6px 12px"}),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"16px"}),

        # 알림 배너
        dbc.Alert([
            html.I(className="fa-solid fa-triangle-exclamation me-2"),
            html.Strong("긴급: "), "태풍 KHANUN 북상 중 — 충남·전남 계약 9건 영향권. 해상 선박 1척 고속항해 위험.",
            dbc.Button("태풍 위험관리 바로가기", href="/typhoon", color="light", size="sm",
                       style={"marginLeft":"16px","fontWeight":"700","fontSize":"0.78rem"}),
        ], color="danger", style={"borderRadius":"10px","marginBottom":"16px","fontSize":"0.88rem"}),

        # KPI 5개
        dbc.Row([
            dbc.Col(kpi_card("총 계약 건수",    f"{total_contracts}건",         "#1a2942","fa-file-contract"), md=2),
            dbc.Col(kpi_card("총 가입금액",      f"{total_exposure/1e12:.2f}조 원","#2980b9","fa-won-sign"),    md=3),
            dbc.Col(kpi_card("육상 노출",        f"{land_exposure/1e8:.0f}억 원", "#e74c3c","fa-building"),     md=2),
            dbc.Col(kpi_card("해상 노출",        f"{marine_exposure/1e8:.0f}억 원","#27ae60","fa-ship"),        md=2),
            dbc.Col(kpi_card("만기 임박(6개월)", f"{expiring}건",                "#e67e22","fa-clock"),         md=3),
        ], className="mb-3"),

        # 지도 + 우측 패널
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    html.Div([
                        section_header("계약 위치 지도 — 보종별"),
                        html.Div(lob_filter_buttons, style={"marginBottom":"10px"}),
                    ]),
                    dcc.Graph(id="home-map", figure=_build_home_map("전체"),
                              config={"displayModeBar":False,"scrollZoom":True}),
                ]), style=CARD_STYLE),
            ], md=8),

            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("보종별 가입금액"),
                    dcc.Graph(figure=lob_chart, config={"displayModeBar":False},
                              style={"height":"200px"}),
                    html.Hr(style={"margin":"12px 0"}),
                    section_header("지역별 계약 현황"),
                    dbc.Table([
                        html.Thead(html.Tr([
                            html.Th("지역",style={"fontSize":"0.8rem"}),
                            html.Th("건수",style={"textAlign":"right","fontSize":"0.8rem"}),
                            html.Th("가입금액",style={"textAlign":"right","fontSize":"0.8rem"}),
                        ])),
                        html.Tbody(region_rows),
                    ], borderless=True, size="sm", hover=True),
                    html.Hr(style={"margin":"12px 0"}),
                    section_header("실시간 알림"),
                    html.Div(alert_items),
                ]), style={**CARD_STYLE,"maxHeight":"700px","overflowY":"auto"}),
            ], md=4),
        ], className="mb-3"),

        # 전체 계약 목록
        dbc.Card(dbc.CardBody([
            section_header(f"전체 계약 목록 — 육상 {len(DUMMY_CONTRACTS_FACTORY)}건 · 해상 {len(DUMMY_CONTRACTS_VESSEL)}건"),
            dbc.Table([
                html.Thead(html.Tr([
                    html.Th("증권번호",style={"fontSize":"0.8rem"}),
                    html.Th("계약자",style={"fontSize":"0.8rem"}),
                    html.Th("보종",style={"fontSize":"0.8rem"}),
                    html.Th("상품",style={"fontSize":"0.8rem"}),
                    html.Th("지역",style={"textAlign":"center","fontSize":"0.8rem"}),
                    html.Th("가입금액",style={"textAlign":"right","fontSize":"0.8rem"}),
                    html.Th("만기일",style={"textAlign":"right","fontSize":"0.8rem"}),
                ], style={"background":"#f8f9fa"})),
                html.Tbody(contract_rows),
            ], bordered=False, striped=True, hover=True, size="sm", responsive=True),
        ]), style=CARD_STYLE),
    ])


@callback(
    Output("home-map", "figure"),
    Input({"type":"lob-btn","index":dash.ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def filter_home_map(n_clicks_list: List):
    ctx = dash.callback_context
    if not ctx.triggered:
        return _build_home_map("전체")
    import json as _json
    triggered = ctx.triggered[0]["prop_id"]
    lob = _json.loads(triggered.split(".")[0])["index"]
    return _build_home_map(lob)


# ── 페이지 2: 태풍 위험관리 ──────────────────────────────────────────────────
def layout_typhoon() -> html.Div:
    return html.Div([
        html.Div([
            html.H4("태풍 위험관리", style={"fontWeight": "800", "color": "#1a2942", "margin": 0}),
            html.Div("태풍 경로 예측 → 영향 계약 즉시 식별 → 행동지침 자동 생성", style={"color": "#888", "fontSize": "0.82rem"}),
        ], style={"marginBottom": "20px"}),

        # 컨트롤
        dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Label("태풍 시나리오", style={"fontWeight": "600", "fontSize": "0.85rem", "color": "#444"}),
                    dcc.Dropdown(
                        id="typhoon-scenario",
                        options=[{"label": k, "value": k} for k in TYPHOON_SCENARIOS],
                        value="서해 북상형",
                        clearable=False,
                        style={"fontSize": "0.88rem"},
                    ),
                ], md=4),
                dbc.Col([
                    html.Label("영향권 반경 (km)", style={"fontWeight": "600", "fontSize": "0.85rem", "color": "#444"}),
                    dcc.Slider(id="typhoon-radius", min=30, max=180, step=10, value=100,
                               marks={30:"30km", 90:"90km", 180:"180km"},
                               tooltip={"placement": "bottom", "always_visible": True}),
                ], md=5),
                dbc.Col([
                    html.Label(" ", style={"display": "block"}),
                    dbc.Button([html.I(className="fa-solid fa-rotate me-2"), "분석 실행"],
                               id="typhoon-run", color="primary", style={"width": "100%", "fontWeight": "700"}),
                ], md=3),
            ], align="center"),
        ]), style={**CARD_STYLE, "marginBottom": "16px"}),

        # KPI
        dbc.Row(id="typhoon-kpi", className="mb-3"),

        # 지도 + 계약 리스트
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("태풍 경로 · 공장 위험 지도"),
                    dcc.Graph(id="typhoon-map", config={"displayModeBar": False, "scrollZoom": True}, style={"height": "460px"}),
                ]), style=CARD_STYLE),
            ], md=7),

            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("영향권 계약 리스트"),
                    html.Div(id="typhoon-contract-list"),
                ]), style={**CARD_STYLE, "height": "100%"}),
            ], md=5),
        ], className="mb-3"),

        # 선택 계약 상세
        dbc.Card(dbc.CardBody([
            section_header("선택 계약 상세 · 행동지침"),
            dbc.Row([
                dbc.Col(html.Div(id="typhoon-detail-left"), md=7),
                dbc.Col(html.Div(id="typhoon-detail-right"), md=5),
            ], className="g-3"),
        ]), style=CARD_STYLE, id="typhoon-detail-card"),

        # 우선순위 테이블 + 위험 세부 분류
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("영향권 계약 우선순위 (자동 산출)"),
                    html.Div(id="typhoon-priority-table"),
                ]), style=CARD_STYLE),
            ], md=7),
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("공장별 위험 세부 분류"),
                    html.Div(id="typhoon-risk-breakdown"),
                ]), style=CARD_STYLE),
            ], md=5),
        ], className="mt-3"),

        dcc.Store(id="typhoon-selected-id"),
    ])


@callback(
    Output("typhoon-kpi", "children"),
    Output("typhoon-map", "figure"),
    Output("typhoon-contract-list", "children"),
    Output("typhoon-priority-table", "children"),
    Output("typhoon-risk-breakdown", "children"),
    Input("typhoon-run", "n_clicks"),
    Input("typhoon-scenario", "value"),
    Input("typhoon-radius", "value"),
    prevent_initial_call=False,
)
def update_typhoon(n_clicks: Any, scenario: str, radius: int):
    track = TYPHOON_SCENARIOS.get(scenario, TYPHOON_SCENARIOS["서해 북상형"])

    # 영향 판정
    df = FACTORY_DF.copy()
    def min_dist(lat: float, lon: float) -> float:
        return min(haversine_km(lat, lon, p[0], p[1]) for p in track)

    df["dist_km"] = df.apply(lambda r: min_dist(r["lat"], r["lon"]), axis=1)
    df["impacted"] = df["dist_km"] <= radius

    impacted = df[df["impacted"]].sort_values(["Risk_Score", "dist_km"], ascending=[False, True])
    total_amt = impacted["insured_amount_krw"].sum()
    high_cnt = int((impacted["Risk_Score"] >= 60).sum())

    # KPI
    kpi_row = dbc.Row([
        dbc.Col(kpi_card("영향권 공장", f"{len(impacted)}개", "#e74c3c", "fa-industry"), md=3),
        dbc.Col(kpi_card("고위험 계약", f"{high_cnt}건", "#e67e22", "fa-triangle-exclamation"), md=3),
        dbc.Col(kpi_card("영향권 총 노출", f"{total_amt/1e8:.0f}억 원", "#2980b9", "fa-won-sign"), md=3),
        dbc.Col(kpi_card("분석 시나리오", scenario, "#1a2942", "fa-tornado"), md=3),
    ]).children

    # 지도
    fig = go.Figure()

    # 태풍 경로 선
    track_lats = [p[0] for p in track]
    track_lons = [p[1] for p in track]
    fig.add_trace(go.Scattermapbox(
        lat=track_lats, lon=track_lons,
        mode="lines",
        line=dict(width=4, color="#c0392b"),
        name="태풍 경로",
        hoverinfo="skip",
    ))

    # 태풍 포인트
    for i, p in enumerate(track):
        color = INTENSITY_COLOR.get(p[2], "#e74c3c")
        now = datetime.now()
        ts = now + timedelta(hours=i * 6)
        fig.add_trace(go.Scattermapbox(
            lat=[p[0]], lon=[p[1]],
            mode="markers",
            marker=dict(size=16, color=color, opacity=0.9),
            text=f"T+{i*6}h ({ts.strftime('%m-%d %H:%M')})<br>강도: {p[2]}",
            hoverinfo="text",
            name=f"T+{i*6}h",
            showlegend=False,
        ))

    # 영향 반경 원 (영향권 공장 중심)
    if not impacted.empty:
        for _, r in impacted.iterrows():
            fig.add_trace(go.Scattermapbox(
                lat=[r["lat"]], lon=[r["lon"]],
                mode="markers",
                marker=dict(size=22, color="#c0392b", opacity=0.18),
                hoverinfo="skip",
                showlegend=False,
            ))

    # 공장 마커
    for _, r in df.iterrows():
        if r["impacted"]:
            color, size, border = "#e74c3c", 16, 3
        else:
            color = RISK_COLOR.get(r["Risk_Level"], "#888")
            size, border = 11, 1
        fig.add_trace(go.Scattermapbox(
            lat=[r["lat"]], lon=[r["lon"]],
            mode="markers",
            marker=dict(size=size, color=color, opacity=0.88),
            text=(f"<b>{r['factory_name']}</b><br>"
                  f"위험점수: {r['Risk_Score']:.1f}<br>"
                  f"경로 거리: {r['dist_km']:.1f}km<br>"
                  f"{'⚠ 영향권' if r['impacted'] else '정상'}"),
            hoverinfo="text",
            showlegend=False,
        ))

    fig.update_layout(
        mapbox=dict(style=MAP_STYLE, center=dict(lat=35.8, lon=127.5), zoom=6),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="white",
        showlegend=False,
    )

    # 계약 리스트
    if impacted.empty:
        contract_list = dbc.Alert("현재 설정 기준 영향권 공장이 없습니다. 반경을 넓혀보세요.", color="info")
    else:
        rows = []
        for _, r in impacted.iterrows():
            rows.append(
                dbc.ListGroupItem([
                    html.Div([
                        html.Div([
                            html.Strong(r["factory_name"], style={"fontSize": "0.88rem"}),
                            html.Span(f" {r['contract_id']}", style={"color": "#888", "fontSize": "0.78rem"}),
                        ]),
                        risk_badge(r["Risk_Level"]),
                    ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"}),
                    html.Div([
                        html.Span(f"위험 {r['Risk_Score']:.1f}점", style={"fontSize": "0.78rem", "color": "#555", "marginRight": "12px"}),
                        html.Span(f"거리 {r['dist_km']:.1f}km", style={"fontSize": "0.78rem", "color": "#555", "marginRight": "12px"}),
                        html.Span(f"{int(r['insured_amount_krw'])/1e8:.0f}억원", style={"fontSize": "0.78rem", "color": "#2980b9", "fontWeight": "700"}),
                    ], style={"marginTop": "4px"}),
                ], style={"cursor": "pointer", "padding": "10px 12px"},
                id={"type": "typhoon-contract-item", "index": int(r["factory_id"])},
                action=True,
                )
            )
        contract_list = dbc.ListGroup(rows, flush=True, style={"maxHeight": "400px", "overflowY": "auto"})

    # 우선순위 테이블
    if impacted.empty:
        priority_table = dbc.Alert("영향권 공장이 없습니다.", color="info")
        risk_breakdown = dbc.Alert("데이터 없음", color="secondary")
    else:
        # 우선순위 점수: 위험(50%) + 경로거리역산(30%) + 가입금액(20%)
        pri = impacted.copy()
        pri["priority_score"] = (
            0.5 * pri["Risk_Score"]
            + 0.3 * (100 - pri["dist_km"].clip(0, 100))
            + 0.2 * (pri["insured_amount_krw"] / pri["insured_amount_krw"].max() * 100)
        ).round(1)
        pri = pri.sort_values("priority_score", ascending=False)

        def _action(r: Any) -> str:
            acts = []
            if r["Risk_Score"] >= 70: acts.append("비상대응 상향")
            if r.get("cover_bi"): acts.append("휴업손해 서류 준비")
            else: acts.append("휴업손해 미가입 고지")
            acts.append("운송 일정 재조정")
            return " / ".join(acts[:2])

        p_rows = []
        for rank, (_, r) in enumerate(pri.iterrows(), 1):
            p_rows.append(html.Tr([
                html.Td(html.Strong(f"#{rank}"), style={"textAlign":"center","width":"40px"}),
                html.Td(r["factory_name"], style={"fontSize":"0.83rem","fontWeight":"600"}),
                html.Td(risk_badge(r["Risk_Level"]), style={"textAlign":"center"}),
                html.Td(f"{r['Risk_Score']:.1f}", style={"textAlign":"right","fontSize":"0.83rem"}),
                html.Td(f"{r['dist_km']:.1f}km", style={"textAlign":"right","fontSize":"0.83rem","color":"#e74c3c"}),
                html.Td(f"{int(r['insured_amount_krw'])/1e8:.0f}억", style={"textAlign":"right","fontSize":"0.83rem","color":"#2980b9","fontWeight":"700"}),
                html.Td(f"{r['priority_score']:.1f}", style={"textAlign":"right","fontSize":"0.83rem","fontWeight":"800","color":"#8e44ad"}),
                html.Td(_action(r), style={"fontSize":"0.75rem","color":"#555"}),
            ]))

        priority_table = dbc.Table([
            html.Thead(html.Tr([
                html.Th("순위",style={"width":"40px","textAlign":"center"}),
                html.Th("공장명"), html.Th("등급",style={"textAlign":"center"}),
                html.Th("위험점수",style={"textAlign":"right"}), html.Th("경로거리",style={"textAlign":"right"}),
                html.Th("가입금액",style={"textAlign":"right"}), html.Th("우선순위점수",style={"textAlign":"right","color":"#8e44ad"}),
                html.Th("핵심조치"),
            ], style={"fontSize":"0.8rem","background":"#f8f9fa"})),
            html.Tbody(p_rows),
        ], bordered=False, striped=True, hover=True, size="sm", responsive=True)

        # 위험 세부 분류 — 더미 위험 세부값 생성
        import math as _math
        breakdown_rows = []
        for _, r in pri.iterrows():
            fid = int(r["factory_id"])
            flood   = round(30 + 40 * abs(_math.sin(fid * 1.3)), 1)
            wind    = round(20 + 50 * abs(_math.sin(fid * 2.1)), 1)
            heat    = round(15 + 35 * abs(_math.sin(fid * 0.9)), 1)
            logist  = round(10 + 45 * abs(_math.sin(fid * 1.7)), 1)

            def bar(val: float) -> html.Div:
                color = "#e74c3c" if val >= 65 else ("#e67e22" if val >= 40 else "#27ae60")
                return html.Div(html.Div(style={"width":f"{val:.0f}%","height":"6px","background":color,"borderRadius":"3px"}),
                                style={"background":"#eee","borderRadius":"3px","width":"80px","display":"inline-block","verticalAlign":"middle"})

            breakdown_rows.append(html.Tr([
                html.Td(r["factory_name"], style={"fontSize":"0.8rem","fontWeight":"600","whiteSpace":"nowrap"}),
                html.Td([bar(flood),  html.Span(f" {flood:.0f}", style={"fontSize":"0.75rem","marginLeft":"4px"})]),
                html.Td([bar(wind),   html.Span(f" {wind:.0f}",  style={"fontSize":"0.75rem","marginLeft":"4px"})]),
                html.Td([bar(heat),   html.Span(f" {heat:.0f}",  style={"fontSize":"0.75rem","marginLeft":"4px"})]),
                html.Td([bar(logist), html.Span(f" {logist:.0f}",style={"fontSize":"0.75rem","marginLeft":"4px"})]),
            ]))

        risk_breakdown = dbc.Table([
            html.Thead(html.Tr([
                html.Th("공장명",style={"fontSize":"0.8rem"}),
                html.Th("침수",style={"fontSize":"0.8rem"}), html.Th("강풍",style={"fontSize":"0.8rem"}),
                html.Th("과열",style={"fontSize":"0.8rem"}), html.Th("물류",style={"fontSize":"0.8rem"}),
            ], style={"background":"#f8f9fa"})),
            html.Tbody(breakdown_rows),
        ], bordered=False, striped=True, size="sm", responsive=True,
           style={"maxHeight":"280px","overflowY":"auto","display":"block"})

    return kpi_row, fig, contract_list, priority_table, risk_breakdown


@callback(
    Output("typhoon-detail-left", "children"),
    Output("typhoon-detail-right", "children"),
    Input({"type": "typhoon-contract-item", "index": dash.ALL}, "n_clicks"),
    State("typhoon-scenario", "value"),
    State("typhoon-radius", "value"),
    prevent_initial_call=True,
)
def typhoon_detail(n_clicks_list: List, scenario: str, radius: int):
    ctx = dash.callback_context
    if not ctx.triggered:
        return "", ""

    triggered = ctx.triggered[0]["prop_id"]
    import json as _json
    fid = _json.loads(triggered.split(".")[0])["index"]

    track = TYPHOON_SCENARIOS.get(scenario, TYPHOON_SCENARIOS["서해 북상형"])
    df = FACTORY_DF.copy()
    df["dist_km"] = df.apply(lambda r: min(haversine_km(r["lat"], r["lon"], p[0], p[1]) for p in track), axis=1)
    row = df[df["factory_id"] == fid].iloc[0]

    # 행동지침
    actions = []
    if row["Risk_Score"] >= 70:
        actions.append(("fa-triangle-exclamation", "#e74c3c", "현장 비상대응 단계 즉시 상향"))
    if row.get("cover_flood"):
        actions.append(("fa-droplet", "#2980b9", "침수 예방 설비 사전 점검"))
    if row.get("cover_bi"):
        actions.append(("fa-file-invoice", "#8e44ad", "휴업손해 특약 청구 서류 사전 안내"))
    else:
        actions.append(("fa-circle-exclamation", "#e67e22", "휴업손해 담보 미가입 — 고객 고지 필요"))
    actions.append(("fa-truck", "#27ae60", "출하 일정 재조정 및 운송사 협의"))

    # 담보 항목 — 2×2 뱃지 그리드
    cover_items = [("cover_typhoon","fa-wind","태풍"), ("cover_flood","fa-droplet","침수"),
                   ("cover_wind","fa-tornado","강풍"), ("cover_bi","fa-briefcase","휴업손해")]
    cover_badges = html.Div([
        html.Div([
            html.Div([
                html.I(className=f"fa-solid {icon} me-1"),
                html.Span(label),
            ], style={"fontSize":"0.78rem","fontWeight":"600","marginBottom":"3px",
                      "color":"#27ae60" if row.get(col) else "#e74c3c"}),
            html.Div(
                "✔ 담보" if row.get(col) else "✘ 미담보",
                style={
                    "fontSize":"0.72rem","fontWeight":"700","padding":"2px 8px",
                    "borderRadius":"12px","display":"inline-block",
                    "background":"#eafaf1" if row.get(col) else "#fdecea",
                    "color":"#27ae60" if row.get(col) else "#e74c3c",
                    "border": "1px solid #27ae60" if row.get(col) else "1px solid #e74c3c",
                }
            ),
        ], style={
            "background":"#f8f9fa","borderRadius":"8px","padding":"10px 14px",
            "border": "1px solid #e8f8f0" if row.get(col) else "1px solid #fdecea",
        })
        for col, icon, label in cover_items
    ], style={"display":"grid","gridTemplateColumns":"1fr 1fr","gap":"8px","marginTop":"10px"})

    # 위험 점수 바
    score = row["Risk_Score"]
    score_color = {"red":"#e74c3c","orange":"#e67e22","yellow":"#f1c40f","green":"#27ae60"}.get(row["Risk_Level"],"#888")
    score_bar = html.Div([
        html.Div(style={
            "width": f"{score:.0f}%", "height":"100%",
            "background": score_color, "borderRadius":"4px",
            "transition":"width 0.4s ease",
        })
    ], style={"background":"#eee","borderRadius":"4px","height":"8px","marginBottom":"4px"})

    left = html.Div([
        # 상단: 공장명 + 위험등급 배지
        html.Div([
            html.Div([
                html.I(className="fa-solid fa-industry me-2", style={"color":"#1a2942"}),
                html.Span(row["factory_name"], style={"fontWeight":"700","fontSize":"1rem","color":"#1a2942"}),
            ]),
            risk_badge(row["Risk_Level"]),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"12px"}),

        # 핵심 수치 3개 하이라이트 박스
        html.Div([
            html.Div([
                html.Div("가입금액", style={"fontSize":"0.72rem","color":"#888","marginBottom":"2px"}),
                html.Div(f"{int(row['insured_amount_krw'])/1e8:.0f}억 원",
                         style={"fontWeight":"700","fontSize":"1.1rem","color":"#2980b9"}),
            ], style={"textAlign":"center","flex":"1","padding":"8px","background":"#eaf4fb","borderRadius":"8px"}),
            html.Div([
                html.Div("자기부담금", style={"fontSize":"0.72rem","color":"#888","marginBottom":"2px"}),
                html.Div(f"{int(row['deductible_krw'])/1e6:.0f}백만 원",
                         style={"fontWeight":"700","fontSize":"1.1rem","color":"#555"}),
            ], style={"textAlign":"center","flex":"1","padding":"8px","background":"#f5f5f5","borderRadius":"8px"}),
            html.Div([
                html.Div("태풍 경로 거리", style={"fontSize":"0.72rem","color":"#888","marginBottom":"2px"}),
                html.Div(f"{row['dist_km']:.1f} km",
                         style={"fontWeight":"700","fontSize":"1.1rem","color":"#e74c3c"}),
            ], style={"textAlign":"center","flex":"1","padding":"8px","background":"#fef6f6","borderRadius":"8px"}),
        ], style={"display":"flex","gap":"8px","marginBottom":"12px"}),

        # 부가 정보
        html.Div([
            html.Span("증권번호", style={"fontSize":"0.78rem","color":"#888","marginRight":"6px"}),
            html.Span(row["contract_id"], style={"fontSize":"0.78rem","fontWeight":"600","color":"#333","marginRight":"16px"}),
            html.Span("상품", style={"fontSize":"0.78rem","color":"#888","marginRight":"6px"}),
            html.Span(row["product_name"], style={"fontSize":"0.78rem","fontWeight":"600","color":"#333"}),
        ], style={"marginBottom":"12px","padding":"8px 10px","background":"#f8f9fa","borderRadius":"6px"}),

        # 담보 뱃지
        html.Div("담보 현황", style={"fontSize":"0.8rem","fontWeight":"700","color":"#444","marginBottom":"4px"}),
        cover_badges,
    ])

    right = html.Div([
        # 위험 점수 게이지
        html.Div([
            html.Div([
                html.Span("위험 점수", style={"fontSize":"0.8rem","color":"#666"}),
                html.Span(f"{score:.0f}점", style={"fontWeight":"700","fontSize":"1.1rem","color":score_color,"marginLeft":"8px"}),
                risk_badge(row["Risk_Level"]),
            ], style={"display":"flex","alignItems":"center","gap":"6px","marginBottom":"6px"}),
            score_bar,
            html.Div(f"위험 등급: {row['Risk_Level'].upper()}  |  {row['industry']} / {row['region']}",
                     style={"fontSize":"0.75rem","color":"#888","marginBottom":"14px"}),
        ], style={"background":"#f8f9fa","borderRadius":"8px","padding":"12px","marginBottom":"12px"}),

        # 행동지침
        html.Div("핵심 행동지침", style={"fontWeight":"700","fontSize":"0.85rem","color":"#1a2942","marginBottom":"8px"}),
        html.Div([
            html.Div([
                html.Div([
                    html.I(className=f"fa-solid {icon} me-2", style={"color":color,"width":"16px"}),
                    html.Span(text, style={"fontSize":"0.83rem","color":"#333"}),
                ], style={"display":"flex","alignItems":"center"}),
            ], style={
                "padding":"9px 12px","borderRadius":"7px","marginBottom":"6px",
                "background":"#fff","border":f"1px solid #eee",
                "borderLeft":f"3px solid {color}",
            })
            for icon, color, text in actions
        ]),
    ])

    return left, right


# ── 페이지 3: 위험 캘린더 ────────────────────────────────────────────────────
def layout_calendar() -> html.Div:
    today = datetime.now()
    cur_m = today.month  # 현재 월(1-12)

    MONTHS = ["1월","2월","3월","4월","5월","6월","7월","8월","9월","10월","11월","12월"]

    # ── 계절별 위험 강도 데이터 (1~12월, 0-10 스케일) ─────────────────────────
    SEASONAL_RISK = {
        "태풍":          [0, 0, 0, 1, 2, 4, 8, 10, 8, 3, 1, 0],
        "집중호우":      [1, 1, 2, 3, 5, 9, 10, 9,  6, 3, 2, 1],
        "해상 풍랑":     [8, 8, 7, 5, 4, 4, 5, 5,   7, 7, 8, 9],
        "동결·결빙":     [9, 8, 5, 2, 1, 0, 0, 0,   0, 1, 4, 8],
        "폭염·건조화재": [1, 1, 2, 4, 6, 8, 10,10,  7, 4, 2, 1],
    }

    # ── 히트맵 ───────────────────────────────────────────────────────────────
    risk_types = list(SEASONAL_RISK.keys())
    z_vals = [SEASONAL_RISK[rt] for rt in risk_types]
    # 현재 월 강조를 위한 shape 준비
    heatmap_fig = go.Figure(go.Heatmap(
        z=z_vals,
        x=MONTHS,
        y=risk_types,
        colorscale=[
            [0.0,  "#eafaf1"], [0.2, "#a9dfbf"],
            [0.45, "#f9e79f"], [0.7, "#f0a500"],
            [1.0,  "#c0392b"],
        ],
        zmin=0, zmax=10,
        text=[[str(v) if v > 0 else "" for v in row] for row in z_vals],
        texttemplate="%{text}",
        textfont={"size": 11, "color": "#333"},
        hovertemplate="%{y}  %{x}<br>위험 강도: %{z}/10<extra></extra>",
        showscale=True,
        colorbar=dict(title="위험강도", thickness=12, len=0.8),
    ))
    # 현재 월 컬럼 강조 (세로 rect)
    heatmap_fig.add_vrect(
        x0=cur_m - 1.5, x1=cur_m - 0.5,
        fillcolor="rgba(41,128,185,0.12)",
        layer="below", line_width=2,
        line=dict(color="#2980b9", width=2),
        annotation_text=f"현재({MONTHS[cur_m-1]})",
        annotation_position="top left",
        annotation_font=dict(size=11, color="#2980b9"),
    )
    heatmap_fig.update_layout(
        margin=dict(l=0, r=0, t=8, b=0),
        paper_bgcolor="white", plot_bgcolor="white",
        height=220,
        xaxis=dict(side="top", tickfont=dict(size=11)),
        yaxis=dict(tickfont=dict(size=11)),
    )

    # ── 만기 계약 월별 집계 바 차트 ──────────────────────────────────────────
    contracts_f = pd.DataFrame(DUMMY_CONTRACTS_FACTORY)
    contracts_f["expiry_dt"] = pd.to_datetime(contracts_f["expiry"])

    # 향후 12개월 키(YYYY-MM)·표시 라벨(N월) 생성
    month_keys:    List[str] = []
    month_display: List[str] = []
    for i in range(12):
        m = (today.month - 1 + i) % 12 + 1
        y = today.year + (today.month - 1 + i) // 12
        month_keys.append(f"{y}-{m:02d}")
        month_display.append(f"{m}월")

    future = contracts_f[
        (contracts_f["expiry_dt"] >= today) &
        (contracts_f["expiry_dt"] <= today + timedelta(days=365))
    ].copy()
    future["exp_month"] = future["expiry_dt"].dt.strftime("%Y-%m")

    # 태풍 시즌(6~9월) 구간 배경색 계산
    typhoon_x = [mk for mk in month_keys if "-06" <= mk[-3:] <= "-09"]

    lob_order = list(LOB_COLORS.keys())
    expiry_fig = go.Figure()
    for lob in lob_order:
        sub = future[future["lob"] == lob]
        counts = sub.groupby("exp_month").size().reindex(month_keys, fill_value=0)
        amts   = sub.groupby("exp_month")["insured_amount_krw"].sum().reindex(month_keys, fill_value=0) / 1e8
        expiry_fig.add_trace(go.Bar(
            x=month_display,
            y=counts.values,
            name=lob,
            marker_color=LOB_COLORS[lob],
            customdata=amts.values,
            hovertemplate=f"<b>{lob}</b><br>%{{x}}<br>건수: %{{y}}건<br>가입금액: %{{customdata:.0f}}억<extra></extra>",
        ))

    # 태풍 시즌 구간 음영
    typhoon_display = [month_display[month_keys.index(mk)] for mk in typhoon_x if mk in month_keys]
    if len(typhoon_display) >= 2:
        expiry_fig.add_vrect(
            x0=typhoon_display[0], x1=typhoon_display[-1],
            fillcolor="rgba(231,76,60,0.07)", layer="below",
            line=dict(color="#e74c3c", width=1, dash="dot"),
            annotation_text="태풍 시즌", annotation_position="top left",
            annotation_font=dict(size=10, color="#e74c3c"),
        )
    expiry_fig.update_layout(
        barmode="stack",
        margin=dict(l=0, r=10, t=8, b=0),
        paper_bgcolor="white", plot_bgcolor="white",
        height=240,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=10)),
        xaxis=dict(tickfont=dict(size=11)),
        yaxis=dict(title="만기 건수", tickfont=dict(size=10), dtick=1),
    )

    # ── 포트폴리오 집중도 분석 ────────────────────────────────────────────────
    factory_df_full = FACTORY_DF.copy()

    # 지역별 가입액 (수평 바)
    region_amt = (
        factory_df_full.groupby("region")["insured_amount_krw"]
        .sum().sort_values(ascending=True) / 1e8
    )
    region_fig = go.Figure(go.Bar(
        x=region_amt.values,
        y=region_amt.index,
        orientation="h",
        marker=dict(
            color=region_amt.values,
            colorscale=[[0,"#aed6f1"],[0.5,"#2980b9"],[1,"#1a2942"]],
            showscale=False,
        ),
        text=[f"{v:.0f}억" for v in region_amt.values],
        textposition="outside",
        hovertemplate="%{y}<br>%{x:.0f}억 원<extra></extra>",
    ))
    region_fig.update_layout(
        margin=dict(l=0, r=40, t=8, b=0), height=220,
        paper_bgcolor="white", plot_bgcolor="white",
        xaxis=dict(showticklabels=False, showgrid=False),
        yaxis=dict(tickfont=dict(size=11)),
    )

    # 보종별 가입액 (도넛)
    lob_amt = factory_df_full.groupby("lob")["insured_amount_krw"].sum() / 1e8
    donut_lob = go.Figure(go.Pie(
        labels=lob_amt.index.tolist(),
        values=lob_amt.values,
        hole=0.55,
        marker=dict(colors=[LOB_COLORS.get(l, "#888") for l in lob_amt.index]),
        textinfo="percent",
        textfont=dict(size=11),
        hovertemplate="%{label}<br>%{value:.0f}억<br>%{percent}<extra></extra>",
    ))
    donut_lob.update_layout(
        margin=dict(l=0, r=0, t=8, b=0), height=220,
        paper_bgcolor="white",
        showlegend=True,
        legend=dict(font=dict(size=10), orientation="v", x=1.0, y=0.5),
        annotations=[dict(text="보종별", x=0.5, y=0.5, font_size=12, showarrow=False, font_color="#555")],
    )

    # 업종별 가입액 (도넛)
    industry_amt = factory_df_full.groupby("industry")["insured_amount_krw"].sum().sort_values(ascending=False) / 1e8
    INDUSTRY_COLORS = ["#1a2942","#2980b9","#27ae60","#e67e22","#e74c3c",
                       "#8e44ad","#16a085","#f39c12","#2c3e50","#7f8c8d"]
    donut_ind = go.Figure(go.Pie(
        labels=industry_amt.index.tolist(),
        values=industry_amt.values,
        hole=0.55,
        marker=dict(colors=INDUSTRY_COLORS[:len(industry_amt)]),
        textinfo="percent",
        textfont=dict(size=11),
        hovertemplate="%{label}<br>%{value:.0f}억<br>%{percent}<extra></extra>",
    ))
    donut_ind.update_layout(
        margin=dict(l=0, r=0, t=8, b=0), height=220,
        paper_bgcolor="white",
        showlegend=True,
        legend=dict(font=dict(size=10), orientation="v", x=1.0, y=0.5),
        annotations=[dict(text="업종별", x=0.5, y=0.5, font_size=12, showarrow=False, font_color="#555")],
    )

    # ── KPI 계산 ──────────────────────────────────────────────────────────────
    typhoon_start = datetime(today.year, 6, 1)
    if today >= typhoon_start:
        typhoon_start = datetime(today.year + 1, 6, 1)
    days_to_typhoon = (typhoon_start - today).days

    expiry_6m = contracts_f[
        (contracts_f["expiry_dt"] >= today) &
        (contracts_f["expiry_dt"] <= today + timedelta(days=180))
    ]
    cur_risk_val = max(SEASONAL_RISK[rt][cur_m - 1] for rt in risk_types)
    cur_risk_label = "높음" if cur_risk_val >= 7 else "보통" if cur_risk_val >= 4 else "낮음"
    cur_risk_color = "#e74c3c" if cur_risk_val >= 7 else "#e67e22" if cur_risk_val >= 4 else "#27ae60"

    kpi_row = html.Div([
        html.Div(kpi_card("태풍 시즌까지", f"D-{days_to_typhoon}일", "#2980b9", "fa-wind"),
                 style={"flex":"1","minWidth":"180px"}),
        html.Div(kpi_card("6개월 내 만기", f"{len(expiry_6m)}건", "#e74c3c", "fa-file-circle-exclamation"),
                 style={"flex":"1","minWidth":"180px"}),
        html.Div(kpi_card("6개월 내 만기 가입액", f"{int(expiry_6m['insured_amount_krw'].sum())/1e8:.0f}억",
                          "#8e44ad", "fa-won-sign"), style={"flex":"1","minWidth":"180px"}),
        html.Div(kpi_card(f"{MONTHS[cur_m-1]} 위험 수준", cur_risk_label, cur_risk_color, "fa-gauge-high"),
                 style={"flex":"1","minWidth":"180px"}),
    ], style={"display":"flex","gap":"12px","flexWrap":"wrap","marginBottom":"20px"})

    # ── 이달의 사전 점검 체크리스트 ─────────────────────────────────────────
    MONTHLY_CHECKLIST: Dict[int, List[Tuple[str,str,str]]] = {
        1:  [("fa-snowflake","#2980b9","동결 배관·소화설비 점검"),
             ("fa-wind","#7f8c8d","동절기 강풍 대비 지붕·외벽 점검"),
             ("fa-file-contract","#8e44ad","연간 계약 갱신 일정 수립")],
        2:  [("fa-snowflake","#2980b9","결빙 구간 선박 항로 기상 모니터링"),
             ("fa-file-contract","#8e44ad","상반기 만기 계약 갱신 안내 발송")],
        3:  [("fa-seedling","#27ae60","봄철 건조 화재 예방 점검"),
             ("fa-ship","#2980b9","춘계 풍랑 시즌 선박 항로 리뷰"),
             ("fa-file-invoice","#e67e22","1분기 리스크 현황 보고서 작성")],
        4:  [("fa-wind","#e67e22","태풍 시즌 사전 대비 점검 시작"),
             ("fa-ship","#2980b9","선박 항로 기상 예보 모니터링 강화"),
             ("fa-file-contract","#8e44ad","하반기 만기 계약 갱신 안내 준비"),
             ("fa-industry","#1a2942","공장 방재 설비 연간 점검 스케줄 확정")],
        5:  [("fa-cloud-showers-heavy","#2980b9","장마 대비 침수 취약 시설 사전 점검"),
             ("fa-triangle-exclamation","#e74c3c","태풍 조기경보 시스템 테스트"),
             ("fa-file-contract","#8e44ad","태풍 시즌 임박 계약자 사전 고지")],
        6:  [("fa-tornado","#e74c3c","태풍 시즌 개시 — 일일 기상 모니터링"),
             ("fa-droplet","#2980b9","집중호우 대비 배수 시설 긴급 점검"),
             ("fa-ship","#8e44ad","태풍 경보 시 선박 대피 항구 사전 지정")],
        7:  [("fa-tornado","#e74c3c","태풍 최고 위험 — 비상 대응 체계 가동"),
             ("fa-fire","#e67e22","폭염 건조 화재 위험 — 인화물질 관리 강화"),
             ("fa-ship","#8e44ad","풍랑 주의보 구간 선박 항로 우회 검토")],
        8:  [("fa-tornado","#e74c3c","태풍 최성기 — 임박 계약 손해 예방 순방"),
             ("fa-fire","#e67e22","폭염 지속 시 전력 과부하 화재 위험 점검")],
        9:  [("fa-tornado","#e74c3c","태풍 시즌 후반 — 잔류 태풍 모니터링"),
             ("fa-cloud-showers-heavy","#2980b9","가을 장마 침수 대비"),
             ("fa-file-invoice","#8e44ad","3분기 리스크 현황 보고서 작성")],
        10: [("fa-wind","#e67e22","태풍 시즌 종료 점검 — 시설 피해 조사"),
             ("fa-snowflake","#2980b9","동절기 대비 보일러·배관 사전 점검"),
             ("fa-file-contract","#8e44ad","연말 만기 계약 갱신 안내 발송")],
        11: [("fa-snowflake","#2980b9","결빙 대비 소방·배관 동파 방지 조치"),
             ("fa-ship","#7f8c8d","동절기 풍랑 강화 구간 항로 리뷰"),
             ("fa-file-contract","#8e44ad","연간 계약 포트폴리오 리뷰")],
        12: [("fa-snowflake","#2980b9","연말 동절기 결빙 전면 점검"),
             ("fa-file-invoice","#8e44ad","연간 리스크 관리 성과 보고서"),
             ("fa-calendar-plus","#27ae60","익년도 위험 관리 계획 수립")],
    }

    checklist_items = MONTHLY_CHECKLIST.get(cur_m, [])
    checklist_ui = html.Div([
        html.Div(
            [html.I(className=f"fa-solid {icon} me-2", style={"color": color, "width": "16px"}),
             html.Span(text, style={"fontSize": "0.84rem", "color": "#333"})],
            style={
                "display": "flex", "alignItems": "center",
                "padding": "10px 14px", "marginBottom": "8px",
                "background": "#fff", "borderRadius": "8px",
                "border": "1px solid #eee",
                "borderLeft": f"4px solid {color}",
                "boxShadow": "0 1px 3px rgba(0,0,0,0.05)",
            }
        )
        for icon, color, text in checklist_items
    ])

    # ── 만기 임박 계약 상세 리스트 ───────────────────────────────────────────
    expiry_rows = []
    for _, c in expiry_6m.sort_values("expiry_dt").iterrows():
        days_left = (c["expiry_dt"] - today).days
        badge_color = "danger" if days_left <= 60 else "warning" if days_left <= 120 else "secondary"
        expiry_rows.append(html.Tr([
            html.Td(c["contract_id"], style={"fontSize":"0.8rem","fontWeight":"600"}),
            html.Td(c["lob"], style={"fontSize":"0.8rem"}),
            html.Td(c["product_name"], style={"fontSize":"0.8rem"}),
            html.Td(f"{int(c['insured_amount_krw'])/1e8:.0f}억", style={"fontSize":"0.8rem","textAlign":"right"}),
            html.Td(c["expiry"], style={"fontSize":"0.8rem","textAlign":"center"}),
            html.Td(dbc.Badge(f"D-{days_left}", color=badge_color, pill=True), style={"textAlign":"center"}),
        ]))

    expiry_table = dbc.Table(
        [html.Thead(html.Tr([
            html.Th("증권번호",style={"fontSize":"0.8rem"}),
            html.Th("보종",   style={"fontSize":"0.8rem"}),
            html.Th("상품명", style={"fontSize":"0.8rem"}),
            html.Th("가입금액",style={"fontSize":"0.8rem","textAlign":"right"}),
            html.Th("만기일", style={"fontSize":"0.8rem","textAlign":"center"}),
            html.Th("D-Day",  style={"fontSize":"0.8rem","textAlign":"center"}),
        ])),
         html.Tbody(expiry_rows if expiry_rows else [html.Tr([html.Td("6개월 내 만기 계약 없음", colSpan=6,
             style={"textAlign":"center","color":"#888","fontSize":"0.85rem"})])])],
        bordered=False, hover=True, size="sm", striped=True,
    )

    return html.Div([
        html.Div([
            html.H5([html.I(className="fa-solid fa-calendar-days me-2"), "위험 캘린더"],
                    style={"fontWeight":"700","color":"#1a2942","marginBottom":"4px"}),
            html.P("계절별 위험 예보 · 만기 임박 계약 · 사전 점검 체크리스트",
                   style={"color":"#888","fontSize":"0.85rem","marginBottom":"20px"}),
        ]),

        kpi_row,

        dbc.Row([
            # 좌: 히트맵
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("계절별 위험 강도 히트맵"),
                    html.P("수치가 높을수록 해당 월의 위험 발생 빈도·강도가 높음",
                           style={"fontSize":"0.78rem","color":"#888","marginBottom":"8px"}),
                    dcc.Graph(figure=heatmap_fig, config={"displayModeBar": False}),
                ]), style=CARD_STYLE),
            ], md=8),
            # 우: 이달 체크리스트
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header(f"{MONTHS[cur_m-1]} 사전 점검 체크리스트"),
                    html.P("이번 달 집중 관리 필요 항목", style={"fontSize":"0.78rem","color":"#888","marginBottom":"10px"}),
                    checklist_ui,
                ]), style=CARD_STYLE),
            ], md=4),
        ], className="mb-3"),

        dbc.Row([
            # 좌: 만기 계약 월별 바 차트
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("향후 12개월 만기 계약 현황"),
                    html.P("보종별 만기 건수 — 빨간 점선 구간(태풍 시즌 6~9월)과 겹치는 계약 집중 관리",
                           style={"fontSize":"0.78rem","color":"#888","marginBottom":"8px"}),
                    dcc.Graph(figure=expiry_fig, config={"displayModeBar": False}),
                ]), style=CARD_STYLE),
            ], md=7),
            # 우: 만기 임박 상세 리스트
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("6개월 내 만기 계약 상세"),
                    expiry_table,
                ]), style=CARD_STYLE),
            ], md=5),
        ], className="mb-3"),

        # ── 포트폴리오 집중도 분석 ────────────────────────────────────────────
        dbc.Card(dbc.CardBody([
            section_header("포트폴리오 리스크 집중도 분석"),
            html.P("특정 지역·업종에 가입액이 집중될수록 대형 재난 시 누적 손해 위험 증가",
                   style={"fontSize":"0.78rem","color":"#888","marginBottom":"12px"}),
            dbc.Row([
                dbc.Col([
                    html.Div("지역별 가입액",
                             style={"fontSize":"0.82rem","fontWeight":"700","color":"#444","marginBottom":"6px"}),
                    dcc.Graph(figure=region_fig, config={"displayModeBar": False}),
                ], md=4),
                dbc.Col([
                    html.Div("보종별 가입액",
                             style={"fontSize":"0.82rem","fontWeight":"700","color":"#444","marginBottom":"6px"}),
                    dcc.Graph(figure=donut_lob, config={"displayModeBar": False}),
                ], md=4),
                dbc.Col([
                    html.Div("업종별 가입액",
                             style={"fontSize":"0.82rem","fontWeight":"700","color":"#444","marginBottom":"6px"}),
                    dcc.Graph(figure=donut_ind, config={"displayModeBar": False}),
                ], md=4),
            ]),
        ]), style=CARD_STYLE),
    ])


# ── 페이지 4: 해상 위험관리 ──────────────────────────────────────────────────
def layout_marine() -> html.Div:
    is_live = bool(VF_API_KEY)
    source_badge = (
        dbc.Badge("● AIS LIVE", color="success", style={"fontSize":"0.78rem","padding":"5px 10px","marginLeft":"10px"})
        if is_live else
        dbc.Badge("더미 데이터", color="secondary", style={"fontSize":"0.78rem","padding":"5px 10px","marginLeft":"10px"})
    )

    total_exposure = int(pd.DataFrame(DUMMY_CONTRACTS_VESSEL)["insured_amount_krw"].sum())
    init_kpi = html.Div(
        [
            html.Div(kpi_card("계약 선박",     f"{len(DUMMY_VESSELS)}척",             "#2980b9", "fa-ship"),           style={"minWidth":"220px","flex":"1"}),
            html.Div(kpi_card("계약사",        f"{len(COMPANY_COLORS)}개사",           "#e67e22", "fa-building"),       style={"minWidth":"220px","flex":"1"}),
            html.Div(kpi_card("해상보험 계약", f"{len(DUMMY_CONTRACTS_VESSEL)}건",     "#8e44ad", "fa-file-contract"),  style={"minWidth":"220px","flex":"1"}),
            html.Div(kpi_card("해상 총 노출",  f"{total_exposure/1e8:.0f}억 원",       "#27ae60", "fa-won-sign"),       style={"minWidth":"220px","flex":"1"}),
        ],
        style={"display":"flex","gap":"10px","flexWrap":"nowrap","overflowX":"auto"},
    )

    init_status = dbc.Alert([
        html.I(className="fa-solid fa-satellite-dish me-2"),
        "AIS 연결 중... — 잠시만 기다려주세요",
    ], color="warning", style={"padding":"8px 14px","fontSize":"0.82rem","marginBottom":"0","borderRadius":"8px"})

    # 계약자 필터 버튼
    company_filter_btns = html.Div([
        html.Span("계약자 필터:", style={"fontSize":"0.83rem","fontWeight":"600","color":"#444","marginRight":"8px","lineHeight":"32px"}),
        *[
            dbc.Button(
                company,
                id={"type":"marine-company-btn","index":company},
                color="light",
                size="sm",
                style={
                    "marginRight":"6px","fontWeight":"600","fontSize":"0.8rem",
                    "borderColor": COMPANY_COLORS.get(company, "#ccc") if company != "전체" else "#1a2942",
                    "color": COMPANY_COLORS.get(company, "#1a2942") if company != "전체" else "#1a2942",
                },
            ) for company in COMPANIES
        ],
    ], style={"display":"flex","alignItems":"center","flexWrap":"wrap","gap":"2px","marginBottom":"12px"})

    # MMSI 실시간 조회 박스
    mmsi_search_box = dbc.Card(dbc.CardBody([
        html.Div([
            html.Div([
                html.Label(
                    [html.I(className="fa-solid fa-magnifying-glass me-2"),
                     "선박 조회"],
                    style={"fontSize":"0.85rem","fontWeight":"700","color":"#1a2942","marginBottom":"4px","display":"block"}
                ),
                html.P(
                    "IMO No.(7자리) · 선박명 → 선박 정보 조회  |  MMSI(9자리) → VesselFinder 실시간 위치 추적",
                    style={"fontSize":"0.75rem","color":"#888","marginBottom":"8px"},
                ),
                html.Div([
                    dcc.Input(
                        id="marine-mmsi-input",
                        type="text",
                        placeholder="IMO No. (예: 9926738) / 선박명 (예: VL BREEZE) / MMSI",
                        debounce=False,
                        style={"flex":"1","padding":"7px 12px","borderRadius":"6px",
                               "border":"1px solid #ccc","fontSize":"0.85rem","marginRight":"8px"},
                    ),
                    dbc.Button(
                        [html.I(className="fa-solid fa-magnifying-glass me-1"), "조회"],
                        id="marine-mmsi-btn",
                        color="primary", size="sm", style={"fontWeight":"700","whiteSpace":"nowrap"},
                    ),
                ], style={"display":"flex","alignItems":"center"}),
            ]),
            html.Div(id="marine-mmsi-result", style={"marginTop":"10px"}),
        ]),
    ]), style={**CARD_STYLE, "marginBottom":"12px"})

    return html.Div([
        html.Div([
            html.Div([
                html.H4(["해상 위험관리", source_badge],
                        style={"fontWeight":"800","color":"#1a2942","margin":0,"display":"flex","alignItems":"center","gap":"8px"}),
                html.Div("실시간 AIS 선박 위치 · 계약자별 선박 조회 · 담보 공백 감지 · 자동 갱신",
                         style={"color":"#888","fontSize":"0.82rem"}),
            ]),
        ], style={"marginBottom":"16px"}),

        # AIS 연결 상태 바
        html.Div(init_status, id="ais-status-bar", style={"marginBottom":"12px"}),

        # KPI (동적)
        html.Div(init_kpi, id="marine-kpi", className="mb-3"),

        # 계약자 필터
        company_filter_btns,

        # MMSI 조회 박스
        mmsi_search_box,

        # AIS 연결 중 화면 (데이터 수신 전까지 표시)
        html.Div(
            dbc.Card(
                dbc.CardBody([
                    html.Div([
                        dbc.Spinner(color="primary", type="border", size="lg"),
                        html.Div([
                            html.Strong("AIS 연결 중...", style={"fontSize":"1.1rem","color":"#1a2942"}),
                            html.Div("실시간 선박 위치 데이터를 수집하고 있습니다. 잠시만 기다려주세요.",
                                     style={"color":"#666","fontSize":"0.85rem","marginTop":"4px"}),
                        ], style={"marginLeft":"16px"}),
                    ], style={"display":"flex","alignItems":"center","justifyContent":"center","padding":"40px 0"}),
                ]),
                style={**CARD_STYLE, "minHeight":"200px"},
            ),
            id="marine-loading-wrap",
            style={"marginBottom":"12px"},
        ),

        # 지도 + 선박 목록 (데이터 준비되면 표시)
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    html.Div([
                        section_header("실시간 선박 위치 관제 지도"),
                        html.Div([
                            *[html.Span("● " + c, style={"color": COMPANY_COLORS[c], "fontSize":"0.78rem","marginRight":"12px","fontWeight":"700"})
                              for c in COMPANY_COLORS],
                        ], style={"marginBottom":"4px"}),
                    ]),
                    dcc.Graph(
                        id="marine-map",
                        figure=go.Figure(layout=dict(
                            mapbox=dict(style=MAP_STYLE, center=dict(lat=38, lon=172), zoom=2.2),
                            margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="white",
                        )),
                        config={"displayModeBar": False, "scrollZoom": True},
                        style={"height": "700px"},
                    ),
                ]), style=CARD_STYLE),
            ], md=8),

            dbc.Col([
                dbc.Card(dbc.CardBody([
                    section_header("선박 목록 — 클릭하여 상세 조회"),
                    html.Div(
                        _build_vessel_list(VESSEL_DF, set(VESSEL_CONTRACTS_DF["imo"].astype(str).tolist()), "전체"),
                        id="marine-vessel-list",
                        style={"maxHeight":"700px","overflowY":"auto"},
                    ),
                ]), style=CARD_STYLE),
            ], md=4),
        ], className="mb-3", id="marine-main-wrap", style={"display": "none"}),

        html.Div(id="marine-detail"),

        # ── 계약 테이블 ──────────────────────────────────────────────────────
        dbc.Card(dbc.CardBody([
            section_header(f"해상보험 계약 목록 — {len(DUMMY_CONTRACTS_VESSEL)}건"),
            dbc.Table([
                html.Thead(html.Tr([
                    html.Th("계약자",      style={"fontSize":"0.8rem"}),
                    html.Th("증권번호",    style={"fontSize":"0.8rem","whiteSpace":"nowrap"}),
                    html.Th("선박 IMO",   style={"fontSize":"0.8rem"}),
                    html.Th("상품",        style={"fontSize":"0.8rem"}),
                    html.Th("보험가액",    style={"fontSize":"0.8rem"}),
                    html.Th("선체",        style={"fontSize":"0.8rem","textAlign":"center"}),
                    html.Th("화물",        style={"fontSize":"0.8rem","textAlign":"center"}),
                    html.Th("P&I",         style={"fontSize":"0.8rem","textAlign":"center"}),
                    html.Th("지연",        style={"fontSize":"0.8rem","textAlign":"center"}),
                ], style={"background":"#f8f9fa"})),
                html.Tbody([
                    html.Tr([
                        html.Td(r.get("insured_company",""), style={"fontSize":"0.8rem","fontWeight":"700","color":COMPANY_COLORS.get(r.get("insured_company",""),"#555")}),
                        html.Td(r["contract_id"], style={"fontSize":"0.8rem","fontWeight":"600","color":"#2980b9"}),
                        html.Td(r["imo"],         style={"fontSize":"0.8rem"}),
                        html.Td(r["product_name"],style={"fontSize":"0.8rem"}),
                        html.Td(f"{int(r['insured_amount_krw'])/1e8:.0f}억원", style={"fontSize":"0.8rem","fontWeight":"700","color":"#1a2942"}),
                        html.Td("✓" if r["cover_hull"]  else "—", style={"textAlign":"center","fontSize":"0.8rem","color":"#27ae60" if r["cover_hull"]  else "#ccc"}),
                        html.Td("✓" if r["cover_cargo"] else "—", style={"textAlign":"center","fontSize":"0.8rem","color":"#27ae60" if r["cover_cargo"] else "#ccc"}),
                        html.Td("✓" if r["cover_pi"]    else "—", style={"textAlign":"center","fontSize":"0.8rem","color":"#27ae60" if r["cover_pi"]    else "#ccc"}),
                        html.Td("✓" if r["cover_delay"] else "—", style={"textAlign":"center","fontSize":"0.8rem","color":"#27ae60" if r["cover_delay"] else "#ccc"}),
                    ]) for r in pd.DataFrame(DUMMY_CONTRACTS_VESSEL).to_dict("records")
                ]),
            ], bordered=False, striped=True, hover=True, size="sm", responsive=True),
        ]), style={**CARD_STYLE, "marginTop":"16px"}),

        dcc.Store(id="marine-selected-imo"),
        dcc.Store(id="marine-company-filter", data="전체"),
        dcc.Interval(id="marine-interval", interval=3_000, n_intervals=0),
    ])


def _render_vessel_info_card(info: Dict) -> html.Div:
    """VESSEL_INFO_DB 항목을 선박정보 카드로 렌더링."""
    flag_map = {"KR":"🇰🇷","PA":"🇵🇦","MH":"🇲🇭","LR":"🇱🇷","BS":"🇧🇸","SG":"🇸🇬"}
    flag_emoji = flag_map.get(info.get("flag",""), "🏳️")
    is_detained = info.get("special_status") == "억류"

    detained_alert = dbc.Alert([
        html.I(className="fa-solid fa-triangle-exclamation me-2"),
        html.Strong("⚠️ 억류 선박"),
        html.Span(f" — {info.get('special_note','')}", style={"fontSize":"0.82rem","marginLeft":"6px"}),
    ], color="danger", style={"padding":"8px 12px","fontSize":"0.83rem","marginBottom":"10px","borderRadius":"8px"}) if is_detained else None

    fields = [
        ("IMO No.",    info.get("imo","-")),
        ("MMSI",       info.get("mmsi","-")),
        ("선종",        info.get("vessel_type","-")),
        ("국적",        f"{flag_emoji} {info.get('flag_full', info.get('flag','-'))}"),
        ("총톤수(GT)",  f"{info.get('gt',0):,} GT"),
        ("재화중량(DWT)",f"{info.get('dwt',0):,} DWT"),
        ("건조연도",    f"{info.get('built','-')}년"),
        ("선급",        info.get("class_soc","-")),
        ("P&I Club",   info.get("pi_club","-")),
        ("선주",        info.get("owner","-")),
        ("운항사",      info.get("manager","-")),
    ]

    grid = html.Div([
        html.Div([
            html.Span(label, style={"fontSize":"0.75rem","color":"#888","display":"block","marginBottom":"1px"}),
            html.Span(value, style={"fontSize":"0.83rem","fontWeight":"600","color":"#1a2942"}),
        ], style={"padding":"7px 10px","background":"#f8f9fa","borderRadius":"6px"})
        for label, value in fields
    ], style={"display":"grid","gridTemplateColumns":"1fr 1fr","gap":"6px","marginBottom":"10px"})

    mmsi_btn = dbc.Button(
        [html.I(className="fa-solid fa-satellite-dish me-1"), "MMSI 실시간 추적"],
        id="marine-mmsi-btn",   # 기존 콜백 재활용
        color="primary", size="sm", style={"fontWeight":"700"},
    ) if info.get("mmsi") and VF_API_KEY else None

    children = [
        html.Div([
            html.Span(f"{flag_emoji} {info['vessel_name']}",
                      style={"fontWeight":"800","fontSize":"1.05rem","color":"#1a2942"}),
            dbc.Badge("억류", color="danger", pill=True, style={"marginLeft":"8px","fontSize":"0.75rem"}) if is_detained else None,
        ], style={"marginBottom":"8px","display":"flex","alignItems":"center"}),
    ]
    if detained_alert:
        children.append(detained_alert)
    children.append(grid)
    if mmsi_btn:
        children.append(mmsi_btn)

    return html.Div(children, style={"padding":"2px 0"})


def _current_vessel_df() -> pd.DataFrame:
    """해상 데모 고정: 더미 선박 70척만 사용"""
    return VESSEL_DF


def _linked_contracts_for_vessels(vessel_df: pd.DataFrame) -> pd.DataFrame:
    """실시간 선박에도 일부 데모 계약을 자동 매핑해 연결 시나리오를 보여준다."""
    linked = VESSEL_CONTRACTS_DF.copy()
    if vessel_df.empty:
        return linked

    v = vessel_df.copy()
    imo_series = v["imo"].astype(str).str.strip() if "imo" in v.columns else pd.Series([""] * len(v), index=v.index)
    mmsi_series = v["mmsi"].astype(str).str.strip() if "mmsi" in v.columns else pd.Series([""] * len(v), index=v.index)
    id_key = imo_series.mask(imo_series.eq(""), mmsi_series)
    keys = [k for k in id_key.astype(str).tolist() if k and k != "0" and k.lower() != "nan"]
    existing = set(linked["imo"].astype(str).tolist())
    missing = [k for k in keys if k not in existing]
    if not missing:
        return linked

    templates = VESSEL_CONTRACTS_DF.to_dict("records")
    add_rows: List[Dict[str, Any]] = []
    for i, key in enumerate(missing[:200]):  # 데모용 최대 200척
        t = dict(templates[i % len(templates)])
        t["imo"] = key
        t["contract_id"] = f"{t['contract_id']}-AUTO{i+1:02d}"
        add_rows.append(t)
    if add_rows:
        linked = pd.concat([linked, pd.DataFrame(add_rows)], ignore_index=True)
    return linked


def _contract_popup_lines(imo: str, linked_contracts_df: pd.DataFrame) -> str:
    """해당 IMO의 계약 요약 HTML 문자열 반환"""
    contracts = linked_contracts_df[linked_contracts_df["imo"] == imo]
    if contracts.empty:
        return ""
    lines = ["<b>─ 연결 계약 ─</b>"]
    for _, c in contracts.iterrows():
        covers = []
        if c.get("cover_hull"):  covers.append("선체")
        if c.get("cover_cargo"): covers.append("화물")
        if c.get("cover_pi"):    covers.append("P&I")
        if c.get("cover_delay"): covers.append("지연")
        amt = int(c["insured_amount_krw"]) / 1e8
        lines.append(
            f"{c['contract_id']}<br>"
            f"  {c['product_name']}  {amt:.0f}억원<br>"
            f"  담보: {' / '.join(covers) if covers else '없음'}"
        )
    return "<br>".join(lines)


def _build_marine_map(selected_imo: Optional[str], vessel_df: pd.DataFrame, company_filter: str = "전체") -> go.Figure:
    """계약사별 색상 마커, 호버에 계약 정보 포함"""
    fig = go.Figure()
    if vessel_df.empty:
        vessel_df = VESSEL_DF
    df = vessel_df.copy()
    df["lat"] = pd.to_numeric(df.get("lat"), errors="coerce")
    df["lon"] = pd.to_numeric(df.get("lon"), errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    if df.empty:
        return go.Figure()

    # id_key: imo 우선, 없으면 mmsi
    imo_col  = df["imo"].astype(str)  if "imo"  in df.columns else pd.Series([""] * len(df), index=df.index)
    mmsi_col = df["mmsi"].astype(str) if "mmsi" in df.columns else pd.Series([""] * len(df), index=df.index)
    df["id_key"] = imo_col.where(imo_col.str.strip() != "", mmsi_col)
    df["name"]   = df.get("vessel_name", pd.Series([""] * len(df), index=df.index)).astype(str)
    df["name"]   = df["name"].where(df["name"].str.strip() != "", df["id_key"])
    df["sog"]    = pd.to_numeric(df.get("sog", 0), errors="coerce").fillna(0)
    df["company"] = df.get("insured_company", pd.Series(["기타"] * len(df), index=df.index)).fillna("기타")
    df["route"]   = df.get("route", pd.Series([""] * len(df), index=df.index)).fillna("")

    # 계약 정보 합치기 (imo 기준)
    contracts_df = VESSEL_CONTRACTS_DF.copy()
    imo_to_contracts: Dict[str, str] = {}
    for imo_val, grp in contracts_df.groupby("imo"):
        lines = []
        for _, c in grp.iterrows():
            covers = [lbl for col, lbl in [("cover_hull","선체"),("cover_cargo","화물"),("cover_pi","P&I"),("cover_delay","지연")] if c.get(col)]
            amt = int(c["insured_amount_krw"]) / 1e8
            lines.append(f"  [{c['contract_id']}]  {c['product_name']}  {amt:.0f}억원  담보: {'/'.join(covers) if covers else '없음'}")
        imo_to_contracts[str(imo_val)] = "<br>".join(lines)

    def _hover(row: pd.Series) -> str:
        contract_txt = imo_to_contracts.get(str(row["id_key"]), "")
        base = (f"<b>{row['name']}</b><br>"
                f"계약사: {row['company']}<br>"
                f"항로: {row['route']}<br>"
                f"SOG: {row['sog']:.1f}kn")
        if contract_txt:
            base += "<br><b>── 계약 ──</b><br>" + contract_txt
        return base

    df["hover"] = df.apply(_hover, axis=1)

    # 계약사 필터 적용
    if company_filter and company_filter != "전체":
        df = df[df["company"] == company_filter]

    # 태평양 중심 고정 (전 선박이 태평양에 분포)
    center_lat, center_lon = 38.0, 172.0

    # ── 항로선 (대권항로 곡선 점선, 출발항→목적항, 회사별 색상, 중복 제거) ────────
    for company, color in COMPANY_COLORS.items():
        sub = df[df["company"] == company]
        if sub.empty:
            continue
        r_lats: List = []
        r_lons: List = []
        seen_routes: set = set()
        for _, row in sub.iterrows():
            route_str = str(row.get("route", "")).strip()
            if not route_str or route_str in seen_routes:
                continue
            seen_routes.add(route_str)
            orig = _origin_coords(route_str)
            dest = _dest_coords(route_str)
            if not orig or not dest:
                continue
            arc_lats, arc_lons = _route_arc_dashed(
                orig[0], orig[1], dest[0], dest[1],
                n=60, dash_on=5, dash_off=3,
            )
            r_lats.extend(arc_lats + [None])
            r_lons.extend(arc_lons + [None])
        if r_lats:
            fig.add_trace(go.Scattermapbox(
                lat=r_lats, lon=r_lons,
                mode="lines",
                line=dict(width=1.8, color=color),
                opacity=0.50,
                hoverinfo="skip",
                showlegend=False,
            ))

    # ── 계약사별 선박 마커 (흰 테두리 원형, AIS 스타일) ─────────────────────────
    for company, color in COMPANY_COLORS.items():
        sub = df[df["company"] == company]
        if sub.empty:
            continue
        fig.add_trace(go.Scattermapbox(
            lat=sub["lat"].tolist(), lon=sub["lon"].tolist(),
            mode="markers",
            marker=dict(
                size=11,
                color=color,
                opacity=0.95,
                allowoverlap=True,
            ),
            customdata=sub["hover"].tolist(),
            hovertemplate="%{customdata}<extra></extra>",
            name=company,
            showlegend=True,
        ))

    # 계약사 없는 선박 (MMSI 직접 조회 등)
    df_other = df[~df["company"].isin(COMPANY_COLORS)]
    if not df_other.empty:
        fig.add_trace(go.Scattermapbox(
            lat=df_other["lat"].tolist(), lon=df_other["lon"].tolist(),
            mode="markers",
            marker=dict(size=10, color="#555555", opacity=0.9, allowoverlap=True),
            customdata=df_other["hover"].tolist(),
            hovertemplate="%{customdata}<extra></extra>",
            name="기타",
            showlegend=True,
        ))

    # 억류·특수상황 선박 (VESSEL_INFO_DB 기준) — 빨간 ⚠ 마커
    detained = [v for v in VESSEL_INFO_DB.values()
                if v.get("special_status") == "억류" and v.get("lat") is not None]
    if detained:
        fig.add_trace(go.Scattermapbox(
            lat=[v["lat"] for v in detained],
            lon=[v["lon"] for v in detained],
            mode="markers",
            marker=dict(size=18, color="#e74c3c", opacity=1.0, allowoverlap=True),
            customdata=[
                f"<b>⚠️ {v['vessel_name']}</b> [억류]<br>"
                f"IMO: {v['imo']}  |  MMSI: {v['mmsi']}<br>"
                f"{v.get('special_note','')}"
                for v in detained
            ],
            hovertemplate="%{customdata}<extra></extra>",
            name="⚠️ 억류 선박",
            showlegend=True,
        ))
        # 펄싱 효과용 큰 반투명 원
        fig.add_trace(go.Scattermapbox(
            lat=[v["lat"] for v in detained],
            lon=[v["lon"] for v in detained],
            mode="markers",
            marker=dict(size=36, color="#e74c3c", opacity=0.20, allowoverlap=True),
            hoverinfo="skip", showlegend=False,
        ))

    # 선택 선박 강조 — 노란 원형 링
    if selected_imo:
        sel = df[df["id_key"] == str(selected_imo)]
        if not sel.empty:
            fig.add_trace(go.Scattermapbox(
                lat=sel["lat"].tolist(), lon=sel["lon"].tolist(),
                mode="markers",
                marker=dict(size=34, color="#f39c12", opacity=0.45),
                hoverinfo="skip", showlegend=False,
            ))

    fig.update_layout(
        mapbox=dict(style=MAP_STYLE, center=dict(lat=center_lat, lon=center_lon), zoom=2.6),
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11), bgcolor="rgba(255,255,255,0.85)"),
        uirevision="marine-live",
    )
    return fig


def _build_vessel_list(vessel_df: pd.DataFrame, contract_imos: set, company_filter: str = "전체") -> html.Div:
    if vessel_df.empty:
        return dbc.Alert("선박 데이터 없음", color="secondary")
    df = vessel_df.copy()
    if company_filter and company_filter != "전체" and "insured_company" in df.columns:
        df = df[df["insured_company"] == company_filter]
    if df.empty:
        return dbc.Alert(f"{company_filter} 소속 선박 없음", color="secondary")
    items = []
    for _, r in df.head(80).iterrows():
        imo     = str(r.get("imo","") or r.get("mmsi",""))
        name    = str(r.get("vessel_name","") or imo)
        sog     = r.get("sog", 0.0)
        vtype   = r.get("vessel_type","기타")
        rlv     = r.get("risk_level","NORMAL")
        company = str(r.get("insured_company",""))
        ccolor  = COMPANY_COLORS.get(company, "#888")
        items.append(
            dbc.ListGroupItem([
                html.Div([
                    html.Div([
                        html.I(className="fa-solid fa-ship me-2", style={"color": ccolor}),
                        html.Strong(name[:22], style={"fontSize":"0.85rem"}),
                    ]),
                    risk_badge(rlv),
                ], style={"display":"flex","justifyContent":"space-between","alignItems":"center"}),
                html.Div([
                    html.Span(company, style={"fontSize":"0.7rem","fontWeight":"700","color":ccolor,"marginRight":"6px"}),
                    html.Span(f"IMO {r.get('imo','')}" if r.get("imo") else f"MMSI {imo}",
                              style={"fontSize":"0.72rem","color":"#888","marginRight":"6px"}),
                    html.Span(vtype, style={"fontSize":"0.72rem","color":"#555","marginRight":"6px"}),
                    html.Span(f"SOG {sog}kn", style={"fontSize":"0.72rem","color":"#2980b9","fontWeight":"700"}),
                ], style={"marginTop":"3px"}),
            ], id={"type":"marine-vessel-item","index":imo},
               action=True, style={"cursor":"pointer","padding":"8px 12px"})
        )
    return html.Div(items)


def _make_vessel_detail(vrow: pd.Series, imo: str, linked_contracts_df: pd.DataFrame) -> html.Div:
    contracts = linked_contracts_df[linked_contracts_df["imo"] == imo]
    has_hull  = bool(contracts["cover_hull"].any())  if not contracts.empty else False
    has_pi    = bool(contracts["cover_pi"].any())    if not contracts.empty else False
    sog = float(vrow.get("sog", 0.0))

    gaps, actions = [], []
    if sog >= 16 and not has_pi:
        gaps.append("고속 항해 중 P&I 담보 없음 — 충돌 배상 리스크")
        actions.append("속력 감속 및 P&I 추가 가입 검토")
    if sog >= 13 and not has_hull:
        gaps.append("선체 담보 없음 — 선박 손상 리스크 노출")
        actions.append("선체보험 긴급 가입 검토")
    if not gaps:
        gaps.append("현재 리스크 기준 담보 공백 없음")
        actions.append("현 항로 유지, 30분 간격 모니터링")

    # 데모 모드: 실시간 선박과 계약을 동적 매칭하지 않고, 없으면 더미 상위 계약을 보여준다.
    show_contracts = contracts if not contracts.empty else linked_contracts_df.head(3)
    if show_contracts.empty:
        pol_table = dbc.Alert("표시할 더미 계약이 없습니다.", color="warning")
    else:
        pol_rows = []
        for _, c in show_contracts.iterrows():
            icons = []
            for col, lbl in [("cover_hull","선체"),("cover_cargo","화물"),("cover_pi","P&I"),("cover_delay","지연")]:
                bg = "#27ae60" if c[col] else "#e0e0e0"
                icons.append(html.Span(lbl, style={"background":bg,"color":"#fff" if c[col] else "#aaa",
                    "borderRadius":"4px","padding":"1px 7px","fontSize":"0.72rem","marginRight":"3px","fontWeight":"600"}))
            pol_rows.append(html.Tr([
                html.Td(c["contract_id"],style={"fontSize":"0.8rem","fontWeight":"700"}),
                html.Td(c["product_name"],style={"fontSize":"0.8rem"}),
                html.Td(f"{int(c['insured_amount_krw'])/1e8:.0f}억",style={"fontSize":"0.8rem","color":"#2980b9","fontWeight":"700"}),
                html.Td(icons),
            ]))
        header_note = "선택 선박 매칭 계약" if not contracts.empty else "데모 고정 계약(비매칭)"
        pol_table = html.Div([
            html.Div(header_note, style={"fontSize":"0.75rem","color":"#888","marginBottom":"6px"}),
            dbc.Table([
            html.Thead(html.Tr([html.Th("증권번호"),html.Th("상품"),html.Th("보험가액"),html.Th("담보")])),
            html.Tbody(pol_rows),
            ], bordered=False, striped=True, size="sm", hover=True),
        ])

    name = str(vrow.get("vessel_name","") or imo)
    return dbc.Card(dbc.CardBody([
        section_header(f"선택 선박 상세 — {name}"),
        dbc.Row([
            dbc.Col([
                dbc.Table([html.Tbody([
                    html.Tr([html.Td("선박명",style={"color":"#666","fontSize":"0.83rem"}),   html.Td(name,style={"fontWeight":"700","fontSize":"0.83rem"})]),
                    html.Tr([html.Td("IMO / MMSI",style={"color":"#666","fontSize":"0.83rem"}),html.Td(f"{vrow.get('imo','')} / {vrow.get('mmsi',imo)}",style={"fontSize":"0.83rem"})]),
                    html.Tr([html.Td("종류",style={"color":"#666","fontSize":"0.83rem"}),      html.Td(str(vrow.get("vessel_type","기타")),style={"fontSize":"0.83rem"})]),
                    html.Tr([html.Td("SOG",style={"color":"#666","fontSize":"0.83rem"}),       html.Td(f"{sog}kn",style={"fontWeight":"700","color":"#e74c3c" if sog>=16 else "#333","fontSize":"0.83rem"})]),
                    html.Tr([html.Td("마지막 수신",style={"color":"#666","fontSize":"0.83rem"}),html.Td(str(vrow.get("last_fix","-"))[:16],style={"fontSize":"0.83rem"})]),
                    html.Tr([html.Td("위험 등급",style={"color":"#666","fontSize":"0.83rem"}), html.Td(risk_badge(str(vrow.get("risk_level","NORMAL"))))]),
                ])], borderless=True, size="sm"),
            ], md=3),
            dbc.Col([
                html.Div("연결 증권", style={"fontWeight":"700","marginBottom":"8px","color":"#1a2942","fontSize":"0.9rem"}),
                pol_table,
            ], md=5),
            dbc.Col([
                html.Div("담보 공백 · 권고 행동", style={"fontWeight":"700","marginBottom":"8px","color":"#1a2942","fontSize":"0.9rem"}),
                dbc.Alert([
                    html.Ul([html.Li(g,style={"fontSize":"0.82rem"}) for g in gaps],style={"paddingLeft":"16px","marginBottom":"0"}),
                ], color="danger" if any("없음" not in g for g in gaps) else "success",
                   style={"padding":"10px","borderRadius":"8px","marginBottom":"10px"}),
                html.Ul([html.Li(a,style={"fontSize":"0.82rem","marginBottom":"4px"}) for a in actions],
                        style={"paddingLeft":"16px"}),
            ], md=4),
        ]),
    ]), style=CARD_STYLE, className="mt-3")


# ── 계약사 필터 버튼 콜백 ────────────────────────────────────────────────────
@callback(
    Output("marine-company-filter", "data"),
    Input({"type":"marine-company-btn","index":dash.ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def update_company_filter(n_clicks_list: List):
    import json as _json
    ctx = dash.callback_context
    if not ctx.triggered:
        return "전체"
    triggered = ctx.triggered[0]["prop_id"]
    try:
        return _json.loads(triggered.split(".")[0])["index"]
    except Exception:
        return "전체"


# ── MMSI 실시간 조회 콜백 ─────────────────────────────────────────────────────
@callback(
    Output("marine-mmsi-result", "children"),
    Input("marine-mmsi-btn", "n_clicks"),
    State("marine-mmsi-input", "value"),
    prevent_initial_call=True,
)
def lookup_mmsi(n_clicks: Any, mmsi_value: Optional[str]):
    """통합 선박 조회: IMO No. → DB 조회 / 선박명 → 부분 검색 / MMSI → 실시간 추적"""
    if not mmsi_value or not mmsi_value.strip():
        return dbc.Alert("IMO No., 선박명, 또는 MMSI를 입력해주세요.",
                         color="warning", style={"padding":"6px 10px","fontSize":"0.82rem"})
    q = mmsi_value.strip()

    # ── 1. IMO No. 검색 (7자리 숫자) ─────────────────────────────────────────
    if q.isdigit() and len(q) == 7:
        info = VESSEL_INFO_DB.get(q)
        if info:
            return _render_vessel_info_card(info)
        # DUMMY_VESSELS에서도 검색
        dummy_match = next((v for v in DUMMY_VESSELS if str(v.get("imo","")) == q), None)
        if dummy_match:
            return dbc.Alert([
                html.I(className="fa-solid fa-ship me-2"),
                html.Strong(dummy_match["vessel_name"]),
                f"  |  IMO {q}  |  {dummy_match['vessel_type']}  |  항로: {dummy_match['route']}",
            ], color="info", style={"padding":"8px 12px","fontSize":"0.83rem"})
        return dbc.Alert(f"IMO {q}: 등록된 선박 없음 (Equasis 연동 예정)",
                         color="secondary", style={"padding":"6px 10px","fontSize":"0.82rem"})

    # ── 2. 선박명 검색 (문자 포함 또는 숫자 아닌 경우) ──────────────────────
    if not q.isdigit():
        q_up = q.upper()
        db_matches   = [v for v in VESSEL_INFO_DB.values() if q_up in v["vessel_name"].upper()]
        dummy_matches = [v for v in DUMMY_VESSELS if q_up in v["vessel_name"].upper()]

        if len(db_matches) == 1 and not dummy_matches:
            return _render_vessel_info_card(db_matches[0])

        result_items = []
        for m in db_matches:
            detained = m.get("special_status") == "억류"
            result_items.append(html.Div([
                html.Div([
                    html.Span(m["vessel_name"], style={"fontWeight":"700","fontSize":"0.88rem","color":"#1a2942"}),
                    dbc.Badge("억류", color="danger", pill=True, style={"marginLeft":"6px","fontSize":"0.72rem"}) if detained else None,
                ], style={"display":"flex","alignItems":"center"}),
                html.Div(
                    f"IMO {m['imo']}  |  MMSI {m['mmsi']}  |  {m['vessel_type']}  |  {m['flag_full']}  |  {m['gt']:,} GT  |  {m['built']}년 건조",
                    style={"fontSize":"0.78rem","color":"#555","marginTop":"2px"},
                ),
                dbc.Alert(m["special_note"], color="danger",
                          style={"padding":"4px 10px","fontSize":"0.78rem","marginTop":"4px","marginBottom":"0"}) if detained else None,
            ], style={"padding":"10px 12px","border":"1px solid #eee","borderRadius":"8px",
                      "marginBottom":"6px","background":"#fef6f6" if detained else "#f8f9fa",
                      "borderLeft":"4px solid #e74c3c" if detained else "4px solid #2980b9"}))

        for m in dummy_matches[:5]:
            result_items.append(html.Div([
                html.Span(m["vessel_name"], style={"fontWeight":"700","fontSize":"0.88rem","color":"#1a2942"}),
                html.Div(f"IMO {m['imo']}  |  {m['vessel_type']}  |  항로: {m['route']}",
                         style={"fontSize":"0.78rem","color":"#555","marginTop":"2px"}),
            ], style={"padding":"10px 12px","border":"1px solid #eee","borderRadius":"8px",
                      "marginBottom":"6px","background":"#f8f9fa",
                      "borderLeft":"4px solid #27ae60"}))

        if not result_items:
            return dbc.Alert(f"'{q}': 일치하는 선박 없음",
                             color="secondary", style={"padding":"6px 10px","fontSize":"0.82rem"})

        return html.Div([
            html.Div(f"검색 결과 {len(db_matches)+len(dummy_matches)}건",
                     style={"fontSize":"0.8rem","color":"#888","marginBottom":"8px"}),
            *result_items,
        ])

    # ── 3. MMSI 검색 (9자리 숫자) → VesselFinder 실시간 추적 ────────────────
    if q.isdigit() and len(q) == 9:
        # DB에서 선박명 먼저 확인
        db_info = next((v for v in VESSEL_INFO_DB.values() if v.get("mmsi") == q), None)

        if not VF_API_KEY:
            if db_info:
                return html.Div([
                    dbc.Alert("VesselFinder API 미연결 — DB 정보만 표시",
                              color="warning", style={"padding":"6px 10px","fontSize":"0.8rem","marginBottom":"8px"}),
                    _render_vessel_info_card(db_info),
                ])
            return dbc.Alert("MMSI 조회: VesselFinder API 키 미설정",
                             color="secondary", style={"padding":"6px 10px","fontSize":"0.82rem"})

        data = _fetch_vessel_vf(q)
        if not data:
            err = _ais_status.get("last_error", "응답 없음")
            fallback = _render_vessel_info_card(db_info) if db_info else None
            return html.Div([
                dbc.Alert(f"실시간 조회 실패: {err}",
                          color="danger", style={"padding":"6px 10px","fontSize":"0.8rem","marginBottom":"8px"}),
                fallback,
            ] if fallback else [dbc.Alert(f"조회 실패: {err}", color="danger",
                                          style={"padding":"6px 10px","fontSize":"0.8rem"})])

        lat = data.get("latitude")
        lon = data.get("longitude")
        name = data.get("vesselName", "(이름 없음)")
        if lat is None:
            return dbc.Alert(f"MMSI {q}: 위치 정보 없음 (선박명: {name})",
                             color="warning", style={"padding":"6px 10px","fontSize":"0.82rem"})

        global _mmsi_tracking_enabled
        _mmsi_tracking_enabled = True
        with _ais_lock:
            _ais_store[q] = {
                "mmsi": q, "vessel_name": str(name).strip(),
                "imo": str(data.get("imo","")).strip(),
                "vessel_type": str(data.get("vesselType","기타")),
                "flag": str(data.get("flag","")),
                "lat": round(float(lat),5), "lon": round(float(lon),5),
                "sog": float(data.get("speedKnots") or 0.0),
                "last_fix": str(data.get("updatedAt",""))[:16].replace("T"," "),
                "area": str(data.get("area","")), "status": str(data.get("status","")),
                "insured_company": "기타",
            }
        sog = float(data.get("speedKnots") or 0.0)
        return dbc.Alert([
            html.I(className="fa-solid fa-satellite-dish me-2", style={"color":"#27ae60"}),
            html.Strong(f"{name}"),
            f"  MMSI {q}  |  {float(lat):.3f}°N {float(lon):.3f}°E  |  SOG {sog:.1f}kn → 지도 추가됨",
        ], color="success", style={"padding":"8px 12px","fontSize":"0.83rem"})

    return dbc.Alert("입력 형식을 확인해주세요. IMO(7자리) · 선박명 · MMSI(9자리)",
                     color="secondary", style={"padding":"6px 10px","fontSize":"0.82rem"})


# ── 자동 갱신 콜백 ────────────────────────────────────────────────────────────
@callback(
    Output("ais-status-bar",    "children"),
    Output("marine-kpi",        "children"),
    Output("marine-map",        "figure"),
    Output("marine-vessel-list","children"),
    Output("marine-loading-wrap","style"),
    Output("marine-main-wrap",  "style"),
    Input("marine-interval",    "n_intervals"),
    Input({"type":"marine-vessel-item","index":dash.ALL}, "n_clicks"),
    State("marine-selected-imo","data"),
    State("marine-company-filter","data"),
    prevent_initial_call=False,
)
def refresh_marine(n_intervals: int, vessel_clicks: List, selected_imo: Optional[str], company_filter: Optional[str]):
    import json as _json
    company_filter = company_filter or "전체"

    try:
        ctx = dash.callback_context
        if ctx.triggered:
            triggered_id = ctx.triggered[0]["prop_id"]
            if "marine-vessel-item" in triggered_id:
                try:
                    selected_imo = _json.loads(triggered_id.split(".")[0])["index"]
                except Exception:
                    pass

        vdf_full = _current_vessel_df()
        vdf = vdf_full.head(200) if len(vdf_full) > 200 else vdf_full

        # ── AIS 상태 바 ──────────────────────────────────────────────────────────
        if VF_API_KEY:
            connected = _ais_status.get("connected", False)
            count     = _ais_status.get("count", 0)
            last_msg  = _ais_status.get("last_msg") or "-"
            last_err  = _ais_status.get("last_error") or ""
            last_try  = _ais_status.get("last_poll_try") or "-"
            if connected or count > 0:
                status_bar = dbc.Alert([
                    html.I(className="fa-solid fa-ship me-2"),
                    html.Strong("VesselFinder 연결됨 "),
                    f"| 수신 선박: {count:,}척 | 마지막 조회: {last_msg} | {VF_POLL_INTERVAL//60}분 자동 갱신",
                ], color="success", style={"padding":"8px 14px","fontSize":"0.82rem","marginBottom":"0","borderRadius":"8px"})
            else:
                status_bar = dbc.Alert([
                    html.I(className="fa-solid fa-ship me-2"),
                    f"VesselFinder 조회 중... (마지막 시도: {last_try}) ",
                    html.Small(last_err[:100] if last_err else "", style={"opacity":0.8}),
                ], color="warning", style={"padding":"8px 14px","fontSize":"0.82rem","marginBottom":"0","borderRadius":"8px"})
        else:
            status_bar = dbc.Alert(
                "API 키 미설정 — 더미 데이터 표시 중",
                color="secondary", style={"padding":"8px 14px","fontSize":"0.82rem","marginBottom":"0","borderRadius":"8px"},
            )

        # KPI: 계약사 필터에 맞게 계산
        total_exposure = int(VESSEL_CONTRACTS_DF["insured_amount_krw"].sum())
        kpi = html.Div(
            [
                html.Div(kpi_card("계약 선박",     f"{len(DUMMY_VESSELS)}척",         "#2980b9", "fa-ship"),          style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("계약사",        f"{len(COMPANY_COLORS)}개사",       "#e67e22", "fa-building"),      style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("해상보험 계약", f"{len(DUMMY_CONTRACTS_VESSEL)}건", "#8e44ad", "fa-file-contract"), style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("해상 총 노출",  f"{total_exposure/1e8:.0f}억 원",   "#27ae60", "fa-won-sign"),      style={"minWidth":"220px","flex":"1"}),
            ],
            style={"display":"flex","gap":"10px","flexWrap":"nowrap","overflowX":"auto"},
        )

        # ── 지도 / 목록 ──────────────────────────────────────────────────────────
        contract_imos = set(VESSEL_CONTRACTS_DF["imo"].astype(str).tolist())
        fig   = _build_marine_map(selected_imo, vdf, company_filter)
        vlist = _build_vessel_list(vdf, contract_imos, company_filter)

        ready = len(vdf) > 0
        loading_style = {"marginBottom":"12px"} if not ready else {"display":"none"}
        main_style    = {"display":"none"} if not ready else {"display":"flex"}
        return status_bar, kpi, fig, vlist, loading_style, main_style

    except Exception as e:
        status_bar = dbc.Alert(
            f"렌더링 오류(더미 표시): {type(e).__name__}: {str(e)[:120]}",
            color="warning",
            style={"padding":"8px 14px","fontSize":"0.82rem","marginBottom":"0","borderRadius":"8px"},
        )
        total_exposure = int(VESSEL_CONTRACTS_DF["insured_amount_krw"].sum())
        kpi = html.Div(
            [
                html.Div(kpi_card("계약 선박",     f"{len(VESSEL_DF)}척",             "#2980b9", "fa-ship"),          style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("계약사",        f"{len(COMPANY_COLORS)}개사",       "#e67e22", "fa-building"),      style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("해상보험 계약", f"{len(DUMMY_CONTRACTS_VESSEL)}건", "#8e44ad", "fa-file-contract"), style={"minWidth":"220px","flex":"1"}),
                html.Div(kpi_card("해상 총 노출",  f"{total_exposure/1e8:.0f}억 원",   "#27ae60", "fa-won-sign"),      style={"minWidth":"220px","flex":"1"}),
            ],
            style={"display":"flex","gap":"10px","flexWrap":"nowrap","overflowX":"auto"},
        )
        fig = _build_marine_map(None, VESSEL_DF, "전체")
        vlist = _build_vessel_list(VESSEL_DF, set(VESSEL_CONTRACTS_DF["imo"].astype(str).tolist()), "전체")
        return status_bar, kpi, fig, vlist, {"marginBottom":"12px"}, {"display":"none"}


@callback(
    Output("marine-detail",       "children"),
    Output("marine-selected-imo", "data"),
    Input({"type":"marine-vessel-item","index":dash.ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def marine_detail(n_clicks_list: List):
    import json as _json
    ctx = dash.callback_context
    if not ctx.triggered:
        return "", None
    triggered = ctx.triggered[0]["prop_id"]
    imo = _json.loads(triggered.split(".")[0])["index"]

    vdf = _current_vessel_df()
    id_col = "imo" if "imo" in vdf.columns else "mmsi"
    matched = vdf[vdf[id_col].astype(str) == str(imo)]
    if matched.empty:
        return dbc.Alert(f"해당 선박이 현재 수신 목록에서 제외되었습니다: {imo}", color="warning"), imo

    return _make_vessel_detail(matched.iloc[0], imo, VESSEL_CONTRACTS_DF), imo


# ── 라우팅 ───────────────────────────────────────────────────────────────────
@callback(Output("page-content", "children"), Input("url", "pathname"))
def route(pathname: str):
    if pathname == "/typhoon":
        return layout_typhoon()
    elif pathname == "/marine":
        return layout_marine()
    elif pathname == "/calendar":
        return layout_calendar()
    return layout_home()


if __name__ == "__main__":
    # use_reloader=False: AIS 백그라운드 스레드 중복 실행 방지
    app.run(debug=True, port=8050, use_reloader=False)
