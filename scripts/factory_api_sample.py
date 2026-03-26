import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


def _pick_first(d: Dict[str, Any], keys: List[str]) -> Tuple[Optional[str], Optional[Any]]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return k, d[k]
    return None, None


def fetch_kicox_factories_sample(
    *,
    sido: str,
    sigungu: str,
    limit: int = 10,
    timeout_s: int = 20,
) -> Dict[str, Any]:
    """
    한국산업단지공단(산단공) '공장등록현황'은 제공 포털/엔드포인트가 여러 형태로 배포되는 경우가 있어,
    이 함수는 '응답 구조 확인' 목적의 샘플이다.

    TODO: 아래 BASE_URL / params 키들은 실제 사용 중인 데이터 포털 문서에 맞게 바꿔야 한다.
    """
    load_dotenv()

    api_key = os.getenv("KICOX_API_KEY")  # 공공데이터포털/기관 포털에서 발급
    if not api_key:
        raise RuntimeError(
            "환경변수 KICOX_API_KEY가 없습니다. .env에 KICOX_API_KEY=... 를 넣어주세요."
        )

    # NOTE: 실제 엔드포인트는 사용 중인 포털 문서에 따라 다릅니다.
    # 예시로만 넣어두고, 실행 시 404/인증 에러가 나면 문서 URL에 맞춰 교체하세요.
    base_url = os.getenv("KICOX_FACTORY_API_URL", "").strip() or "https://api.example.com/factoryRegStatus"

    params = {
        "serviceKey": api_key,
        "type": "json",
        "pageNo": 1,
        "numOfRows": max(limit, 10),
        # 지역 필터(예시). 실제 필드명은 문서 기준으로 수정 필요.
        "sido": sido,
        "sigungu": sigungu,
    }

    r = requests.get(base_url, params=params, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def extract_top10_company_industry_address(payload: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    """
    응답 JSON의 '회사명/업종/주소' 필드가 어떤 키로 오는지 탐색적으로 뽑아준다.
    - 회사명: companyNm / corpNm / entrprsNm / 업체명 등 다양한 케이스 대비
    - 업종: induty / industry / 업종명 등
    - 주소: addr / adres / address / 소재지 등
    """
    # 흔한 공공데이터포털 구조: response > body > items > item (list or dict)
    items = None
    cur: Any = payload
    for k in ("response", "body", "items", "item"):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            break
    items = cur

    if isinstance(items, dict):
        items_list = [items]
    elif isinstance(items, list):
        items_list = items
    else:
        # 구조가 다르면 전체 payload에서 list 후보를 찾지 않고 그냥 실패 처리
        raise ValueError("items 구조를 자동으로 찾지 못했습니다. 응답 JSON 구조를 확인하세요.")

    out: List[Dict[str, Any]] = []
    for row in items_list[:limit]:
        if not isinstance(row, dict):
            continue

        company_k, company_v = _pick_first(
            row,
            ["companyNm", "corpNm", "entrprsNm", "cmpnyNm", "업체명", "회사명", "기업명", "사업장명"],
        )
        industry_k, industry_v = _pick_first(
            row,
            ["industry", "induty", "indutyNm", "업종", "업종명", "업태", "업태명"],
        )
        addr_k, addr_v = _pick_first(
            row,
            ["address", "addr", "adres", "addrRoad", "addrJibun", "소재지", "소재지주소", "도로명주소", "지번주소"],
        )

        out.append(
            {
                "company_key": company_k,
                "company": company_v,
                "industry_key": industry_k,
                "industry": industry_v,
                "address_key": addr_k,
                "address": addr_v,
            }
        )

    return out


def guess_address_type(addr: Optional[str]) -> str:
    """
    카카오 지오코딩에 넣기 전, 주소가 도로명/지번 느낌인지 대략 판별.
    - '로', '길'이 있으면 도로명 가능성이 높음
    - '동', '리', '번지' 패턴이 강하면 지번 가능성이 높음
    (정확 판별은 불가. 여기선 체크 포인트용)
    """
    if not addr:
        return "unknown(empty)"
    a = str(addr)
    if "번지" in a:
        return "jibun_likely"
    if any(tok in a for tok in ["로 ", "로,", "길 ", "길,"]):
        return "road_likely"
    if any(tok in a for tok in ["동 ", "리 ", "동,", "리,"]):
        return "jibun_likely"
    return "unknown"


def main() -> None:
    # 예: 경기도 평택시
    payload = fetch_kicox_factories_sample(sido="경기도", sigungu="평택시", limit=10)

    top10 = extract_top10_company_industry_address(payload, limit=10)
    print(json.dumps(top10, ensure_ascii=False, indent=2))

    print("\n--- address format quick check ---")
    for i, row in enumerate(top10, start=1):
        addr = row.get("address")
        print(f"{i:02d}. {guess_address_type(addr)} | {addr}")

    print(
        "\nNOTE: 실행이 404/인증 오류면, .env에 KICOX_FACTORY_API_URL을 실제 문서 엔드포인트로 설정하세요."
    )


if __name__ == "__main__":
    main()
