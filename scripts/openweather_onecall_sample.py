import json
import os
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv


def fetch_onecall(
    *,
    lat: float,
    lon: float,
    exclude: str = "minutely,daily,alerts",
    units: str = "metric",
    timeout_s: int = 20,
) -> Dict[str, Any]:
    """
    OpenWeather One Call API 3.0 호출.
    - units="metric"이면 temp는 섭씨(°C), wind_speed는 m/s.
    - rain은 시간별 예보에서 hourly[i].rain['1h'] 형태로 mm (지난 1시간 누적) 제공되는 경우가 흔함.
    """
    load_dotenv()
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "환경변수 OPENWEATHER_API_KEY가 없습니다. .env에 OPENWEATHER_API_KEY=... 를 넣어주세요."
        )

    url = "https://api.openweathermap.org/data/3.0/onecall"
    params = {
        "lat": lat,
        "lon": lon,
        "exclude": exclude,
        "units": units,
        "appid": api_key,
    }

    r = requests.get(url, params=params, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def _get_nested(d: Dict[str, Any], path: Tuple[str, ...]) -> Optional[Any]:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def summarize_units(payload: Dict[str, Any], *, units: str) -> Dict[str, Any]:
    """
    JSON에 담긴 값의 '단위'를 문서 기반으로 요약하고,
    실제 payload에서 예시 값도 같이 뽑아 보여준다.
    """
    cur_temp = _get_nested(payload, ("current", "temp"))
    cur_hum = _get_nested(payload, ("current", "humidity"))

    # hourly rain은 케이스가 다양: hourly[i].rain = {"1h": mm} 또는 rain 자체가 없을 수 있음
    hourly0 = None
    if isinstance(payload.get("hourly"), list) and payload["hourly"]:
        hourly0 = payload["hourly"][0]
    rain_1h = None
    if isinstance(hourly0, dict):
        rv = hourly0.get("rain")
        if isinstance(rv, dict) and "1h" in rv:
            rain_1h = rv["1h"]
        elif isinstance(rv, (int, float)):
            rain_1h = rv

    if units == "metric":
        temp_unit = "°C"
        wind_unit = "m/s"
    elif units == "imperial":
        temp_unit = "°F"
        wind_unit = "miles/hour"
    else:
        temp_unit = "K"
        wind_unit = "m/s"

    return {
        "assumed_units": {
            "temp": temp_unit,
            "humidity": "%",
            "rain": "mm (typically 'last 1h' if hourly.rain['1h'] exists)",
            "wind_speed": wind_unit,
        },
        "sample_values_from_payload": {
            "current.temp": cur_temp,
            "current.humidity": cur_hum,
            "hourly[0].rain(1h)": rain_1h,
        },
        "rain_note": (
            "One Call에서 '누적 강수량(예: 6시간 누적)'이 별도 컬럼으로 주어지지 않는 경우가 많아 "
            "hourly의 rain(1h)을 원하는 윈도우(6h/24h)로 직접 합산하는 방식이 일반적입니다."
        ),
    }


def main() -> None:
    # 예시 좌표: 평택시청 근처 (대략값)
    lat, lon = 36.9921, 127.1129
    units = "metric"
    payload = fetch_onecall(lat=lat, lon=lon, units=units)

    print("--- raw keys ---")
    print(list(payload.keys()))

    print("\n--- units summary ---")
    print(json.dumps(summarize_units(payload, units=units), ensure_ascii=False, indent=2))

    # current와 hourly 일부를 구조 확인용으로 출력(너무 길면 줄임)
    slim = {
        "current": payload.get("current", {}),
        "hourly_head": (payload.get("hourly") or [])[:2],
    }
    print("\n--- slim json (current + hourly first 2) ---")
    print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
