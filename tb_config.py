"""
tb_config — 설정 파일 로더 + 전역 상수

config/api.json, config/vocab.json, data/*.json 을 임포트 시 1회 로드해
다른 모듈이 공유하는 상수 뷰를 만든다. 이 모듈은 다른 tb_* 모듈을
임포트하지 않는다 (의존성 최하층).

의존 방향: tb_config ← tb_api ← tb_helpers ← tb_scheduler ← travel_briefing_mcp
"""

from __future__ import annotations
from typing import Literal, Any
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import logging

# 로컬 개발용 .env 자동 로드 (배포 환경에선 무시됨) — 최하층 모듈이라 가장 먼저 실행됨
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("travel-briefing")

# 일본이 메인, 나머지 8개국(중국·대만 + 동남아 6)도 전 툴 응답 가능.
# 큐레이션 스팟(destinations_jp.json)은 JP 만, 그 외 국가는 city_meta 기반 도시 가이드 제공.
SupportedCountry = Literal["JP", "CN", "TW", "VN", "TH", "PH", "SG", "MY", "ID"]

# 한국 사용자 대상 서비스 — '오늘'은 항상 KST 기준이어야 한다.
# 컨테이너가 UTC 로 뜨면 00~09시 KST 사이에 하루가 밀려 D-day 와 환율 고시일이 틀어진다.
# Dockerfile 에서 TZ=Asia/Seoul 을 주지만, 플랫폼이 덮어써도 안전하도록 코드에서도 고정한다.
KST = timezone(timedelta(hours=9))


def today_kst() -> date:
    """
    - 한국 표준시 기준 오늘 날짜를 반환하는 함수 (컨테이너 TZ 설정과 무관)
    ### Args:
      - None
    ### Returns:
      - today(date): KST 기준 오늘
    """
    return datetime.now(KST).date()

# 큐레이션 JSON 이 있는 국가 (get_destinations 가 스팟 단위 목록을 반환)
CURATED_COUNTRIES = ("JP",)

# ===========================================================================
# 외부 설정 파일 로더 (config/ + data/)
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

# --- HTTP·TTL (config/api.json 의 ttl_seconds 에서 관리) ---
_TTL            = _API_CFG.get("ttl_seconds", {})
_HTTP_TIMEOUT_S = _API_CFG.get("http_timeout_seconds", 2.0)
_NEGATIVE_TTL_S = _TTL.get("negative", 60)  # 실패 상태 캐시 — 연속 타임아웃으로 인한 p99 위반 방지

# 비자는 06/18시 워밍 + 13h 유지 (다음 워밍까지 재호출 없음), 경보는 1h 온디맨드 병행
_MOFA_VISA_TTL_S     = _TTL.get("mofa_visa",     46800)
_MOFA_ALERT_TTL_S    = _TTL.get("mofa_warning",  3600)
_EXCHANGE_TTL_S      = _TTL.get("koreaexim",     86400)
# 7h — 6h 간격 워밍이 만료 직전에 갱신하도록 1h 여유
_NAVER_BLOG_TTL_S    = _TTL.get("naver_blog",    25200)
_EXTERNAL_JSON_TTL_S = _TTL.get("external_json", 86400)

# 워밍 스케줄 (시각 리스트, 00시 = 자정) — config/api.json 의 warm_schedule 로 조정
_WARM_SCHEDULE: dict[str, list[int]] = _API_CFG.get("warm_schedule", {
    "koreaexim":  [0, 6, 10, 12, 14, 18, 20],  # 하루 단위 고시 — 재고시·장애 대비 다회
    "mofa":       [6, 18],                      # 비자·경보 — 하루 2회 선제 갱신
    "naver_blog": [0, 6, 12, 18],               # 인기 쿼리 사전 워밍 (TTL 7h 와 6h 간격 맞물림)
})

# --- 국가별 데이터 뷰 ---
_STATIC: dict[str, dict]                 = {c: d["static"]        for c, d in _COUNTRY_DATA.items()}
_SEASON_RULES: dict[str, list]           = {c: d["season_rules"]  for c, d in _COUNTRY_DATA.items()}
_COUNTRY_FILTER_KW: dict[str, list[str]] = {c: d.get("filter_keywords", []) for c, d in _COUNTRY_DATA.items()}
_MOFA_COUNTRY_CODE: dict[str, str]       = {c: d["mofa_iso3"]     for c, d in _COUNTRY_DATA.items()}

# 도시 메타는 국가별로 분리 — {country: {city_key: meta}}
_CITY_META_BY_COUNTRY: dict[str, dict] = {c: d.get("city_meta", {}) for c, d in _COUNTRY_DATA.items()}


def city_meta(country: str) -> dict:
    """
    - 국가의 도시 메타 dict 를 반환하는 접근자
    ### Args:
      - country(str): 국가 코드
    ### Returns:
      - meta(dict): {city_key: {name_ko, tags, best_for, season_highlight}}. 없으면 빈 dict
    """
    return _CITY_META_BY_COUNTRY.get(country, {})

# --- 어휘·레이블 뷰 ---
_PURPOSE_KO: dict[str, str]     = _VOCAB.get("purpose_ko", {})
_PURPOSE_ANGLE: dict[str, dict] = _VOCAB.get("purpose_angle", {})
_QUERY_VOCAB: dict[str, Any]    = _VOCAB.get("query", {})
_LEVEL_NAME: dict[int, str]     = _VOCAB.get("level_name", {}) or {
    0: "미발령", 1: "여행유의", 2: "여행자제", 3: "출국권고", 4: "여행금지",
}
