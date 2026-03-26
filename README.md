# TCS Demo (Streamlit)

보험사 관점 태풍-계약 위험 알림 데모 앱입니다.

## Local Run

```bash
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Streamlit Cloud Deploy

1. 이 프로젝트를 GitHub 저장소에 업로드합니다.
2. Streamlit Cloud에서 **New app** 선택
3. Repository / Branch / Main file path를 아래처럼 설정
   - Main file path: `streamlit_app.py`
4. **App settings -> Secrets**에 아래 키를 등록합니다.

```toml
KAKAO_REST_API_KEY="YOUR_KAKAO_REST_API_KEY"
OPENWEATHER_API_KEY="YOUR_OPENWEATHER_API_KEY"
KICOX_LOCAL_CSV_PATH="한국산업단지공단_전국등록공장현황_등록공장현황자료_20241231.csv"
```

## Notes

- `.env`는 로컬 전용이며 GitHub에 올리지 않습니다.
- Streamlit Cloud에서는 Secrets에 등록한 값을 우선 사용합니다.
- CSV 파일도 함께 저장소에 포함되어야 로컬 공장 데이터 기반 데모가 동작합니다.
