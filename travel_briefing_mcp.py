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
from pathlib import Path
import html
import re
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

# ===========================================================================
# 1) 외부 설정 파일 로더 (config/ + data/)
# ===========================================================================
_BASE_DIR = Path(__file__).parent


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("설정 파일 로드 실패 %s: %s", path, e)
        return {}


def _load_api_config() -> dict:
    return _load_json(_BASE_DIR / "config" / "api.json")


def _load_vocab() -> dict:
    raw = _load_json(_BASE_DIR / "config" / "vocab.json")
    q = raw.get("query", {})
    # JSON 정수 키(문자열) → int 변환
    q["month_to_season"] = {int(k): v for k, v in q.get("month_to_season", {}).items()}
    q["month_season_kw"] = {int(k): v for k, v in q.get("month_season_kw", {}).items()}
    raw["level_name"]    = {int(k): v for k, v in raw.get("level_name", {}).items()}
    # budget_tier: null → float("inf"), list → tuple
    tiers = q.get("budget_tier", [])
    q["budget_tier"] = tuple(
        (t[0], float("inf") if t[1] is None else t[1], t[2]) for t in tiers
    )
    return raw


def _load_country_files() -> dict[str, dict]:
    """data/*.json 전체 로드 → {country_code: data}"""
    result: dict[str, dict] = {}
    data_dir = _BASE_DIR / "data"
    if not data_dir.exists():
        return result
    for fp in data_dir.glob("*.json"):
        raw = _load_json(fp)
        code = raw.get("code")
        if code:
            # season_rules: list[list] → list[tuple]
            raw["season_rules"] = [tuple(r) for r in raw.get("season_rules", [])]
            result[code] = raw
    return result


# 설정 로드 (모듈 임포트 시 1회)
_API_CFG      = _load_api_config()
_VOCAB        = _load_vocab()
_COUNTRY_DATA = _load_country_files()

# --- API 엔드포인트 ---
_EP           = _API_CFG.get("endpoints", {})
_MOFA_VISA_URL  = _EP.get("mofa_visa",    "https://apis.data.go.kr/1262000/EntranceVisaService2/getEntranceVisaList2")
_MOFA_WARN_URL  = _EP.get("mofa_warning", "https://apis.data.go.kr/1262000/TravelWarningServiceV3/getTravelWarningListV3")
_KOREAEXIM_URL  = _EP.get("koreaexim",    "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON")
_NAVER_BLOG_URL = _EP.get("naver_blog",   "https://openapi.naver.com/v1/search/blog")

_TTL          = _API_CFG.get("ttl_seconds", {})
_HTTP_TIMEOUT_S = _API_CFG.get("http_timeout_seconds", 2.5)

# --- 국가별 데이터 뷰 (하위 호환 유지) ---
_STATIC: dict[str, dict]                           = {c: d["static"]        for c, d in _COUNTRY_DATA.items()}
_SEASON_RULES: dict[str, list]                     = {c: d["season_rules"]  for c, d in _COUNTRY_DATA.items()}
_CITY_META: dict[str, dict]                        = _COUNTRY_DATA.get("JP", {}).get("city_meta", {})
_COUNTRY_FILTER_KW: dict[str, list[str]]           = {c: d.get("filter_keywords", []) for c, d in _COUNTRY_DATA.items()}
_MOFA_COUNTRY_CODE: dict[str, str]                 = {c: d["mofa_iso3"]     for c, d in _COUNTRY_DATA.items()}

# --- 어휘·레이블 뷰 ---
_PURPOSE_KO: dict[str, str]  = _VOCAB.get("purpose_ko", {})
_QUERY_VOCAB: dict[str, Any] = _VOCAB.get("query", {})
_LEVEL_NAME_CFG: dict[int, str] = _VOCAB.get("level_name", {})


def _filter_by_country(posts: list[dict], country: str) -> list[dict]:
    keywords = _COUNTRY_FILTER_KW.get(country, [])
    if not keywords:
        return posts
    result = []
    for p in posts:
        text = (p.get("title", "") + " " + p.get("description", "")).lower()
        if any(kw.lower() in text for kw in keywords):
            result.append(p)
    return result


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
_EXTERNAL_JSON_TTL_S = _TTL.get("external_json", 86400)

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
_MOFA_VISA_TTL_S  = _TTL.get("mofa_visa",    21600)
_MOFA_ALERT_TTL_S = _TTL.get("mofa_warning", 3600)


_MOFA_VISA_ALL_KEY = "mofa_visa_all"  # 전국가 비자 목록 캐시 키


def _fetch_mofa_visa(country: str) -> dict:
    """
    - 외교부 입국허가요건 API 전체 목록을 캐시 후 country_iso_alp2 로 필터링하는 함수 (TTL 6h)
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

    # 전국가 목록 캐시 확인 (여러 국가 조회 시 API 중복 호출 방지)
    all_items = _cache.get(_MOFA_VISA_ALL_KEY)
    if all_items is None:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
                resp = client.get(
                    _MOFA_VISA_URL,
                    params={"serviceKey": api_key, "returnType": "JSON", "numOfRows": "300", "pageNo": "1"},
                )
                resp.raise_for_status()
                raw = resp.json().get("response", {}).get("body", {}).get("items", {}).get("item", [])
                if isinstance(raw, dict):
                    raw = [raw]
                all_items = {x["country_iso_alp2"]: x for x in raw if x.get("country_iso_alp2")}
                _cache.set(_MOFA_VISA_ALL_KEY, all_items, _MOFA_VISA_TTL_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MOFA 비자 API 실패: %s", exc)
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


_LEVEL_NAME = _LEVEL_NAME_CFG or {0: "미발령", 1: "여행유의", 2: "여행자제", 3: "출국권고", 4: "여행금지"}


# ===========================================================================
# 4-1) 수출입은행 환율 API (TTL 24h)
# ===========================================================================
_EXCHANGE_TTL_S = _TTL.get("koreaexim", 86400)
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
# 4-2) 네이버 블로그 검색 API (TTL 1h)
# ===========================================================================
_NAVER_BLOG_TTL_S = _TTL.get("naver_blog", 3600)


def _fetch_naver_blog(query: str, display: int = 5) -> list[dict]:
    """
    - 네이버 블로그 검색 API 로 여행 후기를 검색하는 함수 (TTL 1h)
      description 의 HTML 태그를 제거해 정제된 텍스트로 반환.
    ### Args:
      - query(str): 검색 쿼리
      - display(int): 반환할 결과 수 (최대 10)
    ### Returns:
      - items(list[dict]): [{'title', 'link', 'description', 'bloggername'}] 형태
    """
    cache_key = f"naver_blog:{query}:{display}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    client_id     = os.getenv("NAVER_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        logger.warning("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
        return []

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.get(
                _NAVER_BLOG_URL,
                params={"query": query, "display": display, "sort": "sim"},
                headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("네이버 블로그 검색 실패 (%s): %s", query, exc)
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


# ===========================================================================
# 5) 툴 7개
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
        lines = ["# 일본 대표 도시 가이드\n"]
        for key, c in cities.items():
            meta = _CITY_META.get(key, {})
            tags     = "·".join(meta.get("tags", [])[:3])
            best_for = "·".join(_PURPOSE_KO.get(p, p) for p in meta.get("best_for", []))
            lines.append(
                f"- **{c['name_ko']}** ({c['name_local']})  `city='{key}'`\n"
                f"  - 특징: {tags or '-'}  |  추천: {best_for or '-'}"
            )
        lines.append("\n> 도시를 지정하면 카테고리별 스팟 목록을 보여드립니다.")
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


@mcp.tool(annotations={"title": "Itinerary Recommendation", "openWorldHint": True, **_READONLY})
def recommend_itinerary(
    country: SupportedCountry,
    depart_date: str,
    return_date: str,
    budget_krw: int,
    purpose: Literal["family", "couple", "friends", "solo"],
    num_people: int,
    city: Optional[str] = None,
) -> str:
    """
    Recommends a personalized travel itinerary by searching real traveler blog
    posts via the Naver Blog API from Travel Briefing(트래블 브리핑).
    Results are based on actual trip reports matching the given schedule, budget,
    purpose, and group size. Optionally filter by city for more specific results.

    - 여행 조건(일정·예산·목적·인원·도시)을 받아 네이버 블로그 후기 기반 맞춤 추천을 반환하는 함수
    ### Args:
      - country(SupportedCountry): 국가 코드 ('JP')
      - depart_date(str): 출국일 'YYYY-MM-DD'
      - return_date(str): 귀국일 'YYYY-MM-DD'
      - budget_krw(int): 1인당 예산 (원, 예: 1000000)
      - purpose(str): 여행 목적 'family'|'couple'|'friends'|'solo'
      - num_people(int): 여행 인원 수
      - city(Optional[str]): 도시 키 (예: 'tokyo', 'osaka'). 미지정 시 국가 전체 검색
    ### Returns:
      - md(str): 조건 요약 + 블로그 후기 목록 마크다운 (상위 8건)
    """
    static = _STATIC[country]
    try:
        dep = datetime.strptime(depart_date, "%Y-%m-%d").date()
        ret = datetime.strptime(return_date, "%Y-%m-%d").date()
        nights = (ret - dep).days
    except ValueError:
        dep = date.today()
        nights = 3

    purpose_ko = _PURPOSE_KO.get(purpose, purpose)
    budget_str = f"{budget_krw // 10000}만원"
    nights_str = f"{nights}박{nights + 1}일"
    query = _build_naver_query(
        country=country, nights=nights, purpose=purpose,
        budget_krw=budget_krw, depart_month=dep.month, city=city,
    )

    posts = _filter_by_country(_fetch_naver_blog(query, display=10), country)
    return _render_itinerary_md(static, query, posts, depart_date, return_date,
                                 budget_str, purpose_ko, num_people, nights_str, city)


# ===========================================================================
# 6) 내부 헬퍼
# ===========================================================================
def _build_naver_query(
    country: str, nights: int, purpose: str,
    budget_krw: int, depart_month: int, city: Optional[str] = None,
) -> str:
    """
    - 여행 조건을 조합해 네이버 블로그 검색 최적 쿼리를 생성하는 함수
      예산·인원수는 쿼리에서 제외(타국 후기 유입 원인). 국가+도시+시즌+목적으로 고정.
    ### Args:
      - country(str): 국가 코드
      - nights(int): 숙박 수
      - purpose(str): 여행 목적 코드
      - budget_krw(int): 1인 예산 (예산 티어 레이블 결정용)
      - depart_month(int): 출발 월 (시즌 키워드 결정용)
      - city(Optional[str]): 도시 키 (None이면 국가명만)
    ### Returns:
      - query(str): 네이버 블로그 검색 쿼리
    """
    static = _STATIC[country]

    # 장소 키워드
    if city and city in _CITY_META:
        place = f"{static['name_ko']} {_CITY_META[city]['name_ko']}"
    else:
        place = static["name_ko"]

    nights_str   = f"{nights}박{nights + 1}일"
    purpose_kw   = _QUERY_VOCAB["purpose"].get(purpose, [_PURPOSE_KO.get(purpose, "")])[0]
    season_kw    = _QUERY_VOCAB["month_season_kw"].get(depart_month, "여행")

    # 예산 티어 레이블 (중간 구간은 공란 → 쿼리 미포함)
    budget_label = ""
    for low, high, label in _QUERY_VOCAB["budget_tier"]:
        if low <= budget_krw < high:
            budget_label = label
            break

    parts = [place, nights_str, purpose_kw, season_kw]
    if budget_label:
        parts.append(budget_label)
    parts.append("여행후기")
    return " ".join(p for p in parts if p)
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


def _render_itinerary_md(
    static: dict, query: str, posts: list[dict],
    depart: str, ret: str, budget_str: str,
    purpose_ko: str, num_people: int, nights_str: str,
    city: Optional[str] = None,
) -> str:
    """
    - 네이버 블로그 검색 결과를 정제 마크다운으로 렌더링하는 함수
    ### Args:
      - static(dict): 국가 정적 정보
      - query(str): 실제 검색에 사용된 쿼리
      - posts(list[dict]): _fetch_naver_blog 결과
      - 나머지: 조건 표시용
      - city(Optional[str]): 도시 키 (헤더 표시용)
    ### Returns:
      - md(str): 조건 요약 + 블로그 후기 목록 마크다운
    """
    city_label = ""
    if city and city in _CITY_META:
        city_label = f" · {_CITY_META[city]['name_ko']}"
    lines = [
        f"# {static['name_ko']}{city_label} 맞춤 여행 추천",
        "",
        f"**일정**: {depart} ~ {ret} ({nights_str})  "
        f"**인원**: {num_people}명  "
        f"**목적**: {purpose_ko}  "
        f"**1인 예산**: {budget_str}",
        "",
    ]

    if not posts:
        lines.append("> 블로그 검색 결과가 없습니다. 네이버 API 키를 확인하거나 잠시 후 다시 시도해주세요.")
        return "\n".join(lines)

    lines.append("## 📝 실제 여행자 후기")
    lines.append("")
    for i, p in enumerate(posts, 1):
        desc = p["description"][:60] + "…" if len(p["description"]) > 60 else p["description"]
        lines.append(f"**{i}. [{p['title']}]({p['link']})**")
        lines.append(f"- {desc}")
        lines.append("")

    lines.append(f"> 검색 쿼리: `{query}`")
    lines.append("> 블로그 내용은 작성자 개인 경험 기준이며 실제와 다를 수 있습니다.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
