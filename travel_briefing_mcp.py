"""
Travel Briefing(트래블 브리핑) MCP 서버 - v9 (PlayMCP 가이드 2026.06.12 준수)

일본을 중심으로 중국·대만·동남아 6개국까지
  - 출발 전 준비(비자 동적·전압/시차/통화/긴급/대사관 등)
  - 현재 상황(외교부 경보 + 안전공지)
  - 환율
  - 항공 시즌 가이드 + 스카이스캐너 비교 링크
  - 도시별 관광지 큐레이션(GitHub Raw JSON, 24h 갱신)
  - 종합 D-7 체크리스트
  - 네이버 블로그 후기 기반 맞춤 일정 추천
를 제공한다.

[모듈 구조] — 의존 방향: tb_config ← tb_api ← tb_helpers ← tb_scheduler ← 이 파일
  - tb_config.py    설정 파일 로더 + 전역 상수 (config/, data/ JSON)
  - tb_api.py       외부 API 클라이언트 (TTL 캐시, MOFA·수출입은행·네이버·GitHub)
  - tb_helpers.py   내부 헬퍼 (쿼리 빌더 · 시즌 판정 · 마크다운 렌더러)
  - tb_scheduler.py 스케줄 워밍 (환율·MOFA·네이버 블로그 선제 갱신 데몬)
  - 이 파일         FastMCP 서버 + 툴 7개 + 엔트리포인트

[가이드 준수]
  - Streamable HTTP / Remote / Stateless
  - 서버·툴 이름에 'kakao' 미포함
  - 전 툴 read-only, annotations 5종 모두 지정
  - description 영문 + 서비스명 영/국문 병기
  - result는 정제 마크다운(API 원본 그대로 반환 금지)
  - 외부 API는 TTL 캐시 + 스케줄 워밍으로 p99 3,000ms 요건 대응
"""

from __future__ import annotations
from typing import Any, Callable, Literal, Optional
from datetime import date, datetime
import inspect
import os

from mcp.server.fastmcp import FastMCP

from tb_config import (
    SupportedCountry, CURATED_COUNTRIES, _STATIC, _PURPOSE_KO, city_meta,
)
from tb_api import (
    _fetch_pool,
    get_embassy, get_destinations_json, get_exchange_for_country,
    _fetch_mofa_visa, _fetch_mofa_alert, _fetch_naver_blog,
)
from tb_helpers import (
    _filter_by_country, _build_naver_query, _judge_season, _build_skyscanner_link,
    _exchange_line,
    _render_alert_md, _render_exchange_md, _render_briefing_md,
    _render_flight_md, _render_destinations_md, _render_city_guide_md,
    _render_itinerary_md,
)
from tb_scheduler import start_warming

# 컨테이너 환경에선 TB_HOST=0.0.0.0 으로 외부 바인딩 (기본은 로컬 전용 — 보안상 안전한 쪽)
# 포트: TB_PORT 우선, 없으면 PaaS 관례인 PORT 를 따른다 (PlayMCP 등 호스팅이 주입하는 포트 대응)
_PORT = os.getenv("TB_PORT") or os.getenv("PORT") or "8000"

mcp = FastMCP(
    "travel-briefing",
    host=os.getenv("TB_HOST", "127.0.0.1"),
    port=int(_PORT),
    json_response=True,
    stateless_http=True,
)

_READONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}

# docstring 에서 국문 내부 문서가 시작되는 지점 (CLAUDE.md 주석 컨벤션 기준)
#   영문 description(가이드·PlayMCP 노출용) → 국문 '- 함수 설명' / '### Args' / '### Returns'
_DOC_INTERNAL_MARKERS = ("- ", "###")


def _public_description(fn: Callable) -> str:
    """
    Extracts the English lead paragraph of a docstring as the public tool description.

    - docstring 에서 PlayMCP 에 노출할 영문 description 만 추출하는 함수
      국문 설명·Args·Returns 는 개발자용 내부 문서이므로 등록 description 에서 제외한다
      (노출 품질 + 1,024자 제한 + 호출 토큰 절감).
    ### Args:
      - fn(Callable): 툴 함수
    ### Returns:
      - description(str): 영문 설명 한 단락 (공백 정규화)
    """
    doc = inspect.getdoc(fn) or ""
    lines: list[str] = []
    for raw in doc.splitlines():
        line = raw.strip()
        if line.startswith(_DOC_INTERNAL_MARKERS):
            break
        lines.append(line)
    return " ".join(l for l in lines if l).strip()


def tool(**kwargs: Any) -> Callable:
    """
    Registers an MCP tool whose description carries only the English paragraph.

    - mcp.tool 래퍼 — description 을 _public_description 결과로 고정 등록하는 데코레이터
      (docstring 은 한 벌만 유지하고, 배포 노출용 설명만 자동으로 분리)
    ### Args:
      - kwargs(Any): mcp.tool 에 그대로 전달할 인자 (annotations 등)
    ### Returns:
      - decorator(Callable): 툴 등록 데코레이터
    """
    def decorator(fn: Callable) -> Callable:
        description = _public_description(fn)
        if not description:
            # 빈 description 을 넘기면 FastMCP 가 docstring 전체로 폴백해 국문 내부 문서가 노출됨
            raise ValueError(f"{fn.__name__}: docstring 첫 단락에 영문 description 이 필요합니다")
        return mcp.tool(description=description, **kwargs)(fn)
    return decorator


# ===========================================================================
# 툴 7개
# ===========================================================================
@tool(annotations={"title": "Trip Briefing", "openWorldHint": True, **_READONLY})
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
    embassy = get_embassy(country)

    visa_warning = ""
    mofa = _fetch_mofa_visa(country)
    visa = mofa.get("visa") or static["visa_static"]
    if not mofa:
        visa_warning = "\n> ⚠️ 비자 정보 실시간 갱신 실패 — 출발 전 외교부 영사콜(02-3210-0404) 재확인 필수"

    return _render_briefing_md(country, static, visa, embassy) + visa_warning


@tool(annotations={"title": "Current Status", "openWorldHint": True, **_READONLY})
def get_current_status(country: SupportedCountry) -> str:
    """
    Retrieves the current MOFA travel-advisory level, effective date, and regional
    advisory note for a destination from Travel Briefing(트래블 브리핑). The calling
    LLM should use these official signals when explaining the overall situation.

    - 외교부 여행경보 단계와 발효일을 반환하는 함수(뉴스는 v1 미포함)
    ### Args:
      - country(SupportedCountry): 국가 코드
    ### Returns:
      - md(str): 경보 단계 + 발효일 + 요약 마크다운. 종합 판단은 호출자 LLM
    """
    alert = _fetch_mofa_alert(country)
    return _render_alert_md(country, alert)


@tool(annotations={"title": "Exchange Rate", "openWorldHint": True, **_READONLY})
def get_exchange_rate(country: SupportedCountry) -> str:
    """
    Retrieves the current KRW-based exchange rate for a destination's currency
    from Travel Briefing(트래블 브리핑) via the Korea Eximbank API. For currencies
    not quoted by the bank (VND, PHP, TWD), returns the USD rate with double-exchange guidance.

    - 해당 국가 통화의 현재 원화 매매기준율을 반환하는 함수
      수출입은행 미고시 통화(베트남 동·필리핀 페소·대만 달러)는 USD 기준 + 이중환전 안내로 대체.
    ### Args:
      - country(SupportedCountry): 국가 코드
    ### Returns:
      - md(str): 현재 환율 마크다운 (실패 시 안내 문구)
    """
    return _render_exchange_md(_STATIC[country], get_exchange_for_country(country))


@tool(annotations={"title": "Flight Season Guide", "openWorldHint": False, **_READONLY})
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


@tool(annotations={"title": "Destinations", "openWorldHint": True, **_READONLY})
def get_destinations(
    country: SupportedCountry,
    city: Optional[str] = None,
    category: Optional[Literal["culture", "food", "nature", "shopping", "onsen"]] = None,
) -> str:
    """
    Retrieves travel destinations for a country from Travel Briefing(트래블 브리핑).
    Japan returns curated spot-level lists (culture/food/nature/shopping/onsen) with
    Kakao Map links; other supported countries (CN, TW, VN, TH, PH, SG, MY, ID) return
    a city-level guide with seasonal highlights and recommended traveler types.

    - 도시·카테고리별 여행지를 반환하는 함수
      일본: 스팟 단위 큐레이션 (외부 JSON, 24h 갱신 → 폐업·리뉴얼 반영)
      그 외 국가: city_meta 기반 도시 가이드 (특징·추천대상·시즌 하이라이트)
    ### Args:
      - country(SupportedCountry): 국가 코드
      - city(Optional[str]): 도시 키. None 이면 국가 내 대표 도시 목록 반환
                             (JP: tokyo/osaka/kyoto/fukuoka/sapporo/okinawa/hiroshima/nara,
                              CN: beijing/shanghai/xian/chengdu/guilin/qingdao,
                              TW: taipei/taichung/tainan/kaohsiung/hualien 등)
      - category(Optional): 카테고리 필터 (culture/food/nature/shopping/onsen). 일본만 지원
    ### Returns:
      - md(str): 도시·스팟을 정제된 마크다운으로 반환. under_renovation 인 곳은 표시.
    """
    # 큐레이션 JSON 이 없는 국가 → city_meta 기반 도시 가이드
    if country not in CURATED_COUNTRIES:
        return _render_city_guide_md(country, category_hint=category)

    data = get_destinations_json()
    cities_meta = city_meta(country)

    if not city:
        cities = data.get("cities", {})
        lines = [f"# {_STATIC[country]['name_ko']} 대표 도시 가이드\n"]
        for key, c in cities.items():
            meta = cities_meta.get(key, {})
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
    return _render_destinations_md(city_data, spots, category,
                                   last_reviewed=data.get("last_reviewed", "-"))


@tool(annotations={"title": "Pre-Trip Checklist", "openWorldHint": True, **_READONLY})
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
    # 외부 API 3종 병렬 조회 — 순차 시 최악 2.5s×3, 병렬 시 max 2.5s (p99 3s 준수)
    # 실패해도 다른 섹션은 살아있도록 개별 예외 처리
    f_visa  = _fetch_pool.submit(_fetch_mofa_visa, country)
    f_alert = _fetch_pool.submit(_fetch_mofa_alert, country)
    f_exch  = _fetch_pool.submit(get_exchange_for_country, country)
    try:
        visa = f_visa.result().get("visa") or static["visa_static"]
    except Exception:
        visa = static["visa_static"]
    try:
        alert = f_alert.result()
    except Exception:
        alert = {"level": 0, "level_name": "조회 실패", "issued_at": "-", "note": "-"}
    try:
        exch = f_exch.result()
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
        exch_line = _exchange_line(exch)
        if exch_line:
            lines.append(f"- **환율**: {exch_line}")
    lines.append(f"- **시즌**: {season['note']}")
    lines.append(f"- **긴급번호**: " + ", ".join(f"{k} {v}" for k, v in static['emergency'].items()))
    lines.append("")

    # 국가별 준비 사항 (전자입국카드·결제앱·비자 등 — 나라마다 다름)
    tips = static.get("travel_tips", [])
    if tips:
        lines.append(f"## 🧭 {static['name_ko']} 특이사항")
        for t in tips:
            lines.append(f"- {t}")
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


@tool(annotations={"title": "Itinerary Recommendation", "openWorldHint": True, **_READONLY})
def recommend_itinerary(
    country: SupportedCountry,
    depart_date: str,
    return_date: str,
    budget_krw: int,
    purpose: Optional[Literal["family", "couple", "friends", "solo"]] = None,
    num_people: Optional[int] = None,
    city: Optional[str] = None,
) -> str:
    """
    Recommends a personalized itinerary from Travel Briefing(트래블 브리핑) using
    travel dates, per-person budget, party size, trip purpose, current exchange-rate
    context, destination traits, and cleaned Naver Blog results. If purpose is omitted,
    returns several planning angles. Supports JP, CN, TW, VN, TH, PH, SG, MY, and ID.

    여행 조건과 정제된 블로그 후기를 조합해 맞춤 일정을 마크다운으로 추천합니다.
    Dates use YYYY-MM-DD; budget_krw is the per-person KRW budget; purpose is one of
    family, couple, friends, or solo; num_people and city are optional.
    """
    try:
        dep = datetime.strptime(depart_date, "%Y-%m-%d").date()
        ret = datetime.strptime(return_date, "%Y-%m-%d").date()
        nights = (ret - dep).days
    except ValueError:
        dep = date.today()
        nights = 3

    budget_str = f"{budget_krw // 10000}만원"
    nights_str = f"{nights}박{nights + 1}일"
    query = _build_naver_query(
        country=country, nights=nights, purpose=purpose,
        budget_krw=budget_krw, depart_month=dep.month, city=city,
    )

    # 블로그 검색과 환율을 병렬 조회 (환율은 워밍되어 있어 대부분 즉시 반환)
    f_posts = _fetch_pool.submit(_fetch_naver_blog, query, 5)
    f_exch  = _fetch_pool.submit(get_exchange_for_country, country)
    try:
        posts = _filter_by_country(f_posts.result(), country)
    except Exception:
        posts = []
    try:
        exch = f_exch.result()
    except Exception:
        exch = None

    return _render_itinerary_md(
        country, query, posts, depart_date, return_date,
        budget_str, purpose, num_people, nights_str, city,
        exch=exch, depart_month=dep.month,
    )


if __name__ == "__main__":
    # 스케줄 워밍 — 기동 즉시 1회 + 지정 시각마다 환율·MOFA·네이버 선제 갱신
    start_warming()
    mcp.run(transport="streamable-http")
