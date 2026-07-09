"""API 키 실호출 검증 스크립트 (개발용, 커밋 불필요)

실행: uv run python test_apis.py
"""
import os, json, urllib.parse
from dotenv import load_dotenv
load_dotenv()
import httpx

MOFA_KEY  = os.getenv("MOFA_API_KEY", "")
EXIM_KEY  = os.getenv("KOREAEXIM_API_KEY", "")
TIMEOUT   = 5

def test_mofa_visa():
    print("\n=== [1] MOFA 비자 API ===")
    for country_nm in ["Japan", "일본", "JPN", "JP"]:
        r = httpx.get(
            "https://apis.data.go.kr/1262000/EntranceVisaService2/getEntranceVisaList2",
            params={"serviceKey": MOFA_KEY, "returnType": "JSON",
                    "numOfRows": "3", "pageNo": "1", "countryNm": country_nm},
            timeout=TIMEOUT,
        )
        print(f"  countryNm={country_nm!r} → HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print("  응답:", json.dumps(data, ensure_ascii=False, indent=2)[:800])
            return data
        else:
            print("  응답:", r.text[:200])
    return None

def test_mofa_alert():
    print("\n=== [2] MOFA 여행경보 V3 API ===")
    r = httpx.get(
        "https://apis.data.go.kr/1262000/TravelWarningServiceV3/getTravelWarningListV3",
        params={"serviceKey": MOFA_KEY, "returnType": "JSON", "numOfRows": "250", "pageNo": "1"},
        timeout=TIMEOUT,
    )
    print(f"  HTTP {r.status_code}")
    if r.status_code == 200:
        items = r.json()["response"]["body"]["items"]["item"]
        jp = next((x for x in items if x.get("iso_code") == "JPN"), None)
        print(f"  전체 국가 수: {len(items)}")
        print("  일본 항목:", json.dumps(jp, ensure_ascii=False, indent=2) if jp else "없음")
        return items
    print("  응답:", r.text[:200])
    return None

def test_exim():
    print("\n=== [3] 수출입은행 환율 API ===")
    r = httpx.get(
        "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON",
        params={"authkey": EXIM_KEY, "data": "AP01"},
        timeout=TIMEOUT,
    )
    print(f"  HTTP {r.status_code}")
    rows = r.json() if r.status_code == 200 else []
    if rows:
        jpy = next((x for x in rows if "JPY" in x.get("cur_unit", "")), None)
        print("  JPY 행:", json.dumps(jpy, ensure_ascii=False, indent=2) if jpy else "없음 (주말?)")
        print("  응답 키 목록:", list(rows[0].keys()))
    else:
        print("  빈 응답 (주말·공휴일은 정상)")

if __name__ == "__main__":
    print(f"MOFA_KEY: {MOFA_KEY[:8]}... ({len(MOFA_KEY)}자)")
    print(f"EXIM_KEY: {EXIM_KEY[:8]}... ({len(EXIM_KEY)}자)")
    test_mofa_visa()
    test_mofa_alert()
    test_exim()
