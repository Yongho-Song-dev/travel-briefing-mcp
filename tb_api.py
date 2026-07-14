"""
tb_api — 외부 API 클라이언트 (TTL 캐시 + HTTP)

외교부(비자·여행경보), 수출입은행(환율), 네이버(블로그 검색),
GitHub Raw(대사관·큐레이션 JSON) 호출을 담당한다.

공통 원칙:
  - 전역 httpx.Client 재사용 (연결 풀·TLS 세션)
  - 성공은 TTL 캐시, 실패는 60s 네거티브 캐시 (_FETCH_FAIL sentinel)
  - 실패 시 예외 대신 빈 값 반환 — 호출자가 정적 폴백 처리 (p99 3s 대응)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import html
import re
import threading
import time
import json
import os
import logging

import httpx

from tb_config import (
    logger,
    _MOFA_VISA_URL, _MOFA_WARN_URL, _KOREAEXIM_URL, _NAVER_BLOG_URL,
    _HTTP_TIMEOUT_S, _NEGATIVE_TTL_S,
    _MOFA_VISA_TTL_S, _MOFA_ALERT_TTL_S, _EXCHANGE_TTL_S,
    _NAVER_BLOG_TTL_S, _EXTERNAL_JSON_TTL_S,
    _MOFA_COUNTRY_CODE, _LEVEL_NAME, _STATIC,
)

# httpx 는 INFO 레벨에서 요청 URL 전체(serviceKey 포함)를 로깅함 — 키 유출 방지
logging.getLogger("httpx").setLevel(logging.WARNING)

# 전역 HTTP 클라이언트 — 연결 풀·TLS 세션 재사용 (httpx.Client 는 스레드 안전)
_http = httpx.Client(timeout=_HTTP_TIMEOUT_S)

# 외부 API 병렬 호출용 스레드 풀 (compose_checklist 의 비자·경보·환율 동시 조회)
_fetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tb-fetch")

# 네거티브 캐시 sentinel — 캐시 미스(None)와 "실패를 기억함"을 구분
_FETCH_FAIL = object()


# ===========================================================================
# 1) TTL 캐시 (호출비·응답시간 절감)
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
# 2) 외부 JSON(대사관 + 관광지 큐레이션) - 부팅 시 + 24h 갱신
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
        resp = _http.get(url)
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


def get_embassy(country: str) -> dict:
    """
    - 국가별 대사관 정보를 반환하는 접근자 (stale 시 자동 갱신)
      전역 _embassy_data 는 재할당되므로 from-import 하지 말고 이 함수를 사용할 것.
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - embassy(dict): 대사관 연락처/주소. 없으면 빈 dict
    """
    _refresh_embassy_if_stale()
    return _embassy_data.get(country, {})


def get_destinations_json() -> dict:
    """
    - 일본 관광지 큐레이션 JSON 전체를 반환하는 접근자 (stale 시 자동 갱신)
    ### Args:
      - None
    ### Returns:
      - data(dict): destinations_jp.json 파싱 결과. 미로드 시 빈 dict
    """
    _refresh_destinations_jp_if_stale()
    return _destinations_jp


# ===========================================================================
# 3) 외교부 API - 비자(입국허가요건) + 여행경보 (별개 API, 별도 TTL)
# ===========================================================================
_MOFA_VISA_ALL_KEY = "mofa_visa_all"  # 전국가 비자 목록 캐시 키


def _refresh_mofa_visa_all() -> Optional[dict]:
    """
    - 전국가 비자 목록을 API 에서 강제 조회해 캐시에 저장하는 함수
      (스케줄 워밍과 콜드 미스 양쪽에서 공용. 캐시 유무와 무관하게 항상 호출)
    ### Args:
      - None
    ### Returns:
      - all_items(Optional[dict]): {iso2: item} 또는 실패 시 None (실패는 60s 네거티브 캐시)
    """
    api_key = os.getenv("MOFA_API_KEY")
    if not api_key:
        return None
    try:
        resp = _http.get(
            _MOFA_VISA_URL,
            params={"serviceKey": api_key, "returnType": "JSON", "numOfRows": "300", "pageNo": "1"},
        )
        resp.raise_for_status()
        raw = resp.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
        if isinstance(raw, dict):
            raw = [raw]
        all_items = {x["country_iso_alp2"]: x for x in raw if x.get("country_iso_alp2")}
        _cache.set(_MOFA_VISA_ALL_KEY, all_items, _MOFA_VISA_TTL_S)
        return all_items
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA 비자 API 실패: %s", exc)
        _cache.set(_MOFA_VISA_ALL_KEY, _FETCH_FAIL, _NEGATIVE_TTL_S)
        return None


def _fetch_mofa_visa(country: str) -> dict:
    """
    - 외교부 입국허가요건 API 전체 목록을 캐시 후 country_iso_alp2 로 필터링하는 함수
      (06/18시 스케줄 워밍 + TTL 13h — 다음 워밍까지 캐시 유지, 실시간 재호출 없음)
      countryNm 파라미터가 필터 역할을 하지 않아 전체(약 190개국) 조회 후 직접 필터링.
    ### Args:
      - country(str): 국가 코드 (예: 'JP')
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

    # 전국가 목록 캐시 확인 (스케줄 워밍이 06/18시 선제 갱신 → 대부분 히트)
    all_items = _cache.get(_MOFA_VISA_ALL_KEY)
    if all_items is _FETCH_FAIL:
        return {}  # 직전 실패 기억 중 — 재시도 대신 즉시 정적 폴백
    if all_items is None:
        all_items = _refresh_mofa_visa_all()
        if all_items is None:
            return {}

    item = all_items.get(country)
    if not item:
        return {}

    try:
        raw_note = item.get("nvisa_entry_evdc_cn") or item.get("gnrl_pspt_visa_cn") or "-"
        visa = {
            "required": _parse_visa_required(item),
            "duration_days": _parse_visa_days(item),
            "note": raw_note.replace("\\n", " / ").strip(),
        }
        result = {"visa": visa}
        _cache.set(cache_key, result, _MOFA_VISA_TTL_S)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA 비자 응답 파싱 실패: %s", exc)
        return {}


def _parse_visa_required(item: dict) -> bool:
    """
    - MOFA 응답 항목에서 비자 필요 여부를 판정하는 함수
      gnrl_pspt_visa_yn = 'Y' 이면 일반여권 무비자 입국 가능 (required=False).
    ### Args:
      - item(dict): MOFA 입국허가요건 API 응답 항목
    ### Returns:
      - required(bool): 비자 필요 여부. 판단 불가 시 True (안전한 쪽)
    """
    return item.get("gnrl_pspt_visa_yn") != "Y"


def _parse_visa_days(item: dict) -> Optional[int]:
    """
    - MOFA 응답에서 일반여권 무비자 체류 가능 일수를 추출하는 함수
      gnrl_pspt_visa_cn 필드(예: '90일')에서 숫자를 파싱.
    ### Args:
      - item(dict): MOFA 입국허가요건 API 응답 항목
    ### Returns:
      - days(Optional[int]): 체류 가능 일수. 없으면 None
    """
    text = item.get("gnrl_pspt_visa_cn", "")
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
    if cached is _FETCH_FAIL:
        return None  # 직전 실패 기억 중 — 재시도 대신 즉시 폴백
    if cached is not None:
        return cached
    return _refresh_mofa_warn_all()


def _refresh_mofa_warn_all() -> Optional[dict]:
    """
    - 전국가 여행경보 목록을 API 에서 강제 조회해 캐시에 저장하는 함수
      (스케줄 워밍과 콜드 미스 양쪽에서 공용)
    ### Args:
      - None
    ### Returns:
      - by_iso(Optional[dict]): {iso3: item} 또는 실패 시 None (실패는 60s 네거티브 캐시)
    """
    api_key = os.getenv("MOFA_API_KEY")
    if not api_key:
        return None
    try:
        resp = _http.get(
            _MOFA_WARN_URL,
            params={"serviceKey": api_key, "returnType": "JSON", "numOfRows": "250", "pageNo": "1"},
        )
        resp.raise_for_status()
        items = resp.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("MOFA V3 전국가 목록 조회 실패: %s", exc)
        _cache.set(_MOFA_WARN_ALL_KEY, _FETCH_FAIL, _NEGATIVE_TTL_S)
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


# ===========================================================================
# 4) 수출입은행 환율 API (TTL 24h + 스케줄 워밍)
# ===========================================================================
_last_known_exch: dict[str, dict] = {}  # TTL 만료 후 주말·공휴일 폴백용 영구 저장


def _exch_unit(country: str) -> Optional[str]:
    """
    - 국가의 수출입은행 고시 통화 단위(cur_unit)를 반환하는 함수
      data/<CC>.json 의 static.exch_unit 을 그대로 사용한다.
      JPY·IDR 은 100 단위 고시('JPY(100)'), 중국은 역외 위안('CNH') 으로 고시됨.
      VND(베트남)·PHP(필리핀)·TWD(대만) 은 수출입은행 미고시 → None.
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - unit(Optional[str]): 고시 cur_unit 접두 문자열. 미고시 통화면 None
    """
    return _STATIC.get(country, {}).get("exch_unit")


def get_exchange_for_country(country: str) -> dict:
    """
    - 국가 기준 환율 조회 결과를 렌더러가 쓰기 좋은 형태로 반환하는 함수
      미고시 통화(VND/PHP/TWD)는 USD 환율을 함께 실어 '원→달러→현지' 이중환전 안내를 가능하게 함.
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - result(dict): {'quoted': bool, 'rate': Optional[dict], 'usd': Optional[dict], 'currency': str}
    """
    static   = _STATIC.get(country, {})
    currency = static.get("currency", "-")
    unit     = _exch_unit(country)
    if unit:
        return {"quoted": True, "rate": _fetch_exchange_rate(unit),
                "usd": None, "currency": currency}
    # 미고시 통화 — USD 기준값만 제공 (현지 환전소에서 달러→현지통화가 일반적)
    return {"quoted": False, "rate": None,
            "usd": _fetch_exchange_rate("USD"), "currency": currency}


def _fetch_exchange_rows() -> Optional[list]:
    """
    - 수출입은행 API 에서 전 통화 고시 목록(원본 rows)을 가져오는 함수
      주말·공휴일은 빈 배열이 정상 응답임 (호출자가 폴백 처리).
    ### Args:
      - None
    ### Returns:
      - rows(Optional[list]): 통화별 행 목록. 네트워크/키 오류 시 None
    """
    api_key = os.getenv("KOREAEXIM_API_KEY")
    if not api_key:
        logger.warning("KOREAEXIM_API_KEY 환경변수 미설정")
        return None
    try:
        resp = _http.get(_KOREAEXIM_URL, params={"authkey": api_key, "data": "AP01"})
        resp.raise_for_status()
        rows = resp.json()
        return rows if isinstance(rows, list) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("환율 API 실패: %s", exc)
        return None


def _parse_exch_row(row: dict) -> Optional[dict]:
    """
    - 수출입은행 응답 행 하나를 내부 결과 dict 로 변환하는 함수
    ### Args:
      - row(dict): {'cur_unit', 'deal_bas_r', 'cur_nm', ...}
    ### Returns:
      - result(Optional[dict]): 파싱 성공 시 결과, 실패 시 None
    """
    try:
        return {
            "currency": row["cur_unit"],
            "deal_bas_r": float(row.get("deal_bas_r", "").replace(",", "")),
            "search_date": datetime.now().strftime("%Y-%m-%d"),
            "cur_nm": row.get("cur_nm", "-"),
            "is_stale": False,
        }
    except (ValueError, KeyError):
        return None


def _store_exch(currency: str, result: dict) -> None:
    """
    - 환율 결과를 TTL 캐시와 폴백 저장소에 함께 기록하는 함수
    ### Args:
      - currency(str): cur_unit 접두 (예: 'JPY(100)')
      - result(dict): _parse_exch_row 결과
    ### Returns:
      - None
    """
    _cache.set(f"exch:{currency}", result, _EXCHANGE_TTL_S)
    _last_known_exch[currency] = result


def _fetch_exchange_rate(currency: str) -> Optional[dict]:
    """
    - 특정 통화의 최신 환율을 반환하는 함수 (TTL 24h + 스케줄 워밍으로 대부분 캐시 히트)
      주말·공휴일 빈 응답 시 직전 영업일 값(is_stale=True) 폴백.
    ### Args:
      - currency(str): 통화 코드 (예: 'JPY(100)', 'USD')
    ### Returns:
      - result(Optional[dict]): {'currency': str, 'deal_bas_r': float, 'search_date': str} 또는 None
    """
    cache_key = f"exch:{currency}"
    cached = _cache.get(cache_key)
    if cached is _FETCH_FAIL:
        return _stale_exch(currency)
    if cached is not None:
        return cached

    rows = _fetch_exchange_rows()
    if rows is None:
        _cache.set(cache_key, _FETCH_FAIL, _NEGATIVE_TTL_S)
        return _stale_exch(currency)
    if not rows:  # 주말·공휴일 정상 빈 응답
        return _stale_exch(currency)

    for row in rows:
        if row.get("cur_unit", "").startswith(currency):
            result = _parse_exch_row(row)
            if result is not None:
                _store_exch(currency, result)
            return result
    return None


def _stale_exch(currency: str) -> Optional[dict]:
    """
    - 직전 영업일 환율 폴백을 반환하는 함수 (없으면 None)
    ### Args:
      - currency(str): 통화 코드
    ### Returns:
      - result(Optional[dict]): is_stale=True 로 표시된 직전 값 또는 None
    """
    if currency in _last_known_exch:
        stale = dict(_last_known_exch[currency])
        stale["is_stale"] = True
        return stale
    return None


# ===========================================================================
# 5) 네이버 블로그 검색 API (TTL 7h + 스케줄 워밍)
# ===========================================================================
def _fetch_naver_blog(query: str, display: int = 5) -> list[dict]:
    """
    - 네이버 블로그 검색 API 로 여행 후기를 검색하는 함수 (TTL 7h)
      description 의 HTML 태그를 제거해 정제된 텍스트로 반환.
    ### Args:
      - query(str): 검색 쿼리
      - display(int): 반환할 결과 수 (최대 10)
    ### Returns:
      - items(list[dict]): [{'title', 'link', 'description', 'bloggername'}] 형태
    """
    cache_key = f"naver_blog:{query}:{display}"
    cached = _cache.get(cache_key)
    if cached is _FETCH_FAIL:
        return []  # 직전 실패 기억 중 — 재시도 대신 즉시 빈 결과
    if cached is not None:
        return cached

    client_id     = os.getenv("NAVER_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        logger.warning("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
        return []

    try:
        resp = _http.get(
            _NAVER_BLOG_URL,
            params={"query": query, "display": display, "sort": "sim"},
            headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("네이버 블로그 검색 실패 (%s): %s", query, exc)
        _cache.set(cache_key, _FETCH_FAIL, _NEGATIVE_TTL_S)
        return []

    cleaned = []
    for it in items:
        desc = re.sub(r"<[^>]+>", "", it.get("description", ""))
        cleaned.append({
            "title":       re.sub(r"<[^>]+>", "", it.get("title", "")),
            "link":        it.get("link", ""),
            "description": desc.strip(),
            "bloggername": it.get("bloggername", ""),
        })
    _cache.set(cache_key, cleaned, _NAVER_BLOG_TTL_S)
    return cleaned
