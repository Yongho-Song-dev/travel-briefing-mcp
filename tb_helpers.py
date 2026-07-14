"""
tb_helpers — 내부 헬퍼 (쿼리 빌더 · 시즌 판정 · 마크다운 렌더러)

외부 호출 없이 순수 데이터 가공만 담당한다. tb_config 의 상수만 의존하며
tb_api 를 임포트하지 않는다 (렌더러에 필요한 동적 값은 인자로 받음).
"""

from __future__ import annotations
from typing import Optional
from datetime import date, datetime, timedelta

from tb_config import (
    _STATIC, _SEASON_RULES, _COUNTRY_FILTER_KW, _DOMESTIC_KW,
    _PURPOSE_KO, _PURPOSE_ANGLE, _PURPOSE_PLAN, _QUERY_VOCAB,
    city_meta, today_kst,
)


# ===========================================================================
# 1) 검색·판정 헬퍼
# ===========================================================================
_SEASON_KO = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}


def _kw_score(title: str, body: str, keywords: list[str]) -> int:
    """
    - 키워드 등장 횟수로 글의 성격을 점수화하는 함수 (제목은 본문보다 3배 가중)
    ### Args:
      - title(str) / body(str): 소문자로 변환된 제목·본문
      - keywords(list[str]): 채점 대상 키워드
    ### Returns:
      - score(int): 가중 합산 점수
    """
    return sum(3 * title.count(kw.lower()) + body.count(kw.lower()) for kw in keywords)


def _filter_by_country(posts: list[dict], country: str) -> list[dict]:
    """
    - 블로그 검색 결과에서 해당 국가와 무관한(국내 여행 등) 글을 제거하는 함수
      단순 포함 검사는 오탐이 난다 — 부산 여행기에 "부산역 근처 일본 가옥" 한 줄만 있어도
      '일본' 이 잡혀 통과했다. 그래서 국가 신호와 국내 신호를 점수로 비교해,
      국가 신호가 더 강한 글만 남긴다.
    ### Args:
      - posts(list[dict]): _fetch_naver_blog 결과
      - country(str): 국가 코드
    ### Returns:
      - result(list[dict]): 해당 국가 글만 남긴 목록
    """
    keywords = _COUNTRY_FILTER_KW.get(country, [])
    if not keywords:
        return posts

    result = []
    for p in posts:
        title = p.get("title", "").lower()
        body  = p.get("description", "").lower()
        abroad   = _kw_score(title, body, keywords)
        domestic = _kw_score(title, body, _DOMESTIC_KW)
        # 해외 신호가 없거나, 국내 신호가 더 강하면 제외 (동점이면 해외로 인정)
        if abroad and abroad >= domestic:
            result.append(p)
    return result


def resolve_trip_dates(
    depart_date: Optional[str] = None,
    return_date: Optional[str] = None,
    month: Optional[int] = None,
    nights: Optional[int] = None,
) -> dict:
    """
    - 사용자가 흘리듯 말한 일정("9월에", "4박5일")을 실제 날짜로 해석하는 함수
      카톡 사용자는 'YYYY-MM-DD' 로 말하지 않는다. 날짜를 필수 인수로 두면 호스트 LLM 이
      인수를 채우지 못해 툴 호출 자체를 포기하므로, 부분 정보만으로도 채워준다.
    ### Args:
      - depart_date(Optional[str]): 출국일 'YYYY-MM-DD'. 없으면 month 로 추정
      - return_date(Optional[str]): 귀국일 'YYYY-MM-DD'. 없으면 nights 로 계산
      - month(Optional[int]): 출발 월 1~12 ("9월에" → 9). 이미 지난 달이면 내년으로 해석
      - nights(Optional[int]): 숙박 수 ("4박5일" → 4)
    ### Returns:
      - result(dict): {'depart': date, 'return': date, 'nights': int, 'estimated': bool}
                      estimated=True 면 날짜를 추정한 것 (응답에 안내 문구 표시)
    """
    today = today_kst()
    estimated = False

    # 1) 출국일
    dep: Optional[date] = None
    if depart_date:
        try:
            dep = datetime.strptime(depart_date, "%Y-%m-%d").date()
        except ValueError:
            dep = None
    if dep is None:
        estimated = True
        if month and 1 <= month <= 12:
            # 이미 지난 달을 말했다면 내년 그 달로 해석 (7월에 "3월" → 내년 3월)
            year = today.year if month >= today.month else today.year + 1
            dep = date(year, month, 15)          # 월 중순 = 그 달의 대표값
        else:
            dep = today + timedelta(days=30)     # 아무 단서 없으면 한 달 뒤

    # 2) 숙박 수 → 귀국일
    n: Optional[int] = None
    if return_date:
        try:
            n = (datetime.strptime(return_date, "%Y-%m-%d").date() - dep).days
        except ValueError:
            n = None
    if n is None or n <= 0:
        if not (nights and nights > 0):
            estimated = True
        n = nights if (nights and nights > 0) else 3   # 기본 3박4일

    return {"depart": dep, "return": dep + timedelta(days=n),
            "nights": n, "estimated": estimated}


def _build_naver_query(
    country: str, nights: int, purpose: Optional[str],
    budget_krw: Optional[int], depart_month: int, city: Optional[str] = None,
) -> str:
    """
    - 여행 조건을 조합해 네이버 블로그 검색 최적 쿼리를 생성하는 함수
      예산·인원수는 쿼리에서 제외(타국 후기 유입 원인). 국가+도시+시즌+목적으로 고정.
    ### Args:
      - country(str): 국가 코드
      - nights(int): 숙박 수
      - purpose(Optional[str]): 여행 목적 코드. None 이면 쿼리에서 생략
      - budget_krw(int): 1인 예산 (예산 티어 레이블 결정용)
      - depart_month(int): 출발 월 (시즌 키워드 결정용)
      - city(Optional[str]): 도시 키 (None이면 국가명만)
    ### Returns:
      - query(str): 네이버 블로그 검색 쿼리
    """
    static = _STATIC[country]
    cities = city_meta(country)

    # 장소 키워드 — 도시명이 국가명과 같으면(싱가포르 등) 중복 표기 제거
    if city and city in cities:
        city_ko = cities[city]["name_ko"]
        place = city_ko if city_ko == static["name_ko"] else f"{static['name_ko']} {city_ko}"
    else:
        place = static["name_ko"]

    nights_str   = f"{nights}박{nights + 1}일"
    purpose_kw   = _QUERY_VOCAB["purpose"].get(purpose, [""])[0] if purpose else ""
    season_kw    = _QUERY_VOCAB["month_season_kw"].get(depart_month, "여행")

    # 예산 티어 레이블 (중간 구간은 공란 → 쿼리 미포함). 예산 미지정이면 생략
    budget_label = ""
    if budget_krw:
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


# ===========================================================================
# 2) 마크다운 렌더러 (result 는 정제 마크다운 — 가이드 준수)
# ===========================================================================
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


def _exchange_line(exch: dict) -> Optional[str]:
    """
    - 환율 조회 결과를 한 줄 요약으로 만드는 함수 (체크리스트·일정추천 요약용)
      미고시 통화는 USD 기준으로 이중환전 안내.
    ### Args:
      - exch(dict): get_exchange_for_country 결과
    ### Returns:
      - line(Optional[str]): 한 줄 요약. 조회 실패 시 None
    """
    if exch.get("quoted"):
        r = exch.get("rate")
        if not r:
            return None
        basis = f"{r['search_date']} 고시" if r.get("is_stale") else "매매기준율"
        return f"{r['currency']} = 약 {r['deal_bas_r']:,.2f} 원 ({basis})"
    usd = exch.get("usd")
    if not usd:
        return None
    return (f"{exch['currency']} 는 수출입은행 미고시 — USD 1달러 = 약 "
            f"{usd['deal_bas_r']:,.2f} 원 (현지에서 달러→현지통화 환전 권장)")


def _render_exchange_md(static: dict, exch: dict) -> str:
    """
    - 환율 조회 결과를 정제 마크다운으로 렌더링하는 함수
      수출입은행 미고시 통화(VND/PHP/TWD)는 USD 기준 + 이중환전 팁으로 대체.
    ### Args:
      - static(dict): 국가 정적 정보
      - exch(dict): get_exchange_for_country 결과
    ### Returns:
      - md(str): 환율 마크다운 (조회 실패 시 안내)
    """
    header = f"# {static['name_ko']} 환율\n"

    if not exch.get("quoted"):
        usd = exch.get("usd")
        if not usd:
            return header + "\n> 환율 조회 실패. 잠시 후 다시 시도해주세요."
        return (
            f"{header}\n"
            f"**{exch['currency']}** 는 한국수출입은행 고시 대상이 아닙니다.\n"
            f"**USD 1달러** = 약 **{usd['deal_bas_r']:,.2f} 원** ({usd['search_date']} 기준 매매기준율)\n\n"
            f"**💡 환전 팁**: 한국에서 미국 달러로 환전 후 현지에서 {exch['currency']} 로 재환전하는 방식이 "
            f"일반적으로 유리합니다. 공항보다 시내 사설 환전소 환율이 좋은 편입니다.\n\n"
            f"> 실거래 환율은 은행·환전소별로 상이. 참고용."
        )

    result = exch.get("rate")
    if not result:
        return header + "\n> 환율 조회 실패. 잠시 후 다시 시도해주세요."

    # 주말·공휴일이나 당일 고시 전에는 직전 영업일 값이 나온다 — 기준일을 정확히 밝힌다
    if result.get("is_stale"):
        basis = f"{result['search_date']} 고시 기준"
        note  = "\n> ⚠️ 오늘 고시가 아직 없어 직전 영업일 매매기준율입니다(주말·공휴일·고시 전)."
    else:
        basis = f"{result['search_date']} 당일 고시"
        note  = ""

    return (
        f"{header}\n"
        f"**{result['currency']}** = 약 **{result['deal_bas_r']:,.2f} 원** (매매기준율, {basis})\n"
        f"{note}\n"
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


def _render_destinations_md(
    city_data: dict, spots: list[dict], category_filter: Optional[str],
    last_reviewed: str = "-",
) -> str:
    """
    - 도시 스팟 목록을 카카오맵 검색 링크와 상태 배지 포함해 마크다운으로 렌더링하는 함수
      status='under_renovation' 은 명시적으로 표시해 오탐을 줄인다.
    ### Args:
      - city_data(dict): destinations_jp.json 의 cities.<key>
      - spots(list[dict]): 필터링된 스팟 리스트
      - category_filter(Optional[str]): 적용된 카테고리(None 이면 전체)
      - last_reviewed(str): 큐레이션 JSON 의 최종 검토일 (푸터 표시용)
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

    lines.append(f"> 큐레이션 데이터 최종 검토일: {last_reviewed}. "
                 f"방문 전 공식 사이트에서 운영 여부를 재확인하세요.")
    return "\n".join(lines)


def _render_city_guide_md(country: str, category_hint: Optional[str] = None) -> str:
    """
    - 큐레이션 JSON 이 없는 국가의 도시 가이드를 city_meta 로 렌더링하는 함수
      (JP 는 스팟 단위 큐레이션, 그 외 국가는 도시 단위 가이드 + 시즌 하이라이트)
    ### Args:
      - country(str): 국가 코드
      - category_hint(Optional[str]): 카테고리 필터 요청 시 안내 문구용
    ### Returns:
      - md(str): 도시별 특징·추천대상·시즌 하이라이트 마크다운
    """
    static = _STATIC[country]
    cities = city_meta(country)
    if not cities:
        return (f"# {static['name_ko']} 도시 가이드\n\n"
                f"> 아직 도시 데이터가 준비되지 않았습니다.")

    lines = [f"# {static['name_ko']} 대표 도시 가이드", ""]
    for key, m in cities.items():
        tags     = "·".join(m.get("tags", [])[:4])
        best_for = "·".join(_PURPOSE_KO.get(p, p) for p in m.get("best_for", []))
        lines.append(f"## {m['name_ko']} ({m['name_local']})  `city='{key}'`")
        lines.append(f"- **특징**: {tags or '-'}")
        lines.append(f"- **추천 대상**: {best_for or '-'}")
        sh = m.get("season_highlight", {})
        if sh:
            season_line = " · ".join(
                f"{label} {sh[k]}"
                for k, label in (("spring", "봄"), ("summer", "여름"),
                                 ("autumn", "가을"), ("winter", "겨울"))
                if sh.get(k)
            )
            lines.append(f"- **시즌별**: {season_line}")
        lines.append("")

    if category_hint:
        lines.append(f"> 카테고리 필터(`{category_hint}`)는 큐레이션 스팟이 있는 일본에서만 지원합니다.")
    lines.append(f"> 스팟 단위 상세 큐레이션은 현재 일본만 제공됩니다. "
                 f"`recommend_itinerary` 로 실제 여행자 후기 기반 추천을 받아보세요.")
    return "\n".join(lines)


def _render_city_picks_md(
    country: str, purpose: Optional[str], depart_month: int, limit: int = 4,
) -> list[str]:
    """
    - 도시를 지정하지 않은 사용자에게 "어디로 갈지"를 제안하는 함수
      "혼자 어디가 좋아?" 같은 질문의 핵심은 목적지 자체다. 목적(best_for)이 맞는 도시를
      우선 노출하고, 출발 월의 시즌 하이라이트를 붙여 선택 근거를 준다.
    ### Args:
      - country(str): 국가 코드
      - purpose(Optional[str]): 여행 목적. 맞는 도시를 앞으로 정렬
      - depart_month(int): 출발 월 (시즌 하이라이트 선택용)
      - limit(int): 최대 노출 도시 수
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (도시 데이터 없으면 빈 리스트)
    """
    cities = city_meta(country)
    if not cities:
        return []

    season_key = _QUERY_VOCAB.get("month_to_season", {}).get(depart_month)

    # 목적이 맞는 도시를 앞에, 나머지는 뒤에 (목적 미지정이면 원래 순서)
    matched = [(k, m) for k, m in cities.items() if purpose and purpose in m.get("best_for", [])]
    others  = [(k, m) for k, m in cities.items() if (k, m) not in matched]
    picked  = (matched + others)[:limit]
    if not picked:
        return []

    head = "## 📍 어디로 갈까요?"
    if purpose:
        head += f" ({_PURPOSE_KO.get(purpose, purpose)}에 잘 맞는 도시)"
    lines = [head]
    # season_highlight 는 4계절 단위라 월 단위 정밀도가 없다 (9월은 autumn 이지만
    # 단풍은 10~11월). "이 시기엔" 이라고 단정하지 말고 계절 하이라이트로만 제시한다.
    season_ko = _SEASON_KO.get(season_key, "")
    for key, m in picked:
        tags = "·".join(m.get("tags", [])[:3])
        hl   = m.get("season_highlight", {}).get(season_key) if season_key else None
        line = f"- **{m['name_ko']}** — {tags}"
        if hl and season_ko:
            line += f" / {season_ko} 하이라이트: **{hl}**"
        lines.append(line)
    lines.append("")
    lines.append(f"> 도시를 정하면 그 도시 기준으로 다시 추천해 드려요. "
                 f"(예: \"{picked[0][1]['name_ko']}로 갈래\")")
    lines.append("")
    return lines


def _pick_spots_for_purpose(spots: list[dict], purpose: Optional[str], want: int) -> list[dict]:
    """
    - 목적에 맞는 카테고리 우선순위로 스팟을 고르는 함수
      폐업 리스크가 낮은 landmark 를 앞세우고, 카테고리를 번갈아 담아 하루가 단조롭지 않게 한다.
    ### Args:
      - spots(list[dict]): 도시의 전체 스팟
      - purpose(Optional[str]): 여행 목적. None 이면 기본 순서
      - want(int): 필요한 스팟 수
    ### Returns:
      - picked(list[dict]): 고른 스팟 (want 개 이하)
    """
    plan  = _PURPOSE_PLAN.get(purpose or "", {})
    order = plan.get("categories") or ["culture", "food", "nature", "shopping", "onsen"]

    # 운영 중단된 곳은 코스에서 제외 (get_destinations 는 뱃지로 표시하지만 코스엔 넣지 않음)
    alive = [s for s in spots if s.get("status") != "under_renovation"]

    by_cat: dict[str, list[dict]] = {}
    for s in alive:
        by_cat.setdefault(s["category"], []).append(s)
    for items in by_cat.values():   # landmark 우선 (수십 년 안정)
        items.sort(key=lambda x: 0 if x.get("stability") == "landmark" else 1)

    picked: list[dict] = []
    while len(picked) < want:
        added = False
        for cat in order:                      # 카테고리를 돌아가며 하나씩
            if by_cat.get(cat):
                picked.append(by_cat[cat].pop(0))
                added = True
                if len(picked) >= want:
                    break
        if not added:                          # 스팟이 동남
            break
    return picked


def _render_day_plan_md(
    city_ko: str, spots: list[dict], purpose: Optional[str], nights: int,
) -> list[str]:
    """
    - 큐레이션 스팟을 일자별 코스 뼈대로 배치하는 함수
      호스트 LLM 이 시간·식사·이동 같은 살을 붙일 수 있도록 '재료와 골격'만 제공한다.
      (서버가 완성된 문장을 쓰지 않는다 — 추론은 호스트 LLM 담당)
    ### Args:
      - city_ko(str): 도시 한글명
      - spots(list[dict]): 도시의 전체 스팟 (destinations JSON)
      - purpose(Optional[str]): 여행 목적
      - nights(int): 숙박 수
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (스팟 없으면 빈 리스트)
    """
    if not spots:
        return []

    plan     = _PURPOSE_PLAN.get(purpose or "", {})
    per_day  = plan.get("spots_per_day", 3)
    days     = nights + 1

    # 첫날은 도착, 마지막 날은 귀국이라 일정을 줄인다
    quota   = [max(per_day - 1, 1)] + [per_day] * max(days - 2, 0)
    if days >= 2:
        quota.append(max(per_day - 1, 1))

    picked = _pick_spots_for_purpose(spots, purpose, sum(quota))
    if not picked:
        return []

    # 스팟이 요청 일수보다 모자라면 균등 재분배 — 안 그러면 마지막 날이 통째로 빠진다
    if len(picked) < sum(quota):
        base, rem = divmod(len(picked), days)
        quota = [base + (1 if i < rem else 0) for i in range(days)]

    labels = ["도착 · 시내 적응"] + ["핵심 관광"] * max(days - 2, 0)
    if days >= 2:
        labels.append("여유롭게 마무리 · 귀국")

    purpose_ko = _PURPOSE_KO.get(purpose, "") if purpose else ""
    head = f"## 🗓 {nights}박{days}일 코스 제안 ({city_ko}"
    head += f" · {purpose_ko})" if purpose_ko else ")"
    lines = [head]

    idx = 0
    for day in range(days):
        take = quota[day] if day < len(quota) else per_day
        todays = picked[idx:idx + take]
        idx += take
        lines.append(f"**Day {day + 1}** — {labels[day] if day < len(labels) else '자유 일정'}")
        if todays:
            for s in todays:
                link = f"https://map.kakao.com/?q={s['search_query']}"
                lines.append(f"- [{s['name_ko']}]({link}) — {s['one_liner']}")
        else:
            # 큐레이션 스팟이 일수보다 적은 경우 — 날짜를 빠뜨리지 않고 여백으로 남긴다
            lines.append("- 자유 일정 (쇼핑·카페·근교 당일치기 등)")
        lines.append("")

    note = plan.get("note")
    if note:
        lines.append(f"> 💡 {note}")
    lines.append("> 순서·시간은 숙소 위치에 맞춰 조정하세요. 아래 실제 후기도 함께 참고하시면 좋습니다.")
    lines.append("")
    return lines


def _render_country_traits_md(country: str, city: Optional[str], depart_month: int) -> list[str]:
    """
    - 국가·도시 특징(여행 팁 + 시즌 하이라이트)을 마크다운 라인 목록으로 만드는 함수
      일정 추천에 국가별 색깔(중국=결제앱/VPN, 태국=복장, 인니=비자 등)을 넣기 위함.
    ### Args:
      - country(str): 국가 코드
      - city(Optional[str]): 도시 키. 지정 시 해당 도시의 시즌 하이라이트 포함
      - depart_month(int): 출발 월 (시즌 하이라이트 선택용)
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (없으면 빈 리스트)
    """
    static = _STATIC.get(country, {})
    lines: list[str] = []

    # 도시 시즌 하이라이트 — 출발 월의 계절에 맞는 것만
    season_key = _QUERY_VOCAB.get("month_to_season", {}).get(depart_month)
    cities = city_meta(country)
    if city and city in cities and season_key:
        highlight = cities[city].get("season_highlight", {}).get(season_key)
        if highlight:
            season_ko = _SEASON_KO.get(season_key, "")
            lines.append(f"- 🗓 **{season_ko} 하이라이트**: {highlight}")

    tips = static.get("travel_tips", [])
    for t in tips:
        lines.append(f"- ℹ️ {t}")

    if lines:
        return [f"## 🧭 {static.get('name_ko', country)} 여행 특징", *lines, ""]
    return []


def _render_itinerary_md(
    country: str, query: str, posts: list[dict],
    depart: str, ret: str, budget_str: Optional[str],
    purpose: Optional[str], num_people: Optional[int], nights_str: str,
    city: Optional[str] = None,
    exch: Optional[dict] = None,
    depart_month: int = 1,
    estimated: bool = False,
    spots: Optional[list[dict]] = None,
    nights: int = 3,
) -> str:
    """
    - 블로그 후기 + 목적별 방향 + 환율 + 국가 특징을 정제 마크다운으로 렌더링하는 함수
      목적이 있으면 그 목적의 방향 3개, 없으면 4가지 목적의 방향을 모두 제시.
    ### Args:
      - country(str): 국가 코드
      - query(str): 실제 검색에 사용된 쿼리
      - posts(list[dict]): _fetch_naver_blog 결과
      - purpose(Optional[str]): 여행 목적 코드. None 이면 전체 방향 제시
      - num_people(Optional[int]): 인원 수. None 이면 표시 생략
      - exch(Optional[dict]): get_exchange_for_country 결과 (예산 감각용)
      - depart_month(int): 출발 월 (시즌 하이라이트용)
      - 나머지: 조건 표시용
    ### Returns:
      - md(str): 조건 요약 + 환율 + 여행 방향 + 국가 특징 + 블로그 후기 마크다운
    """
    static = _STATIC[country]
    cities = city_meta(country)

    city_label = ""
    if city and city in cities:
        city_ko = cities[city]["name_ko"]
        if city_ko != static["name_ko"]:  # 도시국가(싱가포르) 중복 표기 방지
            city_label = f" · {city_ko}"

    cond = [f"**일정**: {depart} ~ {ret} ({nights_str})"]
    if num_people:
        cond.append(f"**인원**: {num_people}명")
    if purpose:
        cond.append(f"**목적**: {_PURPOSE_KO.get(purpose, purpose)}")
    if budget_str:
        cond.append(f"**1인 예산**: {budget_str}")

    lines = [
        f"# {static['name_ko']}{city_label} 맞춤 여행 추천",
        "",
        "  ".join(cond),
        "",
    ]
    if estimated:
        lines.append("> 📅 정확한 날짜를 알려주시면 시즌·항공 정보가 더 정확해집니다.")
        lines.append("")

    # 환율 — 현지 물가 감각을 잡는 데 필요 (예산 배분의 기준)
    if exch:
        exch_line = _exchange_line(exch)
        if exch_line:
            lines.append(f"**💱 환율**: {exch_line}")
            lines.append("")

    # 도시 미지정 = "어디 가면 좋아?" 가 질문의 핵심 → 목적에 맞는 도시부터 제안
    if not city:
        lines.extend(_render_city_picks_md(country, purpose, depart_month))
    # 도시가 정해졌고 큐레이션 스팟이 있으면 → 일자별 코스 뼈대를 재료로 제공
    elif spots:
        city_ko = cities.get(city, {}).get("name_ko", city)
        lines.extend(_render_day_plan_md(city_ko, spots, purpose, nights))

    # 목적별 여행 방향 — 같은 목적이라도 세부 방향(데이트/휴식/취미 등)에 따라
    # 갈 곳이 달라지므로 방향을 함께 제시해 호스트 LLM 이 좁혀가게 함
    if purpose and purpose in _PURPOSE_ANGLE:
        pa = _PURPOSE_ANGLE[purpose]
        lines.append(f"## 🎯 {pa['focus']} 방향 제안")
        for a in pa["angles"]:
            lines.append(f"- {a}")
        lines.append("")
    elif _PURPOSE_ANGLE:
        lines.append("## 🎯 어떤 여행을 원하세요?")
        lines.append("목적에 따라 추천 장소가 달라집니다. 아래에서 방향을 골라주세요.")
        for code, pa in _PURPOSE_ANGLE.items():
            first = pa["angles"][0] if pa.get("angles") else ""
            lines.append(f"- **{_PURPOSE_KO.get(code, code)}** ({pa['focus']}): {first}")
        lines.append("")

    # 국가·도시 특징 (결제 수단·복장·비자 등 나라별 색깔)
    lines.extend(_render_country_traits_md(country, city, depart_month))

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
