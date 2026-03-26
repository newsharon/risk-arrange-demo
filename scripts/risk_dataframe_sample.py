from __future__ import annotations

import numpy as np
import pandas as pd


def scale_linear(x: pd.Series, low: float, high: float) -> pd.Series:
    """
    x를 [low, high] 구간에서 0~1로 선형 스케일링 후 clip.
    low 이하면 0, high 이상이면 1.
    """
    return ((x - low) / (high - low)).clip(0, 1)


def compute_risk(df: pd.DataFrame) -> pd.DataFrame:
    """
    MVP용 위험 점수 예시:
    - HeatRisk = 0.7*temp_scaled + 0.3*humidity_scaled
    - FloodRisk = 0.6*rain_scaled + 0.4*rolling_6h_rain_scaled
    - QualityRisk = 0.6*humidity_scaled + 0.4*temp_scaled
    - TotalRisk = 0.4*Heat + 0.4*Flood + 0.2*Quality
    """
    out = df.copy()

    out["temp_scaled"] = scale_linear(out["temp_c"], 25, 35)
    out["humidity_scaled"] = scale_linear(out["humidity_pct"], 60, 90)
    out["rain_scaled"] = scale_linear(out["rain_mm_h"], 0, 30)

    out = out.sort_values(["factory_id", "timestamp"])
    out["rain_6h_mm"] = (
        out.groupby("factory_id")["rain_mm_h"]
        .rolling(window=6, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
    )
    out["rain_6h_scaled"] = scale_linear(out["rain_6h_mm"], 0, 80)

    out["HeatRisk"] = 100 * (0.7 * out["temp_scaled"] + 0.3 * out["humidity_scaled"])
    out["FloodRisk"] = 100 * (0.6 * out["rain_scaled"] + 0.4 * out["rain_6h_scaled"])
    out["QualityRisk"] = 100 * (0.6 * out["humidity_scaled"] + 0.4 * out["temp_scaled"])

    out["Risk_Score"] = 0.4 * out["HeatRisk"] + 0.4 * out["FloodRisk"] + 0.2 * out["QualityRisk"]

    def level(x: float) -> str:
        if x < 25:
            return "green"
        if x < 50:
            return "yellow"
        if x < 75:
            return "orange"
        return "red"

    out["Risk_Level"] = out["Risk_Score"].apply(level)
    return out


def main() -> None:
    # 가상 공장 5개
    factories = pd.DataFrame(
        {
            "factory_id": [1, 2, 3, 4, 5],
            "factory_name": ["A전자", "B화학", "C물류", "D정밀", "E식품"],
            "industry": ["반도체", "화학", "물류", "정밀", "식품"],
            "lat": [36.99, 35.54, 37.56, 35.16, 37.26],
            "lon": [127.11, 129.31, 126.97, 129.06, 127.01],
        }
    )

    # 임의의 시간별 날씨(각 공장 12시간)
    rng = np.random.default_rng(7)
    hours = pd.date_range("2026-03-19 00:00:00", periods=12, freq="h")

    rows = []
    for fid in factories["factory_id"]:
        base_temp = rng.normal(28, 2)
        for ts in hours:
            rows.append(
                {
                    "factory_id": fid,
                    "timestamp": ts,
                    "temp_c": float(base_temp + rng.normal(0, 1.2)),
                    "humidity_pct": float(np.clip(rng.normal(70, 10), 30, 95)),
                    "rain_mm_h": float(max(0, rng.normal(2.5, 6.0))),  # 0~(가끔 큰 값) 느낌
                }
            )

    weather = pd.DataFrame(rows)
    df = weather.merge(factories, on="factory_id", how="left")

    scored = compute_risk(df)
    cols = [
        "timestamp",
        "factory_id",
        "factory_name",
        "temp_c",
        "humidity_pct",
        "rain_mm_h",
        "rain_6h_mm",
        "HeatRisk",
        "FloodRisk",
        "QualityRisk",
        "Risk_Score",
        "Risk_Level",
    ]
    print(scored[cols].head(20).to_string(index=False))


if __name__ == "__main__":
    main()
