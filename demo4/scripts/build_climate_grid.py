"""
원본 강수 극한지수 격자데이터(Hazard_Map_Precipitation_Present.csv, 0.01도 격자, 약 45만행)를
0.05도로 다운샘플링해 demo4/climate_grid_0.05deg.csv를 생성한다.

원본 파일은 용량(약 21MB)과 NaN(해역) 비중이 커서 앱이 직접 로드하지 않고,
이 스크립트로 한 번만 가공한 경량 파일(약 4,300행)을 앱이 읽어 지도 히트맵에 사용한다.

실행: python scripts/build_climate_grid.py
"""
from pathlib import Path

import pandas as pd

SRC = Path(__file__).resolve().parent.parent / "Hazard_Map_Precipitation_Present.csv"
DST = Path(__file__).resolve().parent.parent / "climate_grid_0.05deg.csv"
GRID_RES = 0.05


def main() -> None:
    df = pd.read_csv(SRC, encoding="cp949")
    df = df.dropna(subset=["종합지수"])

    df["lat_g"] = (df["위도"] / GRID_RES).round() * GRID_RES
    df["lon_g"] = (df["경도"] / GRID_RES).round() * GRID_RES
    grid = df.groupby(["lat_g", "lon_g"])["종합지수"].mean().reset_index()
    grid.columns = ["lat", "lon", "composite_index"]
    grid["lat"] = grid["lat"].round(3)
    grid["lon"] = grid["lon"].round(3)
    grid["composite_index"] = grid["composite_index"].round(2)
    grid = grid.sort_values(["lat", "lon"]).reset_index(drop=True)

    grid.to_csv(DST, index=False, encoding="utf-8")
    print(f"{len(grid)} grid points -> {DST}")


if __name__ == "__main__":
    main()
