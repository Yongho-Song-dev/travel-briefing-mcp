"""
tb_scheduler — 스케줄 워밍 (지정 시각마다 외부 API 를 선제 갱신하는 데몬 스레드)

잡 종류 (스케줄은 config/api.json 의 warm_schedule):
  - koreaexim:  환율 전 통화 캐싱 (하루 7회 — 재고시·장애 대비)
  - mofa:       비자·여행경보 전국가 목록 (하루 2회 06/18시)
  - naver_blog: 인기 조합 블로그 쿼리 사전 검색 (하루 4회, 6h 간격)

새 워밍 잡 추가 방법: 함수 작성 → _WARM_JOBS 에 등록 → warm_schedule 에 시각 추가.
잡 실패는 _run_warm_job 이 격리하므로 스레드는 죽지 않는다.
"""

from __future__ import annotations
from typing import get_args
from datetime import date, datetime, timedelta
import threading
import time
import os

from tb_config import (
    logger, SupportedCountry,
    _API_CFG, _WARM_SCHEDULE, _QUERY_VOCAB, city_meta,
)
from tb_api import (
    _refresh_mofa_visa_all, _refresh_mofa_warn_all,
    _fetch_exchange_rows, _parse_exch_row, _store_exch, _exch_unit,
    _fetch_naver_blog,
)
from tb_helpers import _build_naver_query


# ===========================================================================
# 1) 워밍 잡
# ===========================================================================
def _warm_exchange_once() -> None:
    """
    - 전 지원 국가 통화의 환율을 API 1회 호출로 선제 캐싱하는 함수
      (rows 를 한 번 받아 통화별로 분배 — 통화 수만큼 호출하지 않음)
      USD 는 미고시 통화(VND/PHP/TWD) 국가의 이중환전 안내용으로 항상 캐싱.
    ### Args:
      - None
    ### Returns:
      - None (실패 시 기존 캐시 유지, 로그만 남김)
    """
    rows = _fetch_exchange_rows()
    if not rows:
        logger.info("환율 워밍 스킵 (주말·공휴일 빈 응답 또는 API 실패)")
        return
    by_unit = {r.get("cur_unit", ""): r for r in rows}

    units = {_exch_unit(c) for c in get_args(SupportedCountry)}
    units.discard(None)          # 미고시 통화 국가
    units.add("USD")             # 미고시 통화 폴백용

    warmed = 0
    for unit in units:
        row = by_unit.get(unit)
        if row is None:
            logger.warning("환율 워밍: 고시 목록에 없는 통화 (%s)", unit)
            continue
        result = _parse_exch_row(row)
        if result is not None:
            _store_exch(unit, result)
            warmed += 1
    logger.info("환율 워밍 완료 (%d/%d개 통화)", warmed, len(units))


def _warm_mofa_once() -> None:
    """
    - 비자·여행경보 전국가 목록을 강제 갱신하는 워밍 함수 (06/18시 실행)
      비자는 TTL 13h 라 다음 워밍까지 유지, 경보는 TTL 1h 로 온디맨드 갱신 병행.
    ### Args:
      - None
    ### Returns:
      - None (실패 시 네거티브 캐시 후 다음 스케줄에 재시도)
    """
    visa = _refresh_mofa_visa_all()
    warn = _refresh_mofa_warn_all()
    logger.info("MOFA 워밍: 비자 %s / 경보 %s",
                "OK" if visa else "실패", "OK" if warn else "실패")


def _warm_naver_blog_once() -> None:
    """
    - 인기 조합(도시 × 목적 × 박수 × 이번/다음달)의 블로그 쿼리를 선제 검색해 캐시에
      적재하는 워밍 함수. 사용자 요청이 캐시 히트로 즉시 응답되게 함.
      recommend_itinerary 와 동일한 쿼리 빌더·display 값을 사용해 캐시 키를 일치시킴.
    ### Args:
      - None
    ### Returns:
      - None (키 미설정 시 스킵, 개별 쿼리 실패는 네거티브 캐시에 위임)
    """
    if not (os.getenv("NAVER_CLIENT_ID", "").strip() and os.getenv("NAVER_CLIENT_SECRET", "").strip()):
        logger.info("네이버 블로그 워밍 스킵 (API 키 미설정)")
        return
    cfg        = _API_CFG.get("naver_warm", {})
    main_c     = cfg.get("main_country", "JP")
    budget     = cfg.get("budget_krw", 1_000_000)
    today      = date.today()
    months     = {today.month, today.month % 12 + 1}  # 이번달 + 다음달 (계획 단계 대비)
    purposes   = [None, *_QUERY_VOCAB.get("purpose", {})]

    # 메인 국가(일본)는 전 도시·전 박수, 보조 국가는 상위 N개 도시·대표 박수만.
    # (9개국 전 조합이면 쿼리가 1,500개 넘어 워밍 시간·쿼터 부담)
    main_nights = cfg.get("nights", [2, 3, 4])
    sub_nights  = cfg.get("secondary_nights", [3])
    sub_cities  = cfg.get("secondary_max_cities", 3)

    queries: set[str] = set()  # 같은 시즌 키워드 조합은 자동 중복 제거
    for country in get_args(SupportedCountry):
        is_main = country == main_c
        cities  = list(city_meta(country))
        targets = [None, *(cities if is_main else cities[:sub_cities])]
        nights_list = main_nights if is_main else sub_nights
        for month in months:
            for city in targets:
                for purpose in purposes:
                    for nights in nights_list:
                        queries.add(_build_naver_query(
                            country=country, nights=nights, purpose=purpose,
                            budget_krw=budget, depart_month=month, city=city,
                        ))
    warmed = 0
    for q in sorted(queries):
        if _fetch_naver_blog(q, display=10):
            warmed += 1
        time.sleep(0.05)  # 네이버 rate limit 여유 (백그라운드라 사용자 영향 없음)
    logger.info("네이버 블로그 워밍 완료 (%d/%d 쿼리 적재)", warmed, len(queries))


# 워밍 잡 레지스트리 (스케줄은 _WARM_SCHEDULE — config/api.json 의 warm_schedule)
_WARM_JOBS: dict = {
    "koreaexim":  _warm_exchange_once,
    "mofa":       _warm_mofa_once,
    "naver_blog": _warm_naver_blog_once,
}


# ===========================================================================
# 2) 스케줄러
# ===========================================================================
def _next_warm_at(hours: list[int], now: datetime) -> datetime:
    """
    - 시각 리스트에서 다음 실행 시각을 계산하는 함수
    ### Args:
      - hours(list[int]): 실행 시각 목록 (0~23)
      - now(datetime): 기준 시각
    ### Returns:
      - target(datetime): 오늘 남은 시각 중 가장 이른 것, 없으면 내일 첫 시각
    """
    cands = [now.replace(hour=h % 24, minute=0, second=0, microsecond=0)
             for h in sorted(set(h % 24 for h in hours))]
    future = [t for t in cands if t > now]
    return future[0] if future else min(cands) + timedelta(days=1)


def _run_warm_job(name: str, fn) -> None:
    """
    - 워밍 잡 1회 실행 — 예외를 격리해 스레드가 죽지 않게 하는 래퍼
    ### Args:
      - name(str): 잡 이름 (로그용)
      - fn(callable): 워밍 함수
    ### Returns:
      - None
    """
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("워밍 잡 실패 (%s): %s", name, exc)


def _warm_loop() -> None:
    """
    - 잡별 지정 시각(_WARM_SCHEDULE)에 워밍 함수를 실행하는 데몬 스레드 본체
      기동 직후 전 잡 1회 즉시 실행 후, 가장 가까운 다음 시각까지 대기를 반복.
    ### Args:
      - None
    ### Returns:
      - None (무한 루프, 데몬 스레드로 실행)
    """
    for name, fn in _WARM_JOBS.items():
        _run_warm_job(name, fn)
    while True:
        now = datetime.now()
        next_at = {name: _next_warm_at(hours, now)
                   for name, hours in _WARM_SCHEDULE.items()
                   if hours and name in _WARM_JOBS}
        if not next_at:
            return  # 스케줄 비어있음 — 스레드 종료
        target = min(next_at.values())
        time.sleep(max((target - datetime.now()).total_seconds(), 0) + 1)
        now = datetime.now()
        for name, at in next_at.items():
            if at <= now:
                _run_warm_job(name, _WARM_JOBS[name])


def start_warming() -> None:
    """
    - 워밍 데몬 스레드를 시작하는 함수 (서버 엔트리포인트에서 1회 호출)
      데몬 스레드라 서버 종료를 막지 않음.
    ### Args:
      - None
    ### Returns:
      - None
    """
    threading.Thread(target=_warm_loop, name="tb-warm", daemon=True).start()
