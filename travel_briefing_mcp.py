"""
Travel Briefing(트래블 브리핑) MCP 서버 - v5 (PlayMCP 가이드 2026.06.12 준수)

한국인이 가장 많이 가는 해외여행지 '일본' 한정으로
  - 출발 전 준비(비자 동적·전압/시차/통화/긴급/대사관 등)
  - 현재 상황(외교부 경보 + 안전공지)
  - 환율
  - 항공 시즌 가이드 + 스카이스캐너 비교 링크
  - 도시별 관광지 큐레이션(GitHub Raw JSON, 24h 갱신)
  - 종합 D-7 체크리스트
를 제공한다. (다국가 확장은 v2 로드맵)

[가이드 준수]
  - Streamable HTTP / Remote / Stateless
  - 서버·툴 이름에 'kakao' 미포함
  - 툴 6개, 전 툴 read-only, annotations 5종 모두 지정
  - description 영문 + 서비스명 영/국문 병기
  - result는 정제 마크다운(API 원본 그대로 반환 금지)
  - 외부 API는 TTL 캐시로 p99 3,000ms 요건 대응

[데이터 변동성 등급별 처리]
  - 정적 인메모리: 전압/시차/통화/긴급번호/공항코드(IATA)/시즌규칙
  - 외부 JSON + 24h 갱신: 대사관 연락처, 관광지 큐레이션 (GitHub Raw)
  - 외부 API + TTL 캐시:
      · 비자 (외교부 입국허가요건 API, 6h)
      · 여행경보 (외교부 여행경보 API, 1h)
      · 환율 (수출입은행 oapi.koreaexim.go.kr, 24h)

[외부 API]
  - 외교부 입국허가요건: apis.data.go.kr/1262000/EntranceVisa*
  - 외교부 여행경보:     apis.data.go.kr/1262000/TravelAlarm*
  - 수출입은행 환율:     oapi.koreaexim.go.kr/site/program/financial/exchangeJSON
    (기존 www.koreaexim.go.kr 도메인은 2026-04-30 종료 예정, 신규 도메인 사용)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional, Any
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
import html
import threading
import time
import json
import os
import logging

import httpx
from mcp.server.fastmcp import FastMCP

# 로컬 개발용 .env 자동 로드 (배포 환경에선 무시됨)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("travel-briefing")
mcp = FastMCP("travel-briefing")

SupportedCountry = Literal["JP"]  # v1: 일본만 지원. v2 에서 확장.

_READONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}

# 외부 API 엔드포인트
_MOFA_VISA_URL = "https://apis.data.go.kr/1262000/EntranceVisaService2/getEntranceVisaList2"
_MOFA_WARN_URL = "https://apis.data.go.kr/1262000/TravelWarningServiceV3/getTravelWarningListV3"
_KOREAEXIM_URL = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"

# HTTP 호출 타임아웃(가이드 p99 3,000ms 요건 대비 여유)
_HTTP_TIMEOUT_S = 2.5

# ===========================================================================
# 1) 정적 인메모리 데이터 (거의 불변)
# ===========================================================================
_STATIC: dict[str, dict] = {
    "JP": {"name_ko": "일본", "name_en": "Japan", "currency": "JPY",
           "voltage": "100V", "plug": ["A"], "tz_offset_h": 0,
           "lang": "일본어", "emergency": {"police": "110", "ambulance": "119"},
           "airport_iata": "TYO", "tipping": "팁 문화 없음(오히려 결례)",
           "visa_static": {"required": False, "duration_days": 90, "note": "한국 여권 무비자(관광)"}},
    "VN": {"name_ko": "베트남", "name_en": "Vietnam", "currency": "VND",
           "voltage": "220V", "plug": ["A", "C"], "tz_offset_h": -2,
           "lang": "베트남어", "emergency": {"police": "113", "ambulance": "115"},
           "airport_iata": "SGN", "tipping": "팁 일반적이지 않음, 고급 식당은 5~10%",
           "visa_static": {"required": False, "duration_days": 45, "note": "한국 여권 무비자(45일, 정책 자주 변경)"}},
    "TH": {"name_ko": "태국", "name_en": "Thailand", "currency": "THB",
           "voltage": "220V", "plug": ["A", "B", "C"], "tz_offset_h": -2,
           "lang": "태국어", "emergency": {"police": "191", "ambulance": "1669", "tourist_police": "1155"},
           "airport_iata": "BKK", "tipping": "20~50바트 소액 팁 관습",
           "visa_static": {"required": False, "duration_days": 90, "note": "TDAC(전자입국카드) 사전 작성 필수"}},
    "PH": {"name_ko": "필리핀", "name_en": "Philippines", "currency": "PHP",
           "voltage": "220V", "plug": ["A", "B", "C"], "tz_offset_h": -1,
           "lang": "영어/타갈로그어", "emergency": {"police": "117", "ambulance": "911"},
           "airport_iata": "MNL", "tipping": "10% 정도 일반적",
           "visa_static": {"required": False, "duration_days": 30, "note": "30일 무비자, 출국 항공권 필수"}},
    "SG": {"name_ko": "싱가포르", "name_en": "Singapore", "currency": "SGD",
           "voltage": "230V", "plug": ["G"], "tz_offset_h": -1,
           "lang": "영어/중국어/말레이어", "emergency": {"police": "999", "ambulance": "995"},
           "airport_iata": "SIN", "tipping": "팁 문화 없음(서비스 차지 포함)",
           "visa_static": {"required": False, "duration_days": 90, "note": "SG Arrival Card 사전 작성"}},
    "MY": {"name_ko": "말레이시아", "name_en": "Malaysia", "currency": "MYR",
           "voltage": "240V", "plug": ["G"], "tz_offset_h": -1,
           "lang": "말레이어/영어", "emergency": {"police": "999", "ambulance": "999"},
           "airport_iata": "KUL", "tipping": "팁 일반적이지 않음",
           "visa_static": {"required": False, "duration_days": 90, "note": "MDAC(전자입국카드) 사전 작성"}},
    "ID": {"name_ko": "인도네시아", "name_en": "Indonesia", "currency": "IDR",
           "voltage": "230V", "plug": ["C", "F"], "tz_offset_h": -2,
           "lang": "인도네시아어", "emergency": {"police": "110", "ambulance": "118"},
           "airport_iata": "CGK", "tipping": "10% 정도 일반적",
           "visa_static": {"required": True, "duration_days": 30, "note": "VOA(도착비자) 또는 e-VOA 사전 발급"}},
}

# 시즌 규칙: (시작월, 종료월, 시즌타입, 한 줄 설명). 기후 기반이라 거의 안 바뀜.
_SEASON_RULES: dict[str, list[tuple[int, int, str, str]]] = {
    "JP": [(3, 4, "peak", "벚꽃 시즌, 항공·숙박 최성수기"),
           (7, 8, "peak", "여름 휴가철, 가격 매우 높음"),
           (11, 11, "peak", "단풍 시즌"),
           (1, 2, "off", "겨울 비수기(홋카이도 제외)"),
           (5, 6, "shoulder", "장마 전 어깨 시즌, 가성비 좋음")],
    "VN": [(11, 3, "peak", "건기 성수기"),
           (5, 9, "off", "우기, 가격 저렴하나 비 많음")],
    "TH": [(11, 2, "peak", "건기·서늘기, 최성수기"),
           (3, 5, "shoulder", "혹서기, 가격 중간"),
           (6, 10, "off", "우기, 가격 저렴")],
    "PH": [(12, 5, "peak", "건기 성수기(특히 12~2월)"),
           (6, 11, "off", "우기, 태풍 주의")],
    "SG": [(12, 1, "peak", "연말연시 성수기"),
           (6, 8, "peak", "여름 휴가 성수기"),
           (2, 5, "shoulder", "가성비 좋은 시즌")],
    "MY": [(12, 2, "peak", "건기 성수기"),
           (6, 8, "shoulder", "동·서해안 우기 엇갈림")],
    "ID": [(5, 9, "peak", "발리 건기 성수기"),
           (10, 4, "off", "우기, 가격 저렴")],
}

# 국가 코드 → 외교부 TravelWarningServiceV3 iso_code 매핑 (실측 확인 완료)
_MOFA_COUNTRY_CODE: dict[str, str] = {
    "JP": "JPN",
    "VN": "VNM",
    "TH": "THA",
    "PH": "PHL",
    "SG": "SGP",
    "MY": "MYS",
    "ID": "IDN",
}


# ===========================================================================
# 2) TTL 캐시 (호출비·응답시간 절감)
# ===========================================================================
@dataclass
class _CacheEntry:
    value: object
    expires_at: float


class _TTLCache:
    """
    - 스레드 안전 TTL 캐시. p99 3,000ms 요건 충족을 위해 외부 API 응답을 짧게 캐싱한다.
    """
    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        """
        - 키로 캐시된 값을 반환하는 함수(만료 시 None)
        ### Args:
          - key(str): 캐시 키
        ### Returns:
          - value(object|None): 유효 캐시 값 또는 None
        """
        with self._lock:
            entry = self._store.get(key)
            if entry and entry.expires_at > time.time():
                return entry.value
            if entry:
                self._store.pop(key, None)
            return None

    def set(self, key: str, value: object, ttl_seconds: int) -> None:
        """
        - 키-값을 지정된 TTL로 저장하는 함수
        ### Args:
          - key(str): 캐시 키
          - value(object): 캐시 값
          - ttl_seconds(int): 유효 시간(초)
        ### Returns:
          - None
        """
        with self._lock:
            self._store[key] = _CacheEntry(value=value, expires_at=time.time() + ttl_seconds)


_cache = _TTLCache()


# ===========================================================================
# 3) 외부 JSON(대사관 + 관광지 큐레이션) - 부팅 시 + 24h 갱신
# ===========================================================================
# 환경변수로 오버라이드 가능 (테스트/로컬 개발 시 로컬 파일 사용).
_EMBASSY_JSON_URL = os.getenv(
    "TB_EMBASSY_JSON_URL",
    "https://raw.githubusercontent.com/Yongho-Song-dev/travel-briefing-mcp/main/embassies.json",
)
_DESTINATIONS_JP_URL = os.getenv(
    "TB_DESTINATIONS_JP_URL",
    "https://raw.githubusercontent.com/Yongho-Song-dev/travel-briefing-mcp/main/destinations_jp.json",
)
_embassy_data: dict[str, dict] = {}
_embassy_loaded_at: float = 0.0
_destinations_jp: dict = {}
_destinations_jp_loaded_at: float = 0.0
_EXTERNAL_JSON_TTL_S = 24 * 3600

_refresh_lock = threading.Lock()


def _fetch_github_json(url: str) -> Optional[dict]:
    """
    - GitHub Raw 등 공개 JSON URL을 조회해 dict 로 반환하는 내부 함수
      file:// URL은 로컬 파일로 직접 읽는다 (로컬 개발용).
      실패 시 None 반환(호출자가 기존 캐시 유지하도록 함).
    ### Args:
      - url(str): 조회 대상 JSON URL (http/https 또는 file://)
    ### Returns:
      - data(Optional[dict]): 파싱된 JSON 또는 실패 시 None
    """
    try:
        if url.startswith("file://"):
            path = url[7:]
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 (가용성 우선, 로깅만)
        logger.warning("JSON fetch 실패 (%s): %s", url, exc)
        return None


def _refresh_embassy_if_stale() -> None:
    """
    - 대사관 JSON이 24시간 이상 경과했으면 GitHub Raw에서 다시 가져오는 함수
      (실패해도 직전 캐시 유지 → 가용성 우선)
    ### Args:
      - None
    ### Returns:
      - None (전역 _embassy_data 갱신)
    """
    global _embassy_data, _embassy_loaded_at
    if time.time() - _embassy_loaded_at < _EXTERNAL_JSON_TTL_S and _embassy_data:
        return
    with _refresh_lock:
        # 락 획득 후 재확인 (다른 스레드가 이미 갱신했을 수 있음)
        if time.time() - _embassy_loaded_at < _EXTERNAL_JSON_TTL_S and _embassy_data:
            return
        data = _fetch_github_json(_EMBASSY_JSON_URL)
        if data is not None:
            _embassy_data = data.get("countries", data)
            _embassy_loaded_at = time.time()


def _refresh_destinations_jp_if_stale() -> None:
    """
    - 일본 관광지 큐레이션 JSON이 24시간 이상 경과했으면 GitHub Raw에서 다시 가져오는 함수
      (대사관과 동일한 패턴. 실패 시 직전 캐시 유지)
    ### Args:
      - None
    ### Returns:
      - None (전역 _destinations_jp 갱신)
    """
    global _destinations_jp, _destinations_jp_loaded_at
    if time.time() - _destinations_jp_loaded_at < _EXTERNAL_JSON_TTL_S and _destinations_jp:
        return
    with _refresh_lock:
        if time.time() - _destinations_jp_loaded_at < _EXTERNAL_JSON_TTL_S and _destinations_jp:
            return
        data = _fetch_github_json(_DESTINATIONS_JP_URL)
        if data is not None:
            _destinations_jp = data
            _destinations_jp_loaded_at = time.time()


# ===========================================================================
# 4) 외교부 API - 비자(입국허가요건) + 여행경보 (별개 API, 별도 TTL)
# ===========================================================================
_MOFA_VISA_TTL_S = 6 * 3600     # 비자 정보는 자주 안 바뀜
_MOFA_ALERT_TTL_S = 1 * 3600    # 여행경보는 상황 변동성 있음


def _fetch_mofa_visa(country: str) -> dict:
    """
    - 외교부 해외안전여행 API에서 비자 / 여행경보 / 안전공지를 함께 조회하는 함수
      (한 번 호출로 3종 정보를 묶어 가져와 호출 횟수를 최소화)
      비자는 TTL 6h, 경보·공지는 TTL 1h로 별도 캐싱.
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - data(dict): {'visa': {...}} 형태, 실패 시 빈 dict
    """
    cache_key = f"mofa_visa:{country}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.getenv("MOFA_API_KEY")
    if not api_key:
        logger.warning("MOFA_API_KEY 환경변수 미설정 — 정적 폴백 사용")
        return {}

    params = {
        "serviceKey": api_key,
        "returnType": "JSON",
        "numOfRows": "10",
        "pageNo": "1",
        "countryNm": _STATIC[country]["name_en"],  # 응답 필드에 따라 조정 필요
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(_MOFA_VISA_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA 비자 API 실패 (%s): %s", country, exc)
        return {}

    # 응답 스키마 매핑 (실제 응답 확인 후 필드명 조정 필요)
    # data['response']['body']['items']['item'] 형태 가정
    try:
        items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        if not items:
            return {}
        first = items[0]
        visa = {
            "required": _parse_visa_required(first),
            "duration_days": _parse_visa_days(first),
            "note": first.get("enTrgtRmrk") or first.get("enSmryCn") or "-",
        }
        result = {"visa": visa}
        _cache.set(cache_key, result, _MOFA_VISA_TTL_S)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA 비자 응답 파싱 실패: %s", exc)
        return {}


def _parse_visa_required(item: dict) -> bool:
    """
    - MOFA 응답 항목에서 비자 필요 여부를 추론하는 함수(실제 필드 확인 후 로직 정교화)
    ### Args:
      - item(dict): MOFA API 응답의 개별 항목
    ### Returns:
      - required(bool): 비자 필요 여부. 판단 불가 시 True (안전한 쪽)
    """
    # 무비자 관련 필드가 있으면 우선 참조. 예: entaCn(입국가능기간 문자열)
    text = " ".join(str(v) for v in item.values() if v).lower()
    if "무비자" in text or "visa-free" in text or "visa free" in text:
        return False
    return True


def _parse_visa_days(item: dict) -> Optional[int]:
    """
    - MOFA 응답에서 무비자 체류 가능 일수를 추출하는 함수
    ### Args:
      - item(dict): MOFA API 응답의 개별 항목
    ### Returns:
      - days(Optional[int]): 체류 가능 일수. 없으면 None
    """
    import re
    text = " ".join(str(v) for v in item.values() if v)
    m = re.search(r"(\d{1,3})\s*일", text)
    return int(m.group(1)) if m else None


_MOFA_WARN_ALL_KEY = "mofa_warn_all"  # 전국가 목록 캐시 키


def _fetch_mofa_warn_all() -> Optional[dict]:
    """
    - TravelWarningServiceV3 전국가 목록을 가져와 iso_code 키 dict 로 캐싱하는 함수 (TTL 1h)
      197개국을 한 번에 반환하므로 국가별 호출 대신 전체를 캐시하고 필터링.
    ### Args:
      - None
    ### Returns:
      - data(Optional[dict]): {iso_code: item} 또는 실패 시 None
    """
    cached = _cache.get(_MOFA_WARN_ALL_KEY)
    if cached is not None:
        return cached

    api_key = os.getenv("MOFA_API_KEY")
    if not api_key:
        return None

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                _MOFA_WARN_URL,
                params={"serviceKey": api_key, "returnType": "JSON", "numOfRows": "250", "pageNo": "1"},
            )
            resp.raise_for_status()
            items = resp.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA V3 전국가 목록 조회 실패: %s", exc)
        return None

    by_iso = {x["iso_code"]: x for x in items if x.get("iso_code")}
    _cache.set(_MOFA_WARN_ALL_KEY, by_iso, _MOFA_ALERT_TTL_S)
    return by_iso


def _fetch_mofa_alert(country: str) -> dict:
    """
    - 외교부 여행경보 V3 API 에서 국가별 경보 단계·지역노트를 조회하는 함수 (TTL 1h)
      전국가 목록을 한 번 호출해 캐시 후 iso_code 로 필터링.
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - data(dict): {'level': 0~4, 'level_name': str, 'issued_at': str, 'note': str}
    """
    cache_key = f"mofa_alert:{country}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    if not os.getenv("MOFA_API_KEY"):
        return {"level": 0, "level_name": "정보 없음", "issued_at": "-", "note": "API 키 미설정"}

    all_data = _fetch_mofa_warn_all()
    if all_data is None:
        return {"level": 0, "level_name": "조회 실패", "issued_at": "-", "note": "출발 전 0404.go.kr 재확인"}

    iso = _MOFA_COUNTRY_CODE.get(country)
    item = all_data.get(iso) if iso else None

    result = _parse_warning_item(item) if item else {
        "level": 0, "level_name": "미발령", "issued_at": "-", "note": "-",
    }
    _cache.set(cache_key, result, _MOFA_ALERT_TTL_S)
    return result


def _parse_warning_item(item: dict) -> dict:
    """
    - V3 경보 항목에서 최고 레벨과 지역 노트를 추출하는 함수
      ban(4) > limita(3) > control(2) > attention(1) 순서로 전국 레벨 결정.
      *_partial 필드는 레벨에 반영하지 않고 노트에만 포함.
    ### Args:
      - item(dict): TravelWarningServiceV3 응답의 개별 국가 항목
    ### Returns:
      - result(dict): {'level': int, 'level_name': str, 'issued_at': str, 'note': str}
    """
    level = 0
    notes: list[str] = []

    checks = [
        ("ban_yna",        "ban_note",     4, "여행금지"),
        ("ban_yn_partial", "ban_note",     4, "여행금지(일부)"),
        ("limita",         "limita_note",  3, "출국권고"),
        ("limita_partial", "limita_note",  0, "출국권고(일부)"),   # 전국 레벨 미반영
        ("control",        "control_note", 2, "여행자제"),
        ("control_partial","control_note", 0, "여행자제(일부)"),
        ("attention",      "attention_note", 1, "여행유의"),
        ("attention_partial","attention_note", 0, "여행유의(일부)"),
    ]
    for field, note_field, lvl, label in checks:
        if item.get(field):
            level = max(level, lvl)
            raw = item.get(note_field) or item.get(field) or ""
            # 공공데이터포털 응답이 이중 HTML 이스케이프인 경우를 처리
            region = raw
            for _ in range(2):
                unescaped = html.unescape(region)
                if unescaped == region:
                    break
                region = unescaped
            if region and region != label:
                notes.append(f"{label}: {region}")
            else:
                notes.append(label)

    return {
        "level": level,
        "level_name": _LEVEL_NAME.get(level, "정보 없음"),
        "issued_at": item.get("wrt_dt") or "-",
        "note": " / ".join(notes) if notes else "-",
    }


_LEVEL_NAME = {0: "미발령", 1: "여행유의", 2: "여행자제", 3: "출국권고", 4: "여행금지"}


# ===========================================================================
# 4-1) 수출입은행 환율 API (TTL 24h)
# ===========================================================================
_EXCHANGE_TTL_S = 24 * 3600
_last_known_exch: dict[str, dict] = {}  # TTL 만료 후 주말·공휴일 폴백용 영구 저장


def _fetch_exchange_rate(currency: str) -> Optional[dict]:
    """
    - 수출입은행 API 에서 특정 통화의 최신 환율을 조회하는 함수 (신규 도메인 사용, TTL 24h)
      영업일 외에는 직전 영업일 값이 응답됨.
    ### Args:
      - currency(str): 통화 코드 (예: 'JPY(100)', 'USD')
    ### Returns:
      - result(Optional[dict]): {'currency': str, 'deal_bas_r': float, 'search_date': str} 또는 None
    """
    cache_key = f"exch:{currency}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.getenv("KOREAEXIM_API_KEY")
    if not api_key:
        logger.warning("KOREAEXIM_API_KEY 환경변수 미설정")
        return None

    params = {"authkey": api_key, "data": "AP01"}
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(_KOREAEXIM_URL, params=params)
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("환율 API 실패: %s", exc)
        return None

    # 응답은 리스트, 각 원소가 통화별 환율. cur_unit 이 'JPY(100)' 형태.
    # 주말·공휴일은 빈 배열 반환 → 마지막 유효값 폴백
    if not isinstance(rows, list) or not rows:
        if currency in _last_known_exch:
            stale = dict(_last_known_exch[currency])
            stale["is_stale"] = True
            return stale
        return None
    for row in rows:
        if row.get("cur_unit", "").startswith(currency):
            deal = row.get("deal_bas_r", "").replace(",", "")
            try:
                result = {
                    "currency": row["cur_unit"],
                    "deal_bas_r": float(deal),
                    "search_date": datetime.now().strftime("%Y-%m-%d"),
                    "cur_nm": row.get("cur_nm", "-"),
                    "is_stale": False,
                }
                _cache.set(cache_key, result, _EXCHANGE_TTL_S)
                _last_known_exch[currency] = result  # 주말 폴백용 영구 보존
                return result
            except (ValueError, KeyError):
                return None
    return None


# ===========================================================================
# 5) 툴 6개
# ===========================================================================
@mcp.tool(annotations={"title": "Trip Briefing", "openWorldHint": True, **_READONLY})
def get_trip_briefing(country: SupportedCountry) -> str:
    """
    Retrieves pre-trip essentials (visa from MOFA API, voltage, time difference,
    currency, emergency numbers, embassy contact) for a destination from
    Travel Briefing(트래블 브리핑). Falls back to cached static visa info if MOFA is unavailable.

    - 출발 전 필수 정보(비자는 외교부 동적, 나머지는 정적/대사관 외부JSON)를 마크다운으로 반환하는 함수
    ### Args:
      - country(SupportedCountry): 국가 코드 ('JP')
    ### Returns:
      - md(str): 비자/전압/시차/통화/긴급/대사관 마크다운 + 면책 + 외교부 폴백 시 경고
    """
    static = _STATIC[country]
    _refresh_embassy_if_stale()
    embassy = _embassy_data.get(country, {})

    visa_warning = ""
    mofa = _fetch_mofa_visa(country)
    visa = mofa.get("visa") or static["visa_static"]
    if not mofa:
        visa_warning = "\n> ⚠️ 비자 정보 실시간 갱신 실패 — 출발 전 외교부 영사콜(02-3210-0404) 재확인 필수"

    return _render_briefing_md(country, static, visa, embassy) + visa_warning


@mcp.tool(annotations={"title": "Current Status", "openWorldHint": True, **_READONLY})
def get_current_status(country: SupportedCountry) -> str:
    """
    Retrieves the current travel-advisory level, official safety notices, and
    recent news headlines for a destination from Travel Briefing(트래블 브리핑).
    Returns raw signals only; the calling LLM should synthesize the overall situation.

    - 외교부 여행경보 단계와 발효일을 반환하는 함수(뉴스는 v1 미포함)
    ### Args:
      - country(SupportedCountry): 국가 코드
    ### Returns:
      - md(str): 경보 단계 + 발효일 + 요약 마크다운. 종합 판단은 호출자 LLM
    """
    alert = _fetch_mofa_alert(country)
    return _render_alert_md(country, alert)


@mcp.tool(annotations={"title": "Exchange Rate", "openWorldHint": True, **_READONLY})
def get_exchange_rate(country: SupportedCountry) -> str:
    """
    Retrieves the current KRW-based exchange rate for a destination's currency
    from Travel Briefing(트래블 브리핑) via the Korea Eximbank API.

    - 해당 국가 통화의 현재 원화 매매기준율을 반환하는 함수
      (v1 은 30일 추이 대신 현재 환율만. 추이는 v2 확장 검토)
    ### Args:
      - country(SupportedCountry): 국가 코드
    ### Returns:
      - md(str): 현재 환율 마크다운 (실패 시 안내 문구)
    """
    static = _STATIC[country]
    query = static["currency"]
    # 일본 엔은 API 응답에서 'JPY(100)' 형태로 100엔 단위 표시
    if query == "JPY":
        query = "JPY(100)"

    result = _fetch_exchange_rate(query)
    return _render_exchange_md(static, result)


@mcp.tool(annotations={"title": "Flight Season Guide", "openWorldHint": False, **_READONLY})
def get_flight_season_guide(country: SupportedCountry, depart_date: str, return_date: str) -> str:
    """
    Provides peak/off-peak season insight, optimal booking-timing tips, and a
    Skyscanner comparison link (no real-time price) from Travel Briefing(트래블 브리핑).

    - 여행 기간을 받아 시즌 판정·예약 팁·스카이스캐너 링크를 반환하는 함수(외부호출 없음)
    ### Args:
      - country(SupportedCountry): 국가 코드
      - depart_date(str): 출국일 'YYYY-MM-DD'
      - return_date(str): 귀국일 'YYYY-MM-DD'
    ### Returns:
      - md(str): 시즌 판정 + 예약 시점 팁 + 스카이스캐너 딥링크
    """
    season = _judge_season(country, depart_date)
    link = _build_skyscanner_link(country, depart_date, return_date)
    return _render_flight_md(country, season, depart_date, return_date, link)


@mcp.tool(annotations={"title": "Destinations", "openWorldHint": True, **_READONLY})
def get_destinations(
    country: SupportedCountry,
    city: Optional[str] = None,
    category: Optional[Literal["culture", "food", "nature", "shopping", "onsen"]] = None,
) -> str:
    """
    Retrieves curated travel spots (culture/food/nature/shopping/onsen) by city
    for a destination from Travel Briefing(트래블 브리핑). v1 supports Japan only;
    other countries return a not-yet-available message.

    - 도시·카테고리로 큐레이션된 관광지 목록을 반환하는 함수 (v1 일본 한정)
      데이터는 외부 JSON(GitHub Raw)에서 24시간 주기 갱신되어 폐업·리뉴얼 반영 가능.
    ### Args:
      - country(SupportedCountry): 국가 코드 (v1 은 'JP'만 지원)
      - city(Optional[str]): 도시 키 ('tokyo','osaka','kyoto','fukuoka','sapporo','okinawa','hiroshima','nara')
                             None 이면 국가 내 대표 도시 목록 반환
      - category(Optional): 카테고리 필터 (culture/food/nature/shopping/onsen), None 이면 전체
    ### Returns:
      - md(str): 도시·스팟을 정제된 마크다운으로 반환. under_renovation 인 곳은 표시.
    """
    if country != "JP":
        return (
            f"# {_STATIC[country]['name_ko']} 관광지 큐레이션\n\n"
            f"> v1 은 일본(JP)만 지원합니다. 다른 국가는 준비 중입니다.\n"
            f"> 그동안 `get_flight_season_guide` 로 시즌 정보를 확인해보세요."
        )

    _refresh_destinations_jp_if_stale()
    data = _destinations_jp

    if not city:
        cities = data.get("cities", {})
        lines = ["# 일본 대표 도시\n"]
        for key, c in cities.items():
            lines.append(f"- **{c['name_ko']}** ({c['name_local']}) — `city='{key}'`")
        lines.append("\n> 원하는 도시를 지정하면 스팟을 카테고리별로 보여드립니다.")
        return "\n".join(lines)

    city_data = data.get("cities", {}).get(city)
    if not city_data:
        return f"> 지원하지 않는 도시 키입니다: `{city}`"

    spots = city_data["spots"]
    if category:
        spots = [s for s in spots if s["category"] == category]
    return _render_destinations_md(city_data, spots, category)


@mcp.tool(annotations={"title": "Pre-Trip Checklist", "openWorldHint": True, **_READONLY})
def compose_checklist(country: SupportedCountry, depart_date: str, return_date: str) -> str:
    """
    Composes a D-7 pre-trip checklist card (copy-paste friendly) by combining the
    other tools' outputs from Travel Briefing(트래블 브리핑).

    - 다른 툴 결과를 종합해 D-7 체크리스트 카드를 만드는 함수(데모 메인)
    ### Args:
      - country(SupportedCountry): 국가 코드
      - depart_date(str): 출국일 'YYYY-MM-DD'
      - return_date(str): 귀국일 'YYYY-MM-DD'
    ### Returns:
      - md(str): 단톡방 붙여넣기용 요약 카드 + D-day 체크리스트 + 유의사항 + 면책
    """
    static = _STATIC[country]
    # 각 도구 결과 수집(실패해도 다른 섹션은 살아있도록 개별 예외 처리)
    try:
        mofa = _fetch_mofa_visa(country)
        visa = mofa.get("visa") or static["visa_static"]
    except Exception:
        visa = static["visa_static"]
    try:
        alert = _fetch_mofa_alert(country)
    except Exception:
        alert = {"level": 0, "level_name": "조회 실패", "issued_at": "-", "note": "-"}
    try:
        exch_query = "JPY(100)" if static["currency"] == "JPY" else static["currency"]
        exch = _fetch_exchange_rate(exch_query)
    except Exception:
        exch = None
    season = _judge_season(country, depart_date)

    # D-day 산출: 오늘부터 출국일까지 남은 일수
    try:
        dep = datetime.strptime(depart_date, "%Y-%m-%d").date()
        days_left = (dep - date.today()).days
    except ValueError:
        days_left = None

    lines = [
        f"# ✈️ {static['name_ko']} 여행 체크리스트 ({depart_date} ~ {return_date})",
        "",
    ]
    if days_left is not None:
        lines.append(f"**D-{days_left}** 남았습니다.\n" if days_left >= 0 else "출국일이 지났습니다.\n")

    # 요약 카드 (단톡방 붙여넣기용)
    visa_txt = f"무비자 {visa.get('duration_days','?')}일" if not visa.get("required") else "비자 필요"
    lines.append("## 📋 요약")
    lines.append(f"- **비자**: {visa_txt}")
    lines.append(f"- **여행경보**: {alert['level_name']} (레벨 {alert['level']}, 발효 {alert['issued_at']})")
    if exch:
        lines.append(f"- **환율**: {exch['currency']} = {exch['deal_bas_r']:,.2f} 원")
    lines.append(f"- **시즌**: {season['note']}")
    lines.append(f"- **긴급번호**: " + ", ".join(f"{k} {v}" for k, v in static['emergency'].items()))
    lines.append("")

    # D-day 체크리스트
    lines.append("## 🗓 D-day 체크리스트")
    lines.append("- **D-14 이전**: 여권 유효기간 6개월 이상 확인, 항공권 예약")
    lines.append("- **D-7**: 숙소·주요 예약 확정, 여행자보험, 데이터 로밍/유심")
    lines.append("- **D-3**: 환전(공항보다 시내 사설 환전소가 유리한 경우 있음)")
    lines.append("- **D-1**: 여권·항공권·숙소 바우처 캡처, 짐 최종 확인")
    lines.append("- **출국일**: 공항 2시간 전 도착, 액체류 100ml 규정")
    lines.append("")

    if alert["level"] >= 2:
        lines.append(f"> ⚠️ 여행경보 {alert['level_name']} — 방문 전 외교부(0404.go.kr) 상세 안내 필독")
    lines.append("> 참고용. 실시간 정보는 각 공식 사이트에서 재확인하세요.")
    return "\n".join(lines)


# ===========================================================================
# 6) 내부 헬퍼
# ===========================================================================
def _judge_season(country: str, depart_date: str) -> dict:
    """
    - 출국 월이 해당 국가의 어느 시즌 규칙에 해당하는지 판정하는 함수
    ### Args:
      - country(str): 국가 코드
      - depart_date(str): 출국일 'YYYY-MM-DD'
    ### Returns:
      - result(dict): {'season': 'peak'|'shoulder'|'off', 'note': str}
                      (규칙 미매칭 시 'shoulder'로 폴백)
    """
    month = datetime.strptime(depart_date, "%Y-%m-%d").month
    for start, end, season, note in _SEASON_RULES.get(country, []):
        if start <= end:
            in_range = start <= month <= end
        else:  # 연말~연초 걸침 (예: 12~2월)
            in_range = month >= start or month <= end
        if in_range:
            return {"season": season, "note": note}
    return {"season": "shoulder", "note": "특별한 성수기/비수기 표시 없음"}


def _build_skyscanner_link(country: str, depart_date: str, return_date: str) -> str:
    """
    - 인천(ICN) 출발 기준 스카이스캐너 비교 페이지 딥링크를 생성하는 함수
    ### Args:
      - country(str): 국가 코드
      - depart_date(str): 출국일 'YYYY-MM-DD'
      - return_date(str): 귀국일 'YYYY-MM-DD'
    ### Returns:
      - url(str): 스카이스캐너 검색 URL
    """
    iata = _STATIC[country]["airport_iata"]
    dep = depart_date.replace("-", "")[2:]  # YYMMDD
    ret = return_date.replace("-", "")[2:]
    return f"https://www.skyscanner.co.kr/transport/flights/icn/{iata.lower()}/{dep}/{ret}/"


def _render_alert_md(country: str, alert: dict) -> str:
    """
    - 외교부 여행경보 결과를 정제 마크다운으로 렌더링하는 함수
    ### Args:
      - country(str): 국가 코드
      - alert(dict): _fetch_mofa_alert 결과
    ### Returns:
      - md(str): 경보 단계·발효일·요약 마크다운
    """
    s = _STATIC[country]
    icon = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴", 4: "⛔"}.get(alert["level"], "⚪")
    note_line = f"\n**요약**: {alert['note']}" if alert.get("note") and alert["note"] not in ("-", "") else ""
    return (
        f"# {s['name_ko']} 현재 안전 상황\n\n"
        f"{icon} **경보 단계**: {alert['level_name']} (레벨 {alert['level']})\n"
        f"**발효일**: {alert['issued_at']}{note_line}\n\n"
        f"> 상세 안내는 외교부 해외안전여행([0404.go.kr](https://www.0404.go.kr)) 확인."
    )


def _render_exchange_md(static: dict, result: Optional[dict]) -> str:
    """
    - 환율 조회 결과를 정제 마크다운으로 렌더링하는 함수
    ### Args:
      - static(dict): 국가 정적 정보
      - result(Optional[dict]): _fetch_exchange_rate 결과 또는 None
    ### Returns:
      - md(str): 환율 마크다운 (조회 실패 시 안내)
    """
    header = f"# {static['name_ko']} 환율\n"
    if not result:
        return header + "\n> 환율 조회 실패. 잠시 후 다시 시도해주세요."
    stale_note = (
        "\n> ⚠️ 주말·공휴일로 API 갱신 불가 — 직전 영업일 기준 환율입니다."
        if result.get("is_stale") else ""
    )
    return (
        f"{header}\n"
        f"**{result['currency']}** = **{result['deal_bas_r']:,.2f} 원** (매매기준율, {result['search_date']} 기준)\n"
        f"{stale_note}\n"
        f"> 실거래 환율은 은행·환전소별로 상이. 참고용."
    )


def _render_briefing_md(country: str, static: dict, visa: dict, embassy: dict) -> str:
    """
    - 정적 정보 + 동적 비자 + 대사관을 정제된 마크다운으로 렌더링하는 함수
    ### Args:
      - country(str): 국가 코드
      - static(dict): _STATIC[country]
      - visa(dict): MOFA 비자 정보 또는 visa_static 폴백
      - embassy(dict): 대사관 JSON에서 가져온 연락처/주소
    ### Returns:
      - md(str): 가이드 권장에 따른 정제 마크다운(과도한 raw 데이터 제외)
    """
    s = static
    visa_line = f"무비자 {visa['duration_days']}일" if not visa.get("required") else "비자 필요"
    note = visa.get("note", "")
    plugs = "/".join(s["plug"])
    tz = s["tz_offset_h"]
    tz_str = "KST와 동일" if tz == 0 else f"KST 기준 {tz:+d}시간"

    embassy_block = ""
    if embassy:
        embassy_block = (
            f"\n**🏛️ 한국 대사관**\n"
            f"- 주소: {embassy.get('address', '-')}\n"
            f"- 대표번호: {embassy.get('phone', '-')}\n"
            f"- 긴급(사건·사고): {embassy.get('emergency', '-')}\n"
        )

    return (
        f"# {s['name_ko']}({s['name_en']}) 여행 브리핑\n\n"
        f"**🛂 비자**: {visa_line} — {note}\n"
        f"**🔌 전압/플러그**: {s['voltage']} / {plugs} 타입\n"
        f"**🕐 시차**: {tz_str}\n"
        f"**💱 통화**: {s['currency']}\n"
        f"**📞 긴급**: " + ", ".join(f"{k} {v}" for k, v in s['emergency'].items()) + "\n"
        f"**💁 팁 문화**: {s['tipping']}\n"
        f"{embassy_block}\n"
        f"> 참고용. 출발 전 외교부 해외안전여행(0404.go.kr) 공식 안내를 반드시 확인하세요."
    )


def _render_flight_md(country: str, season: dict, depart: str, ret: str, link: str) -> str:
    """
    - 시즌 판정과 스카이스캐너 링크를 정제 마크다운으로 렌더링하는 함수
    ### Args:
      - country(str): 국가 코드
      - season(dict): _judge_season 결과
      - depart(str): 출국일
      - ret(str): 귀국일
      - link(str): _build_skyscanner_link 결과
    ### Returns:
      - md(str): 시즌/팁/링크 마크다운
    """
    s = _STATIC[country]
    tip_by_season = {
        "peak": "성수기 — 최소 2~3개월 전 예약 권장, 평일 출발이 유리",
        "shoulder": "어깨 시즌 — 1~2개월 전 예약 권장, 가성비 양호",
        "off": "비수기 — 임박 예약도 가격 안정적, 단 기상 확인 필요",
    }
    return (
        f"# {s['name_ko']} 항공권 가이드 ({depart} ~ {ret})\n\n"
        f"**🗓 시즌**: {season['note']}\n"
        f"**💡 예약 팁**: {tip_by_season[season['season']]}\n"
        f"**🔗 비교 검색**: [스카이스캐너에서 ICN→{s['airport_iata']} 보기]({link})\n\n"
        f"> 실시간 가격은 비교 사이트에서 확인하세요."
    )


_CATEGORY_LABEL = {
    "culture": "🏯 문화·관광", "food": "🍜 먹거리",
    "nature": "🌿 자연·공원", "shopping": "🛍 쇼핑", "onsen": "♨️ 온천",
}


def _render_destinations_md(city_data: dict, spots: list[dict], category_filter: Optional[str]) -> str:
    """
    - 도시 스팟 목록을 카카오맵 검색 링크와 상태 배지 포함해 마크다운으로 렌더링하는 함수
      status='under_renovation' 은 명시적으로 표시해 오탐을 줄인다.
    ### Args:
      - city_data(dict): destinations_jp.json 의 cities.<key>
      - spots(list[dict]): 필터링된 스팟 리스트
      - category_filter(Optional[str]): 적용된 카테고리(None 이면 전체)
    ### Returns:
      - md(str): 마크다운 텍스트
    """
    header = f"# {city_data['name_ko']}({city_data['name_local']}) 관광 스팟"
    if category_filter:
        header += f" — {_CATEGORY_LABEL.get(category_filter, category_filter)}"

    if not spots:
        return f"{header}\n\n> 해당 카테고리 스팟이 아직 준비되지 않았습니다."

    # 카테고리별 그룹핑(필터 없을 때만)
    by_cat: dict[str, list[dict]] = {}
    for s in spots:
        by_cat.setdefault(s["category"], []).append(s)

    lines = [header, ""]
    order = [category_filter] if category_filter else ["culture", "food", "nature", "shopping", "onsen"]
    for cat in order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        if not category_filter:
            lines.append(f"## {_CATEGORY_LABEL.get(cat, cat)}")
        for s in items:
            badges = []
            if s.get("status") == "under_renovation":
                badges.append("🚧 리뉴얼/재건 중")
            elif s.get("status") == "seasonal":
                badges.append("🗓 시즌 한정")
            badge_str = f" {' '.join(badges)}" if badges else ""
            kakao_link = f"https://map.kakao.com/?q={s['search_query']}"
            lines.append(
                f"- **{s['name_ko']}** ({s['name_local']}){badge_str}\n"
                f"  - {s['one_liner']}\n"
                f"  - [카카오맵에서 보기]({kakao_link})"
            )
        lines.append("")

    lines.append(f"> 큐레이션 데이터 최종 검토일: {_destinations_jp.get('last_reviewed', '-')}. "
                 f"방문 전 공식 사이트에서 운영 여부를 재확인하세요.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
