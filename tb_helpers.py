"""
tb_helpers — 내부 헬퍼 (쿼리 빌더 · 시즌 판정 · 마크다운 렌더러)

외부 호출 없이 순수 데이터 가공만 담당한다. tb_config 의 상수만 의존하며
tb_api 를 임포트하지 않는다 (렌더러에 필요한 동적 값은 인자로 받음).
"""

from __future__ import annotations
from typing import Optional
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
import re

from tb_config import (
    _STATIC, _SEASON_RULES, _COUNTRY_FILTER_KW, _DOMESTIC_KW,
    _PURPOSE_KO, _PURPOSE_ANGLE, _PURPOSE_PLAN, _PURPOSE_REASON, _PLAN_TIMING,
    _CITY_FOOD, _CITY_DAILY_COST, _QUERY_VOCAB, _LEVEL_NAME,
    city_meta, today_kst,
)


def _map_link(country: str, query: str) -> str:
    """
    - 모든 지원 국가의 스팟을 Google Maps 검색 링크로 생성하는 함수
      공식 Maps URL 형식(api=1)을 사용해 웹과 Google Maps 앱에서 동일하게 연다.
    ### Args:
      - country(str): 국가 코드
      - query(str): 검색어 (search_query)
    ### Returns:
      - url(str): 지도 검색 URL
    """
    # country 는 호출부 호환성과 향후 국가별 공급자 확장을 위해 유지한다.
    _ = country
    return f"https://www.google.com/maps/search/?{urlencode({'api': '1', 'query': query})}"


def _dwell_hint(spot: dict) -> str:
    """
    - 스팟의 예상 체류시간을 사람이 읽는 문구로 만드는 함수
      스팟에 visit_min 이 있으면 우선, 없으면 카테고리 기본값(분).
    ### Args:
      - spot(dict): 큐레이션 스팟
    ### Returns:
      - hint(str): '약 1시간 30분' 형태. 데이터 없으면 빈 문자열
    """
    m = spot.get("visit_min") or _PLAN_TIMING.get("dwell_min", {}).get(spot.get("category"))
    if not m:
        return ""
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"약 {h}시간 {mm}분"
    return f"약 {h}시간" if h else f"약 {mm}분"


def _transit_hint(prev: Optional[dict], cur: dict) -> str:
    """
    - 두 스팟 사이의 이동 힌트를 만드는 함수
      실제 소요는 인접 6분~원거리 50분까지 벌어져 단일 상수가 오히려 위험하고,
      전 스팟 쌍을 검증할 수도 없다. 그래서 이동시간을 숫자로 단정하지 않고
      같은 구역이면 도보권, 다른 구역이면 구글맵 경로검색을 유도한다.
      area 없는 스팟이면 힌트를 생략(빈 문자열).
    ### Args:
      - prev(Optional[dict]): 직전 스팟 (없으면 첫 방문지)
      - cur(dict): 현재 스팟
    ### Returns:
      - hint(str): 이동 안내 문자열 또는 판단 불가 시 빈 문자열
    """
    if not prev:
        return ""
    a1, a2 = prev.get("area"), cur.get("area")
    if not a1 or not a2:
        return ""
    if a1 == a2:
        return f"{a1} 구역 내 도보"
    return f"{a1}→{a2} 대중교통"


# ===========================================================================
# 1) 검색·판정 헬퍼
# ===========================================================================
_SEASON_KO = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}


def _city_token(value: str) -> str:
    """도시 키 비교용으로 공백·구분기호와 대소문자를 정규화한다."""
    return re.sub(r"[\s·._-]+", "", value).casefold()


def resolve_city_key(country: str, city: Optional[str]) -> Optional[str]:
    """
    - 내부 키·한국어명·현지어 도시명을 내부 도시 키로 변환하는 함수
    ### Args:
      - country(str): 국가 코드
      - city(Optional[str]): 사용자 또는 호스트 LLM 이 전달한 도시명
    ### Returns:
      - key(Optional[str]): 일치한 내부 키. 입력이 없거나 일치하지 않으면 None
    """
    if not city:
        return None
    wanted = _city_token(city)
    for key, meta in city_meta(country).items():
        candidates = [key, meta.get("name_ko", ""), meta.get("name_local", "")]
        candidates.extend(meta.get("aliases", []))
        if wanted in {_city_token(v) for v in candidates if v}:
            return key
    return None


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


def _city_match_keys(country: str, city: Optional[str]) -> list[str]:
    """
    - 도시명 매칭용 키워드 목록 (한국어명·현지어·별칭·키)을 소문자로 반환하는 함수
    ### Args:
      - country(str): 국가 코드
      - city(Optional[str]): 도시 키
    ### Returns:
      - keys(list[str]): 소문자 매칭 키. city 없으면 빈 리스트
    """
    if not city:
        return []
    meta = city_meta(country).get(city, {})
    keys = {city, meta.get("name_ko", ""), meta.get("name_local", ""), *meta.get("aliases", [])}
    return [k.lower() for k in keys if k]


def _filter_by_country(posts: list[dict], country: str, city: Optional[str] = None) -> list[dict]:
    """
    - 블로그 검색 결과에서 대상 국가·도시와 무관한 글을 제거하는 함수
      3단계 판정 (제목은 본문보다 3배 가중):
        1) 대상국 신호가 있어야 하고 국내(한국) 신호보다 강해야 함
        2) 다른 해외 국가 신호가 더 강하면 제외 — 베트남 검색에 '상하이' 글이 섞이는 문제
        3) 도시가 지정되면 그 도시명이 제목·본문에 있어야 함 — '다낭' 검색에 '나트랑' 글 제외
    ### Args:
      - posts(list[dict]): _fetch_naver_blog 결과
      - country(str): 국가 코드
      - city(Optional[str]): 도시 키. 지정 시 도시명 필수 매칭
    ### Returns:
      - result(list[dict]): 대상 국가·도시 글만 남긴 목록
    """
    keywords = _COUNTRY_FILTER_KW.get(country, [])
    if not keywords:
        return posts

    other_kw = [kw for c, kw in _COUNTRY_FILTER_KW.items() if c != country]
    city_keys = _city_match_keys(country, city)

    result = []
    for p in posts:
        title = p.get("title", "").lower()
        body  = p.get("description", "").lower()
        target   = _kw_score(title, body, keywords)
        domestic = _kw_score(title, body, _DOMESTIC_KW)
        if not target or target < domestic:
            continue
        # 다른 해외 국가 신호가 더 강하면 그 나라 글로 보고 제외
        other_max = max((_kw_score(title, body, kw) for kw in other_kw), default=0)
        if other_max > target:
            continue
        # 도시 지정 시 도시명이 제목·본문에 있어야 통과 (같은 국가 다른 도시 제거)
        if city_keys and not any(k in title or k in body for k in city_keys):
            continue
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
      - result(dict): {'depart': date, 'return': date, 'nights': int,
                       'estimated': bool, 'basis': 'exact'|'month'|'none'}
                      estimated=True 면 날짜를 추정한 것 (응답에 안내 문구 표시)
                      basis 는 무엇을 근거로 잡았는지 — 추정값을 확정 날짜처럼 보이지 않게 표기할 때 쓴다
    """
    today = today_kst()
    estimated = False
    basis = "exact"

    # 1) 출국일
    dep: Optional[date] = None
    if depart_date:
        try:
            dep = datetime.strptime(depart_date, "%Y-%m-%d").date()
        except ValueError:
            dep = None
    if dep is None:
        estimated = True
        basis = "month" if (month and 1 <= month <= 12) else "none"
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
            "nights": n, "estimated": estimated, "basis": basis,
            "month": month if basis == "month" else None}


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
# 일정 응답에 얹을 핵심 팁의 카테고리 우선순위 (안전·건강을 위로, 다양성 확보)
_TIP_SUMMARY_PRIORITY = ("safety", "health", "culture", "transport", "payment", "weather", "connectivity")


def _render_local_tips_summary(
    static: dict, max_n: int = 3, city_key: Optional[str] = None,
) -> list[str]:
    """
    - 상세 현지 팁에서 카테고리가 겹치지 않게 핵심 몇 개만 요약하는 함수
      '준비물+일정'을 한 번에 물어 recommend_itinerary 만 호출돼도 핵심 현지 팁이
      빠지지 않도록, 전체 반복 대신 카테고리별 대표 팁만 짧게 얹는다.
    ### Args:
      - static(dict): _STATIC[country]
      - max_n(int): 최대 노출 개수
      - city_key(Optional[str]): 도시 지정 시 도시 전용 팁 필터
    ### Returns:
      - lines(list[str]): 마크다운 라인 (팁 없으면 빈 리스트)
    """
    tips = [t for t in static.get("local_tips", [])
            if isinstance(t, dict) and t.get("title") and _tip_in_scope(t, city_key)]
    if not tips:
        return []
    picked: list[dict] = []
    seen: set[str] = set()
    for cat in _TIP_SUMMARY_PRIORITY:          # 카테고리 다양성 우선
        hit = next((t for t in tips if t.get("category") == cat and cat not in seen), None)
        if hit:
            picked.append(hit)
            seen.add(cat)
        if len(picked) >= max_n:
            break
    for t in tips:                              # 부족하면 순서대로 채움
        if len(picked) >= max_n:
            break
        if t not in picked:
            picked.append(t)

    lines = ["## 🧳 핵심 현지 팁"]
    for t in picked[:max_n]:
        action = t["details"][-1] if t.get("details") else ""
        lines.append(f"- **{t['title']}** — {action}" if action else f"- **{t['title']}**")
    lines.append("> 전체 현지 팁·예절은 준비물 안내(get_trip_briefing)에서 확인하세요.")
    lines.append("")
    return lines


def _tip_in_scope(tip: object, city_key: Optional[str]) -> bool:
    """도시 전용 팁(scope.cities)은 해당 도시일 때만 노출한다.

    발리 전용 차낭 사리 팁이 자카르타·족자 질문에 나오면 안 된다. 도시가 특정되지
    않았으면(country-only) 필터하지 않고 국가 전반 팁으로 모두 보여준다.
    """
    if not isinstance(tip, dict):
        return True
    scope = tip.get("scope") or {}
    cities = scope.get("cities")
    if not cities:                 # 전국 공통 팁
        return True
    if city_key is None:           # 도시 미지정 → 국가 전반 보기(필터 안 함)
        return True
    return city_key in cities


def _render_local_tips_md(
    static: dict, *, compact: bool = False, city_key: Optional[str] = None,
) -> str:
    """국가별 문화·안전·교통·결제 팁을 일관된 마크다운으로 렌더링한다.

    ``travel_tips`` 는 전자입국카드처럼 출발 전에 처리할 짧은 준비 항목이고,
    ``local_tips`` 는 현지에서 왜 조심해야 하는지와 실제 행동을 함께 설명한다.
    compact=True 이면 현재상황 응답이 너무 길어지지 않도록 행동 요령만 보여준다.
    city_key 를 주면 그 도시에 해당하는 팁만 필터한다(도시 전용 scope 팁 제어).
    """
    tips = [t for t in static.get("local_tips", []) if _tip_in_scope(t, city_key)]
    if not tips:
        return ""

    heading = static.get("local_tips_title") or f"{static.get('name_ko', '')} 현지 Tip"
    lines = [f"## 🧭 {heading}", ""]
    for index, tip in enumerate(tips, 1):
        if isinstance(tip, str):
            lines.append(f"{index}. {tip}")
            continue

        lines.append(f"{index}. **{tip.get('title', '현지 유의사항')}**")
        details = tip.get("details", [])
        if compact and details:
            details = details[-1:]
        for detail in details:
            lines.append(f"   - {detail}")

    reviewed = static.get("local_tips_last_reviewed")
    suffix = f" ({reviewed} 검토)" if reviewed else ""
    lines += ["", f"> 지역·시설별 차이가 있고 규정은 바뀔 수 있습니다{suffix}. 현장 안내와 공식 최신 정보를 우선하세요."]
    return "\n".join(lines)


def _advisory_summary(alert: dict) -> tuple[int, str, bool]:
    """
    - 요약용 경보 단계를 전국(비-일부) 기준으로 계산하는 함수
      일부 지역만 여행금지인 나라를 국가 전체 '여행금지'로 오인하지 않게 한다.
    ### Returns:
      - (level, name, has_regional): 전국 레벨, 단계명, 지역별 상이 여부
    """
    regions = alert.get("regions") or []
    if not regions:
        return alert.get("level", 0), alert.get("level_name", "정보 없음"), False
    nationwide = max((r["level"] for r in regions if not r["partial"]), default=0)
    has_regional = any(r["level"] > nationwide or r["partial"] for r in regions)
    return nationwide, _LEVEL_NAME.get(nationwide, "미발령"), has_regional


def _render_alert_md(country: str, alert: dict) -> str:
    """
    - 외교부 여행경보를 '전국 기준 + 지역별 단계'로 정제 렌더링하는 함수
      단일 '국가 최고 레벨' 헤드라인은 오해를 부른다 — 남부 일부만 여행금지인 나라를
      "여행금지"로 단정하면 안전한 도시를 물은 사용자가 겁먹는다. 그래서 서버는 안전/위험을
      단정하지 않고, 전국 기준 단계 + 지역별 단계 분해 + 단계 설명(범례)만 사실로 전달한다.
    ### Args:
      - country(str): 국가 코드
      - alert(dict): _fetch_mofa_alert 결과 (regions 있으면 지역별 분해)
    ### Returns:
      - md(str): 여행경보 현황 마크다운
    """
    s = _STATIC[country]
    name = s["name_ko"]
    ok = alert.get("ok", True)
    issued = alert.get("issued_at", "-")
    tail = "> 상세 안내는 외교부 해외안전여행([0404.go.kr](https://www.0404.go.kr)) 확인."

    # 조회 실패를 🟢(안전 확인됨)으로 보이면 안 된다 — 실패는 레벨이 아니라 상태 미확인이다
    if not ok:
        note = alert.get("note") or "출발 전 0404.go.kr 재확인"
        return (f"# {name} 여행경보 현황\n\n"
                f"⚪ **경보 단계**: {alert.get('level_name', '정보 없음')} — 외교부 실시간 조회에 실패했습니다\n"
                f"**요약**: {note}\n\n{tail}")

    lines = [f"# {name} 여행경보 현황", ""]
    regions = alert.get("regions") or []

    if not regions:
        # 지역 데이터가 없으면(미발령·간이 dict) 전국 단계만 사실로
        lvl = alert.get("level", 0)
        lines.append(f"{_ALERT_ICON.get(lvl, '⚪')} **경보 단계**: {alert.get('level_name', '미발령')}")
        if alert.get("note") and alert["note"] not in ("-", ""):
            lines.append(f"**요약**: {alert['note']}")
    else:
        # 전국(비-일부) 최고 단계를 기준으로, 지역별로 다르면 목적지 확인을 유도
        nationwide = max((r["level"] for r in regions if not r["partial"]), default=0)
        base = "전국" if nationwide > 0 else "전국 일반"
        head = f"{_ALERT_ICON.get(nationwide, '🟢')} **{base}**: {_LEVEL_NAME.get(nationwide, '미발령')}"
        if any(r["level"] > nationwide or r["partial"] for r in regions):
            head += " — 지역마다 다르니 목적지를 아래에서 확인하세요"
        lines.append(head)
        lines += ["", "**지역별 단계**"]
        for lvl in (4, 3, 2, 1):
            regs = [r for r in regions if r["level"] == lvl]
            if not regs:
                continue
            texts = " · ".join(r["region"] for r in regs if r["region"])
            lines.append(f"- {_ALERT_ICON[lvl]} **{_LEVEL_NAME.get(lvl)}**{f': {texts}' if texts else ''}")
        lines += ["", "> 단계: 여행유의(신변안전 유의) < 여행자제(불필요 여행 자제) "
                  "< 출국권고(가급적 출국) < 여행금지(방문 금지)"]

    lines.append(f"**발효일**: {issued}")
    lines.append(tail)
    return "\n".join(lines)


def _exchange_line(exch: dict) -> Optional[str]:
    """
    - 환율 조회 결과를 한 줄 요약으로 만드는 함수 (체크리스트·일정추천 요약용)
      미고시 통화는 USD 기준값만 참고로 제공(현지 환전 안내는 하지 않음).
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
    return (f"{exch['currency']} 는 수출입은행 미고시 | USD 1달러 = 약 "
            f"{usd['deal_bas_r']:,.2f} 원 (해외 출금 가능 카드로 현지 ATM 출금 가능)")


def _render_exchange_md(static: dict, exch: dict) -> str:
    """
    - 환율 조회 결과를 정제 마크다운으로 렌더링하는 함수
      수출입은행 미고시 통화(VND/PHP/TWD)는 USD 기준 참고값 + ATM 안내로 대체.
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
            f"**{exch['currency']}** 는 한국수출입은행 고시 대상이 아니라 원화 직접 환산이 없습니다.\n"
            f"**USD 1달러** = 약 **{usd['deal_bas_r']:,.2f} 원** (참고용 · {exch['currency']} 환율은 아님)\n\n"
            f"> 해외 출금 가능 카드로 현지 ATM 출금이 편리합니다. 참고용."
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


def _render_briefing_md(
    country: str, static: dict, visa: dict, embassy: dict, city_key: Optional[str] = None,
) -> str:
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
    if visa.get("required"):
        visa_line = "비자 필요"
    else:
        days = visa.get("duration_days")
        visa_line = f"무비자 {days}일" if days else "무비자"
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

    # "뭐 챙겨야 해?" 질문에 팩트만이 아니라 행동으로 답하는 준비 팁 (한국=220V·C/F 기준)
    elec = ("한국 플러그 일부 호환 — 멀티 어댑터 있으면 안전"
            if {"C", "F"} & set(s["plug"]) else "한국과 다른 플러그 — 멀티 어댑터 필요")
    if not (s["voltage"].startswith("22") or s["voltage"].startswith("23")):
        elec += f", 220V 기기는 변압기 확인({s['voltage']})"
    prep_lines = [
        f"- 🔌 전기: {elec}",
        f"- 💵 현금: 출발 전 환전 또는 해외 출금 가능 카드로 현지 ATM 출금",
    ]
    if visa.get("required"):
        prep_lines.insert(0, "- 🛂 비자: 출발 전 사전 발급(도착비자·e-VISA 등) 필요")
    prep_block = "\n**🎒 챙길 것**\n" + "\n".join(prep_lines) + "\n"

    # 입국신고(TWAC·eTravel·All Indonesia 등)·결제앱은 출발 전 해야 할 일 — 브리핑에도 노출한다.
    tips = s.get("travel_tips", [])
    tips_block = ("\n**✈️ 출발 전 확인**\n" + "\n".join(f"- {t}" for t in tips) + "\n") if tips else ""

    briefing = (
        f"# {s['name_ko']}({s['name_en']}) 여행 브리핑\n\n"
        f"**🛂 비자**: {visa_line} — {note}\n"
        f"**🔌 전압/플러그**: {s['voltage']} / {plugs} 타입\n"
        f"**🕐 시차**: {tz_str}\n"
        f"**💱 통화**: {s['currency']}\n"
        f"**📞 긴급**: " + ", ".join(f"{k} {v}" for k, v in s['emergency'].items()) + "\n"
        f"**💁 팁 문화**: {s['tipping']}\n"
        f"{tips_block}"
        f"{prep_block}"
        f"{embassy_block}\n"
        f"> 참고용. 출발 전 외교부 해외안전여행(0404.go.kr) 공식 안내를 반드시 확인하세요."
    )
    local_tips = _render_local_tips_md(s, city_key=city_key)
    return f"{briefing}\n\n{local_tips}" if local_tips else briefing


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
    last_reviewed: str = "-", country: str = "JP",
) -> str:
    """
    - 도시 스팟 목록을 Google Maps 검색 링크와 상태 배지 포함해 마크다운으로 렌더링하는 함수
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
                reopen = s.get("reopen_date")
                badge = "🚧 리뉴얼/재건 중"
                if reopen:
                    badge += f" (일반 공개 예정 {reopen})"
                badges.append(badge)
            elif s.get("status") == "seasonal":
                badges.append("🗓 시즌 한정")
            badge_str = f" {' '.join(badges)}" if badges else ""
            map_link = _map_link(country, s["search_query"])
            lines.append(
                f"- **{s['name_ko']}** ({s['name_local']}){badge_str}\n"
                f"  - {s['one_liner']}\n"
                f"  - [Google 지도에서 보기]({map_link})"
            )
        lines.append("")

    lines.append(f"> 큐레이션 데이터 최종 검토일: {last_reviewed}. "
                 f"방문 전 공식 사이트에서 운영 여부를 재확인하세요.")
    return "\n".join(lines)


def _render_city_guide_md(
    country: str,
    category_hint: Optional[str] = None,
    city: Optional[str] = None,
) -> str:
    """
    - 스팟 큐레이션이 없는 도시를 city_meta 기반 가이드로 렌더링하는 함수
    ### Args:
      - country(str): 국가 코드
      - category_hint(Optional[str]): 카테고리 필터 요청 시 안내 문구용
      - city(Optional[str]): 내부 도시 키. 지정하면 해당 도시만 표시
    ### Returns:
      - md(str): 도시별 특징·추천대상·시즌 하이라이트 마크다운
    """
    static = _STATIC[country]
    cities = city_meta(country)
    if not cities:
        return (f"# {static['name_ko']} 도시 가이드\n\n"
                f"> 아직 도시 데이터가 준비되지 않았습니다.")

    selected_city = city if city in cities else None
    selected = {selected_city: cities[selected_city]} if selected_city else cities
    title = (
        f"{selected[selected_city]['name_ko']} 도시 가이드"
        if selected_city else f"{static['name_ko']} 대표 도시 가이드"
    )
    lines = [f"# {title}", ""]
    for key, m in selected.items():
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
        lines.append(f"> 카테고리 필터(`{category_hint}`)는 스팟 큐레이션이 있는 도시에서 지원합니다.")
    lines.append("> 이 도시는 아직 스팟 단위 큐레이션이 없어 도시 메타 정보로 안내했습니다. "
                 "큐레이션 도시 일정은 `recommend_itinerary` 로 확인하세요.")
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
    # 재입력 유도가 아니라 '이미 말한 도시로 상세 일정'을 바로 만들도록 호스트 LLM 에 지시.
    # (사용자가 도시를 말했는데 city 없이 호출된 경우 — 같은 도시를 다시 묻지 않게)
    first_city = picked[0][1]["name_ko"]
    lines.append(f"> 위 도시 중 하나로 상세 일정을 원하시면 이 도구를 `city` 를 채워 다시 호출하세요 "
                 f"(예: {first_city}). 사용자에게 도시를 다시 되묻지 말고, 이미 언급했다면 그 도시로 진행하세요.")
    lines.append("")
    return lines


def _pick_spots_for_purpose(
    spots: list[dict], purpose: Optional[str], want: int,
    mentions: Optional[dict[str, int]] = None,
) -> list[dict]:
    """
    - 목적에 맞는 카테고리 우선순위로 스팟을 고르는 함수
      폐업 리스크가 낮은 landmark 를 앞세우고, 카테고리를 번갈아 담아 하루가 단조롭지 않게 한다.
      블로그 언급 수(mentions)가 있으면 카테고리 내에서 요즘 많이 언급된 곳을 앞세워
      같은 도시라도 시즌마다 코스가 미묘하게 달라지게 한다 (동적성).
    ### Args:
      - spots(list[dict]): 도시의 전체 스팟
      - purpose(Optional[str]): 여행 목적. None 이면 기본 순서
      - want(int): 필요한 스팟 수
      - mentions(Optional[dict]): {name_ko: 블로그 언급 수}
    ### Returns:
      - picked(list[dict]): 고른 스팟 (want 개 이하)
    """
    plan  = _PURPOSE_PLAN.get(purpose or "", {})
    order = plan.get("categories") or ["culture", "food", "nature", "shopping", "onsen"]
    mentions = mentions or {}

    # 운영 중단된 곳은 코스에서 제외 (get_destinations 는 뱃지로 표시하지만 코스엔 넣지 않음)
    alive = [s for s in spots if s.get("status") != "under_renovation"]

    by_cat: dict[str, list[dict]] = {}
    for s in alive:
        by_cat.setdefault(s["category"], []).append(s)
    for items in by_cat.values():
        # 1순위 landmark, 2순위 블로그 언급 많은 순 (동적)
        items.sort(key=lambda x: (
            0 if x.get("stability") == "landmark" else 1,
            -mentions.get(x["name_ko"], 0),
        ))

    picked: list[dict] = []
    while len(picked) < want:
        # 한 라운드 = 카테고리마다 한 곳씩 (하루가 단조롭지 않도록)
        round_spots = [by_cat[cat].pop(0) for cat in order if by_cat.get(cat)]
        if not round_spots:                    # 스팟이 동남
            break
        # 같은 라운드 안에서는 후기 언급이 많은 곳을 앞에 둔다.
        # 뒤에서 슬롯이 모자라면 꼬리부터 잘리므로 이 순서가 곧 '배치될 확률'이다.
        # (라운드를 넘나드는 정렬은 하지 않는다 — 카테고리 다양성이 깨진다)
        round_spots.sort(key=lambda s: -mentions.get(s["name_ko"], 0))
        picked.extend(round_spots)
    return picked[:want]


def _spot_match_keys(spot: dict) -> set[str]:
    """
    - 스팟 하나를 블로그 텍스트에서 찾기 위한 매칭 키 집합을 만드는 함수
      큐레이션 이름과 블로그 표현이 달라(대사↔신사) 여러 변형으로 매칭한다.
    ### Args:
      - spot(dict): 큐레이션 스팟
    ### Returns:
      - keys(set[str]): 2자 이상 매칭 키 (전체 이름·괄호 속 이름·명시적 별칭·공백 제거형)
    """
    name = spot["name_ko"]
    base = re.sub(r"\s*\(.*?\)", "", name).strip()
    parens = re.findall(r"\(([^)]+)\)", name)
    raw_keys = {base, spot.get("search_query", ""), *parens, *spot.get("aliases", [])}
    keys = {k.strip().casefold() for k in raw_keys if k and len(k.strip()) >= 2}
    keys.update(k.replace(" ", "") for k in list(keys))
    return keys


def _count_spot_mentions(posts: list[dict], spots: list[dict]) -> dict[str, int]:
    """
    - 블로그 스니펫에서 각 큐레이션 스팟이 몇 번 언급됐는지 집계하는 함수
      검색 결과는 시즌마다 바뀌므로(가을=단풍 명소, 봄=벚꽃 명소), 같은 도시라도
      시기에 따라 다른 신호가 나온다 — 정적 큐레이션에 동적 인기도를 결합.
      (크롤링이 아니라 네이버가 제공하는 검색 요약만 분석 — 저작권 안전)
    ### Args:
      - posts(list[dict]): _fetch_naver_blog 결과 (이미 정제됨)
      - spots(list[dict]): 도시 큐레이션 스팟
    ### Returns:
      - mentions(dict): {스팟 name_ko: 언급 횟수} (0 회는 제외)
    """
    if not posts or not spots:
        return {}
    result: dict[str, int] = {}
    for s in spots:
        keys = _spot_match_keys(s)
        mentioned_posts = 0
        for post in posts:
            text = (post.get("title", "") + " " + post.get("description", "")).casefold()
            compact = re.sub(r"\s+", "", text)
            if any((key in text) or (key.replace(" ", "") in compact) for key in keys):
                mentioned_posts += 1
        if mentioned_posts:
            result[s["name_ko"]] = mentioned_posts
    return result


def _cluster_spots_by_area(spots: list[dict]) -> list[dict]:
    """같은 구역의 스팟을 연속 배치하되 구역·스팟의 기존 우선순위는 유지한다."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for spot in spots:
        # 구역이 없는 스팟끼리는 입력 순서를 유지하도록 하나의 기본 그룹으로 둔다.
        area = spot.get("area") or "__unassigned__"
        if area not in groups:
            groups[area] = []
            order.append(area)
        groups[area].append(spot)
    return [spot for area in order for spot in groups[area]]


# 도착일(Day 1)에 넣지 않을 반나절 근교의 체류시간 기준(분). 이보다 오래 걸리면 중간일로.
_DAY1_MAX_VISIT_MIN = 150

# 시간과 결부된 장소만 시간대를 표기한다(야경·노을·야시장·해돋이). 그 외는 시간 무관.
_TIME_SUNSET = ("야경", "노을", "일몰", "석양")
_TIME_NIGHT = ("야시장", "포장마차", "야타이")
_TIME_MORNING = ("해돋이", "일출")


def _time_signal(spot: dict) -> tuple[int, str]:
    """
    - 장소가 시간과 결부됐는지 판정하는 함수 (해질녘·저녁·이른 아침만 표기)
      오전/오후 같은 임의 슬롯 대신, 야경·야시장처럼 시간이 실제로 중요한 곳만 안내한다.
    ### Args:
      - spot(dict): 큐레이션 스팟
    ### Returns:
      - (rank, note): rank 는 하루 내 정렬용(0 아침·1 무관·3 저녁), note 는 표기 문구(없으면 '')
    """
    pref = spot.get("preferred_times") or []
    if isinstance(pref, str):
        pref = [pref]
    text = f"{spot.get('one_liner', '')} {spot.get('name_ko', '')}"
    if any(k in text for k in _TIME_SUNSET):
        return 3, "해질녘 풍경 추천"
    if any(k in text for k in _TIME_NIGHT) or ("evening" in pref and "lunch" not in pref):
        return 3, "저녁 추천"
    if any(k in text for k in _TIME_MORNING) or ("morning" in pref and "lunch" not in pref):
        return 0, "이른 아침 추천"
    return 1, ""


def _pop_sight_for_area(
    sights: list[dict], area: Optional[str], max_visit: Optional[int] = None,
) -> Optional[dict]:
    """
    - 같은 구역이 있으면 먼저 골라 하루 동선을 뭉치는 함수(없으면 남은 우선순위 순)
      max_visit 을 주면 그보다 오래 걸리는 반나절 근교(단수이·베이터우 등)는 건너뛴다.
      도착일(Day 1)은 체크인·비행 여파로 먼 반나절 일정을 넣지 않기 위한 장치.
    ### Args:
      - sights(list[dict]): 남은 관광 스팟 (area 순 정렬됨)
      - area(Optional[str]): 직전 방문 구역. 있으면 같은 구역 우선
      - max_visit(Optional[int]): 체류시간 상한(분). 초과 스팟은 건너뜀
    ### Returns:
      - spot(Optional[dict]): 고른 스팟(목록에서 제거). 후보 없으면 None
    """
    if not sights:
        return None

    def _visit(spot: dict) -> int:
        return int(spot.get("visit_min")
                   or _PLAN_TIMING.get("dwell_min", {}).get(spot.get("category"), 0))

    order = sorted(
        range(len(sights)),
        key=lambda idx: 0 if area and sights[idx].get("area") == area else 1,
    )
    for idx in order:
        if max_visit is not None and _visit(sights[idx]) > max_visit:
            continue
        return sights.pop(idx)
    return None


def _pop_market(markets: list[dict], area: Optional[str]) -> Optional[dict]:
    """먹거리 시장·거리를 같은 구역 우선으로 하나 고른다(하루의 저녁 활동 후보)."""
    if not markets:
        return None
    if area:
        for idx, m in enumerate(markets):
            if m.get("area") == area:
                return markets.pop(idx)
    return markets.pop(0)


def _menu_name(city_key: Optional[str], city_ko: str, meal_index: int) -> str:
    """도시 대표 음식을 끼니마다 하나씩 순환해 메뉴명만 반환한다(시간 비종속)."""
    raw = _CITY_FOOD.get(city_key or "", "")
    choices = [v.strip() for v in re.split(r"[·,]", raw) if v.strip()]
    return choices[meal_index % len(choices)] if choices else f"{city_ko} 지역 대표 음식"


def _menu_from_food_hint(food_hint: str) -> str:
    """'굴전·루러우판 — 1인 약 120~300 TWD' → '굴전·루러우판' (메뉴명만)."""
    return food_hint.split(" — ")[0].strip()


def _meal_price_note(country: str, city_key: Optional[str]) -> str:
    """한 끼 1인 참고 가격대. 정적 식비(3끼 총액)를 넓은 범위로만 환산한다."""
    daily_food = _CITY_DAILY_COST.get(city_key or "", {}).get("food")
    if not daily_food:
        return ""
    midpoint = daily_food / 3
    unit = 1000 if midpoint >= 10000 else (100 if midpoint >= 1000 else 10)
    low = max(unit, round(midpoint * 0.65 / unit) * unit)
    high = max(low + unit, round(midpoint * 1.35 / unit) * unit)
    currency = _STATIC.get(country, {}).get("currency", "")
    return f"한 끼 1인 약 {low:,.0f}~{high:,.0f} {currency}"


def _render_day_plan_md(
    city_ko: str, spots: list[dict], purpose: Optional[str], nights: int,
    mentions: Optional[dict[str, int]] = None, country: str = "JP",
    city_key: Optional[str] = None,
) -> list[str]:
    """
    - 큐레이션 스팟을 Day 별 '할 것(활동) + 추천 식사'로 배치한 일자별 코스를 만드는 함수
      오전·오후·저녁 슬롯을 라인마다 박으면 '언제'가 '무엇'을 가린다. 시간이 실제로
      중요한 곳(야경·야시장·해돋이)만 표기하고, 나머지는 방문 순서만 준다. 식사는
      시간대에 묶지 않고 Day 별 '추천 식사'로 모아 사용자가 끼니를 직접 고르게 한다.
      (개별 식당은 큐레이션하지 않는다 — 폐업 리스크. 시장·먹거리 거리 단위까지만)
    ### Args:
      - city_ko(str): 도시 한글명
      - spots(list[dict]): 도시의 전체 스팟 (destinations JSON)
      - purpose(Optional[str]): 여행 목적
      - nights(int): 숙박 수
      - mentions(Optional[dict]): 블로그 언급 수 (코스 순서 동적 반영)
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (스팟 없으면 빈 리스트)
    """
    if not spots:
        return []

    if not city_key:
        city_key = next(
            (key for key, meta in city_meta(country).items() if meta.get("name_ko") == city_ko),
            None,
        )

    plan  = _PURPOSE_PLAN.get(purpose or "", {})
    days  = nights + 1
    per_day = plan.get("sights_per_day", 2)
    # 첫날 2곳(도착일도 할 게 있어야 함)·마지막날 1곳·중간일 목적별 밀도만큼.
    sight_budget = 2 + (1 if days >= 2 else 0) + per_day * max(days - 2, 0)
    want = sight_budget + 4  # 종일 일정 후보와 소진 여유분

    # 먹거리(시장·먹거리 거리)는 방문지이자 식사처 — 활동으로 배치하고 메뉴는 추천 식사로 뽑는다
    alive  = [s for s in spots if s.get("status") != "under_renovation"]
    markets = [s for s in alive if s["category"] == "food" and s.get("meal_slot", True)]
    picked = _pick_spots_for_purpose(
        [s for s in alive if s["category"] != "food"], purpose, want, mentions,
    )
    day_trips = [s for s in picked if s.get("trip_scope") == "full_day"]
    sights = _cluster_spots_by_area(
        [s for s in picked if s.get("trip_scope") != "full_day"]
    )
    if not sights and not markets and not day_trips:
        return []

    # 자동 당일치기는 3박 이상일 때 최대 1회만, 첫날·귀국일이 아닌 Day 2에 배치한다.
    day_trip_day = 1 if days >= 4 and day_trips else None
    day_trip = day_trips[0] if day_trip_day is not None else None

    # 목적×카테고리별 '왜 이 스팟인가' — 지시문은 압축 시 잘리므로 각 라인에 데이터로 심는다
    reasons = _PURPOSE_REASON.get(purpose or "", {})
    mentions = mentions or {}
    price_note = _meal_price_note(country, city_key)

    def _extras(spot: dict) -> list[str]:
        # 부연(추천 이유·후기)은 본문 뒤 ' | ' 로 뺀다 — 제목·설명 가독성 우선.
        out = []
        r = reasons.get(spot.get("category", ""))
        if r:
            out.append(r)
        n = mentions.get(spot["name_ko"], 0)
        if n >= 2:
            out.append(f"후기 {n}건")
        return out

    def _activity(spot: dict, prev: Optional[dict], lead: str = "") -> str:
        # 형식: - [이름](링크): 설명 (타이밍·이동) | 추천 이유, 후기
        #   이동은 화살표(→), 절 연결은 쉼표(,), 부연은 괄호, 괄호가 차면 파이프(|).
        link = _map_link(country, spot["search_query"])
        _, tnote = _time_signal(spot)
        meta = [m for m in (lead, tnote, _dwell_hint(spot), _transit_hint(prev, spot)) if m]
        meta_str = f" ({', '.join(meta)})" if meta else ""
        extras = _extras(spot)
        extras_str = f" | {', '.join(extras)}" if extras else ""
        return f"- [{spot['name_ko']}]({link}): {spot['one_liner']}{meta_str}{extras_str}"

    meal_index = [0]

    def _meal_line(count: int, explicit: Optional[str] = None) -> str:
        # 시장 먹거리는 활동으로 이미 나가므로, 식사는 도시 대표 음식만 끼니별로 순환한다.
        menus: list[str] = [explicit] if explicit else []
        while len(menus) < count:
            m = _menu_name(city_key, city_ko, meal_index[0])
            meal_index[0] += 1
            if m not in menus:
                menus.append(m)
        suffix = f" ({price_note})" if price_note else ""
        return f"- 추천 식사: {' / '.join(menus)}{suffix}"

    purpose_ko = _PURPOSE_KO.get(purpose, "") if purpose else ""
    head = f"## 🗓 {nights}박{days}일 코스 제안 ({city_ko}"
    head += f" · {purpose_ko})" if purpose_ko else ")"
    lines = [head]
    note = plan.get("note", "")
    design = f"{purpose_ko} 기준 — {note}" if purpose_ko and note else (note or "")
    if design:
        lines.append(f"**코스 설계**: {design}. 도착일·귀국일은 비행을 감안해 가볍게 배치했어요.")

    for day in range(days):
        first, last = day == 0, day == days - 1
        label = "도착 · 시내 적응" if first else ("여유롭게 마무리 · 귀국" if last else "핵심 관광")
        lines.append(f"**Day {day + 1}** ({label})")

        if day == day_trip_day and day_trip:
            lines.append(_activity(day_trip, None, lead="하루 종일"))
            explicit = _menu_from_food_hint(day_trip["food_hint"]) if day_trip.get("food_hint") else None
            lines.append(_meal_line(1, explicit))
            lines.append("")
            continue

        n_sights = 2 if first else (1 if last else per_day)
        n_meals = 1 if last else 2
        # 도착일은 체크인·비행 여파로 먼 반나절 근교를 넣지 않는다(가까운 곳부터).
        max_visit = _DAY1_MAX_VISIT_MIN if first else None

        # 하루치 활동 수집: 관광 스팟 + (마지막날 제외) 먹거리 시장 1곳
        day_spots: list[dict] = []
        prev_area: Optional[str] = None
        for _ in range(n_sights):
            spot = _pop_sight_for_area(sights, prev_area, max_visit)
            if spot is None:
                break
            day_spots.append(spot)
            prev_area = spot.get("area")
        if not last:
            market = _pop_market(markets, prev_area)
            if market:
                day_spots.append(market)

        # 시간과 결부된 곳(야시장 등)은 하루의 끝, 아침시장은 앞으로 (그 외 순서 유지)
        day_spots.sort(key=lambda s: _time_signal(s)[0])

        if not day_spots:
            lines.append(
                f"- {city_ko} 여유 일정 — 카페 휴식 후 주변 상권 산책·기념품 쇼핑 (약 1시간 30분)"
            )
        prev_spot: Optional[dict] = None
        for spot in day_spots:
            lines.append(_activity(spot, prev_spot))
            prev_spot = spot
        lines.append(_meal_line(n_meals))
        lines.append("")

    lines.append("> 체류 시간은 여유 있게 잡은 추정치입니다. 이동 경로·소요 시간은 "
                 "구글맵 경로검색으로 확인하세요.")
    lines.append("")
    return lines


_ALERT_ICON = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴", 4: "⛔"}

_COST_LABEL = {
    "transport": "교통", "food": "식비(3끼)", "cafe_snack": "카페·간식",
    "admission": "입장료·체험", "contingency": "예비비",
}


def _krw_per_unit(exch: Optional[dict]) -> Optional[float]:
    """
    - 현지 통화 1단위당 원화를 계산하는 함수
      수출입은행은 통화에 따라 100단위로 고시한다(JPY(100), IDR(100)) — 그대로 곱하면
      비용이 100배로 부풀어 오르므로 고시 단위를 반드시 나눠야 한다.
    ### Args:
      - exch(Optional[dict]): get_exchange_for_country 결과
    ### Returns:
      - krw(Optional[float]): 현지 통화 1단위당 원화. 고시가 없으면 None
    """
    if not exch or not exch.get("quoted"):
        return None
    rate = exch.get("rate")
    if not rate or not rate.get("deal_bas_r"):
        return None
    m = re.search(r"\((\d+)\)", rate.get("currency", ""))
    divisor = int(m.group(1)) if m else 1
    return float(rate["deal_bas_r"]) / divisor


def _render_cost_md(
    country: str, city: Optional[str], exch: Optional[dict], nights: int,
    purpose: Optional[str] = None, num_people: Optional[int] = None,
) -> list[str]:
    """
    - 1일 예상 경비와 여행 전체 합계를 마크다운으로 만드는 함수
      호스트 LLM 이 비용을 지어내는 것(근거 없는 '1인 70만원')을 막으려면 서버가 확정 숫자를
      줘야 한다. 항공·숙박은 변동이 커서 제외하고, 현지에서 실제로 쓰는 돈만 다룬다.
      원화 환산은 그날 매매기준율로 계산하며, 미고시 통화는 현지 통화로만 표기한다.
    ### Args:
      - country(str): 국가 코드
      - city(Optional[str]): 도시 키
      - exch(Optional[dict]): get_exchange_for_country 결과
      - nights(int): 숙박 수 (여행 일수 = nights + 1)
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (데이터 없으면 빈 리스트)
    """
    cost = _CITY_DAILY_COST.get(city or "")
    if not cost:
        return []

    keys = ("transport", "food", "cafe_snack", "admission", "contingency")
    items = [(k, cost[k]) for k in keys if cost.get(k)]
    if not items:
        return []

    daily_local = sum(v for _, v in items)
    days = nights + 1
    assumed_couple = purpose == "couple" and not num_people
    people = num_people or (2 if assumed_couple else 1)
    currency = _STATIC.get(country, {}).get("currency", "")
    krw = _krw_per_unit(exch)

    def money(local: float) -> str:
        base = f"{local:,.0f} {currency}"
        return f"{base} (약 {round(local * krw, -3):,.0f}원)" if krw else base

    def local_money(local: float) -> str:
        """상단 요약용 금액. 한 문장 안의 전제가 원화 괄호 때문에 분리되지 않게 한다."""
        return f"{local:,.0f} {currency}"

    breakdown = " · ".join(f"{_COST_LABEL[k]} {v:,.0f}" for k, v in items)
    basis_labels = {
        "transport": "시내 교통", "food": "식사 3끼", "cafe_snack": "카페·간식",
        "admission": "일정상 입장료", "contingency": "예비비",
    }
    included = ", ".join(basis_labels[k] for k, _ in items)
    economy = daily_local * 0.75
    comfortable = daily_local * 1.4
    people_label = "커플 2인" if assumed_couple else f"{people}인"
    lines = [
        "## 💰 예상 현지 경비 — 항공·숙박·쇼핑 제외",
        f"- 🔒 **필수 예산 요약**: {people_label} (일반형 "
        f"{local_money(daily_local * people)}/일, {days}일 "
        f"{local_money(daily_local * days * people)}, 항공·숙박·쇼핑 제외)",
        f"- 📌 **산정 기준**: 일반형 · 1인 하루 · {included} 포함",
        f"- 🧮 **1인 하루**: 절약형 {money(economy)} · 일반형 **{money(daily_local)}** · 여유형 {money(comfortable)}",
        f"- 📊 **일반형 내역**({currency}, 1인/일): {breakdown}",
        f"- 👥 **{people}인 하루 일반형**: {money(daily_local * people)}",
        f"- 🗓 **{days}일 {people}인 현지 체류비**: {money(daily_local * days * people)}",
    ]
    if assumed_couple:
        lines.append("- ℹ️ 커플여행이므로 인원 미입력 시 2명으로 계산했습니다")
    if not krw:
        lines.append(f"- 💱 {currency} 는 수출입은행 미고시라 원화 환산이 없습니다 (해외 출금 가능 카드로 현지 ATM 출금 가능)")
    lines.append("> 전망대·근교 이동·야간 활동을 추가하면 증가합니다. 항공·숙박은 날짜별 변동이 커 별도 계산해야 합니다.")
    lines.append("")
    return lines


def _trip_period_label(
    depart: str, ret: str, nights_str: str, basis: str, depart_month: int,
) -> str:
    """
    - 여행 기간을 '근거 있는 만큼만' 표기하는 함수
      날짜를 추정했는데 구체적 날짜를 노출하면 호스트 LLM 이 확정 일정으로 전달하고,
      그 날짜에 맞지 않는 행사·시즌 정보를 붙이는 문제가 있다 (QA v2 P0-3).
    ### Args:
      - depart(str): 출국일 'YYYY-MM-DD'
      - ret(str): 귀국일 'YYYY-MM-DD'
      - nights_str(str): '2박3일' 형태 표기
      - basis(str): 'exact'(날짜 입력) | 'month'(월만 입력) | 'none'(단서 없음)
      - depart_month(int): 출발 월
    ### Returns:
      - label(str): 기간 표기 문자열
    """
    if basis == "exact":
        return f"{depart} ~ {ret} ({nights_str})"
    if basis == "month":
        return f"{depart_month}월 여행 기준 · {nights_str} (출발일 미정)"
    return f"출발일 미정 · {nights_str} 기준"


_EMERGENCY_KO = {
    "police": "경찰", "ambulance": "구급", "fire": "소방", "tourist_police": "관광경찰",
}


def _render_practical_lines(country: Optional[str]) -> list[str]:
    """
    - 전압·시차·긴급번호를 한 줄씩 압축해 붙이는 함수
      "준비사항도 알려줘" 요청에 일정 툴 하나로 답하려면 필요하지만, 응답이 길어질수록
      호스트 LLM 이 더 공격적으로 압축한다 → 항목당 한 줄, 데이터 라인 형태로만 넣는다.
    ### Args:
      - country(Optional[str]): 국가 코드. None 이면 빈 리스트
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록
    """
    static = _STATIC.get(country or "", {})
    if not static:
        return []

    lines: list[str] = []

    voltage, plug = static.get("voltage"), static.get("plug") or []
    if voltage:
        plug_txt = f" · {'/'.join(plug)}타입 플러그" if plug else ""
        need = " (한국 220V 기기는 변압기 확인)" if voltage.startswith("1") else " (한국과 동일해 그대로 사용)"
        lines.append(f"- 🔌 **전압**: {voltage}{plug_txt}{need}")

    tz = static.get("tz_offset_h")
    if tz is not None:
        tz_txt = "한국과 시차 없음" if tz == 0 else f"한국보다 {abs(tz)}시간 {'느림' if tz < 0 else '빠름'}"
        lines.append(f"- 🕘 **시차**: {tz_txt}")

    emergency = static.get("emergency") or {}
    if emergency:
        nums = " · ".join(f"{_EMERGENCY_KO.get(k, k)} {v}" for k, v in emergency.items())
        lines.append(f"- 🚨 **긴급번호**: {nums} · 영사콜센터 +82-2-3210-0404")

    return lines


def _render_essentials_md(
    exch: Optional[dict], visa: Optional[dict],
    alert: Optional[dict], season: Optional[dict],
    country: Optional[str] = None,
) -> list[str]:
    """
    - 출발 전 필수 정보(비자·안전·환율·시즌)를 한 줄씩 요약하는 함수
      "일정 짜줘" 한 번에 여행 준비 전체가 보이도록 일정 추천 상단에 얹는다.
      상세는 각 전용 툴(get_trip_briefing 등)이 담당 — 여기선 요약만.
    ### Args:
      - exch(Optional[dict]): get_exchange_for_country 결과
      - visa(Optional[dict]): 비자 정보 (required, duration_days, note)
      - alert(Optional[dict]): 여행경보 (level, level_name)
      - season(Optional[dict]): 시즌 판정 (season, note)
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (전부 없으면 빈 리스트)
    """
    items: list[str] = []

    if visa:
        if visa.get("required"):
            items.append(f"- 🛂 **비자**: 사전 발급 필요 — {visa.get('note', '')}".rstrip(" —"))
        else:
            days = visa.get("duration_days")
            items.append(f"- 🛂 **비자**: 무비자{f' {days}일' if days else ''}")

    if alert:
        ok = alert.get("ok", True)
        lvl, name, has_regional = _advisory_summary(alert)
        icon = _ALERT_ICON.get(lvl, "⚪") if ok else "⚪"
        line = f"- {icon} **안전**: {name}"
        if not ok:
            line += " — 외교부 해외안전여행(0404.go.kr)에서 직접 확인하세요"
        else:
            if has_regional:
                line += " (지역별 상이 — 목적지 확인)"
            if lvl >= 2:
                line += " — 방문 전 외교부(0404.go.kr) 확인 권장"
        items.append(line)

    if exch:
        exch_line = _exchange_line(exch)
        if exch_line:
            items.append(f"- 💱 **환율**: {exch_line}")

    if season:
        tip = {"peak": "성수기 — 2~3개월 전 예약 권장",
               "shoulder": "가성비 시즌 — 1~2개월 전 예약",
               "off": "비수기 — 임박 예약도 가격 안정적"}.get(season.get("season"), "")
        note = season.get("note", "")
        # 시즌 규칙 미매칭 폴백 문구는 팁으로 대체 (중복 방지)
        detail = note if "표시 없음" not in note else tip
        if note and "표시 없음" not in note and tip:
            detail = f"{note} ({tip})"
        items.append(f"- 🗓 **시즌**: {detail}")

    # 전압·시차·긴급번호 — 준비사항 질문에 일정 툴 하나로 답하기 위한 최소 실용 정보
    items.extend(_render_practical_lines(country))

    if not items:
        return []
    return ["## ✈️ 출발 전 필수 (요약)", *items, ""]


def _render_country_traits_md(
    country: str, city: Optional[str], depart_month: int, basis: str = "exact",
) -> list[str]:
    """
    - 국가·도시 특징(여행 팁 + 시즌 하이라이트)을 마크다운 라인 목록으로 만드는 함수
      일정 추천에 국가별 색깔(중국=결제앱/VPN, 태국=복장, 인니=비자 등)을 넣기 위함.
    ### Args:
      - country(str): 국가 코드
      - city(Optional[str]): 도시 키. 지정 시 해당 도시의 시즌 하이라이트 포함
      - depart_month(int): 출발 월 (시즌 하이라이트 선택용)
      - basis(str): 날짜 근거. 'exact' 가 아니면 특정일 행사를 확정 추천하지 않는다
    ### Returns:
      - lines(list[str]): 마크다운 라인 목록 (없으면 빈 리스트)
    """
    static = _STATIC.get(country, {})
    lines: list[str] = []

    # 도시 시즌 하이라이트 — 출발 월의 계절에 맞는 것만.
    # season_highlight 에는 '스미다가와 불꽃축제'처럼 날짜가 정해진 행사가 섞여 있다.
    # 출발일이 미정인데 특정일 행사를 추천하면 여행 기간과 어긋난다 (QA v2 P0-3) →
    # 날짜 근거가 없을 땐 확정 추천이 아니라 '확인해 보세요' 로 표현을 낮춘다.
    season_key = _QUERY_VOCAB.get("month_to_season", {}).get(depart_month)
    cities = city_meta(country)
    if city and city in cities and season_key:
        highlight = cities[city].get("season_highlight", {}).get(season_key)
        if highlight:
            season_ko = _SEASON_KO.get(season_key, "")
            if basis == "exact":
                lines.append(f"- 🗓 **{season_ko} 하이라이트**: {highlight}")
            else:
                lines.append(f"- 🗓 **{season_ko} 하이라이트**: {highlight} "
                             f"— 날짜가 정해진 행사는 출국일 확정 후 개최 기간을 확인하세요")

    tips = static.get("travel_tips", [])
    for t in tips:
        lines.append(f"- ℹ️ {t}")

    if lines:
        return [f"## 🧭 {static.get('name_ko', country)} 여행 특징", *lines, ""]
    return []


def _render_season_advice_md(
    country: str, depart_month: int, basis: str,
) -> list[str]:
    """사용자가 월이나 날짜를 준 경우 정적 월별 데이터로 여행 적합성과 운영법을 답한다."""
    if basis not in ("month", "exact"):
        return []
    advice = _STATIC.get(country, {}).get("monthly_advice", {}).get(str(depart_month))
    if not advice:
        return []
    return [
        f"## 🌦 {depart_month}월 여행 판단",
        f"- **결론**: {advice['verdict']}",
        f"- **일정 운영**: {advice['schedule']}",
        f"- **날씨 대안**: {advice['backup']}",
        "",
    ]


def _prep_summary_line(
    visa: Optional[dict], alert: Optional[dict], country: Optional[str],
) -> Optional[str]:
    """비자·안전·긴급번호를 한 줄로 묶은 원자 준비 요약(압축 생존용).

    '출발 전 필수' 블록은 호스트 LLM 이 통째로 버리기도 해서(단일 출처인 근교 도시일수록),
    가장 잘 남는 상단 핵심 요약에 준비 정보를 한 줄로도 심어 둔다.
    """
    parts: list[str] = []
    if visa:
        if visa.get("required"):
            parts.append("비자 사전 발급 필요")
        else:
            days = visa.get("duration_days")
            parts.append(f"무비자{f' {days}일' if days else ''}")
    if alert:
        ok = alert.get("ok", True)
        if ok:
            _, name, _ = _advisory_summary(alert)
            parts.append(f"안전 {name}")
        else:
            parts.append("안전 확인 필요")
    emerg = _STATIC.get(country or "", {}).get("emergency", {})
    nums = "·".join(
        x for x in (
            f"경찰 {emerg.get('police')}" if emerg.get("police") else "",
            f"구급 {emerg.get('ambulance')}" if emerg.get("ambulance") else "",
        ) if x
    )
    if nums:
        parts.append(f"긴급 {nums}")
    if emerg:
        parts.append("영사콜센터 +82-2-3210-0404")
    return " | ".join(parts) if parts else None


def _render_priority_summary_md(
    season_lines: list[str], day_plan_lines: list[str], cost_lines: list[str],
    prep: Optional[str] = None,
) -> list[str]:
    """작은 호스트 LLM이 앞부분만 사용해도 판단·준비·예산·일정·식사가 함께 남는 요약."""
    if not day_plan_lines and not cost_lines and not season_lines:
        return []

    lines = ["## ✅ 먼저 보는 핵심 요약"]

    conclusion = next(
        (line.removeprefix("- **결론**: ") for line in season_lines
         if line.startswith("- **결론**: ")),
        None,
    )
    if conclusion:
        lines.append(f"- **여행 판단**: {conclusion}")

    if prep:
        lines.append(f"- **준비**: {prep}")

    cost_summary = next(
        (line.split(": ", 1)[1] for line in cost_lines
         if line.startswith("- 🔒 **필수 예산 요약**: ")),
        None,
    )
    if cost_summary:
        lines.append(f"- **예산**: {cost_summary}")

    days: list[dict[str, object]] = []
    current: Optional[dict[str, object]] = None
    for line in day_plan_lines:
        day_match = re.match(r"\*\*Day (\d+)", line)
        if day_match:
            current = {"number": day_match.group(1), "spots": [], "meals": [], "price": ""}
            days.append(current)
            continue
        if not current or not line.startswith("- "):
            continue

        if line.startswith("- 추천 식사:"):
            body = line.split("추천 식사:", 1)[1]
            price = re.search(r"\(([^)]*1인[^)]*)\)", body)
            if price:
                current["price"] = price.group(1)
                body = body[: body.rfind("(")]
            for m in body.split("/"):
                m = m.strip()
                if m and m not in current["meals"]:
                    current["meals"].append(m)
            continue

        linked = re.search(r"\[([^\]]+)\]\(", line)  # 활동만 집계, 여유 일정 줄은 링크가 없어 제외
        if linked and linked.group(1) not in current["spots"]:
            current["spots"].append(linked.group(1))

    for day in days:
        spots = day["spots"][:3]
        meals = day["meals"][:2]
        if not spots and not meals:
            continue
        lines.append(f"- **Day {day['number']}**:")
        if spots:
            lines.append("  - 경로: " + " → ".join(spots))
        if meals:
            meal_line = "  - 추천 식사: " + " / ".join(meals)
            if day["price"]:
                meal_line += f" ({day['price']})"
            lines.append(meal_line)

    lines.append("")
    return lines


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
    visa: Optional[dict] = None,
    alert: Optional[dict] = None,
    season: Optional[dict] = None,
    basis: str = "exact",
) -> str:
    """
    - 출발 전 필수 요약 + 일자별 코스 + 여행 방향 + 국가 특징 + 후기를 종합하는 함수
      "일정 짜줘" 한 번에 여행 준비 전체가 나오도록 비자·안전·환율·시즌을 상단에 얹는다.
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

    # 사용자가 날짜를 말하지 않았는데 구체적 날짜를 보여주면 호스트 LLM 이 확정 일정으로 옮겨 적는다.
    # (v2 QA: 8월 추정 일정에 7월 행사를 붙이는 모순이 실제로 발생) → 근거만큼만 표기한다.
    cond = [f"**일정**: {_trip_period_label(depart, ret, nights_str, basis, depart_month)}"]
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
        lines.append("> 📅 출발일이 정해지지 않아 아래 일정은 날짜가 아닌 순서 기준입니다. "
                     "정확한 출국일을 알려주시면 시즌·항공·행사 정보를 맞춰 드려요.")
        lines.append("")

    # 블로그 스니펫에서 큐레이션 스팟 언급을 집계 (동적 인기 신호)
    mentions = _count_spot_mentions(posts, spots or [])

    # 카카오처럼 Tool Response 앞부분을 중심으로 강하게 축약하는 호스트를 위해
    # 상세 섹션을 먼저 만든 뒤 판단·예산·Day별 장소와 식사를 상단 한 블록에 모은다.
    season_lines = _render_season_advice_md(country, depart_month, basis)
    day_plan_lines: list[str] = []
    cost_lines: list[str] = []
    if city and spots:
        city_ko = cities.get(city, {}).get("name_ko", city)
        day_plan_lines = _render_day_plan_md(
            city_ko, spots, purpose, nights, mentions, country, city,
        )
        cost_lines = _render_cost_md(
            country, city, exch, nights, purpose, num_people,
        )

    prep = _prep_summary_line(visa, alert, country)
    lines.extend(_render_priority_summary_md(season_lines, day_plan_lines, cost_lines, prep))

    # 출발 전 필수 요약 — 비자·안전·환율·시즌 (여행 질문 하나로 준비 전체가 보이도록)
    lines.extend(_render_essentials_md(exch, visa, alert, season, country))
    lines.extend(season_lines)

    # 도시 미지정 = "어디 가면 좋아?" 가 질문의 핵심 → 목적에 맞는 도시부터 제안
    has_day_plan = bool(city and spots)
    if not city:
        lines.extend(_render_city_picks_md(country, purpose, depart_month))
    # 도시가 정해졌고 큐레이션 스팟이 있으면 → 일자별 코스 뼈대를 재료로 제공
    elif spots:
        lines.extend(day_plan_lines)
        lines.extend(cost_lines)

    # 서로 다른 후기에 반복 등장한 큐레이션 명소 = 블로그에서 추출한 동적 인기 신호.
    # LLM 이 파편 스니펫을 종합하는 대신, 서버가 집계한 '몇 건의 후기가 언급했나'를 준다.
    hot = [(name, c) for name, c in sorted(mentions.items(), key=lambda x: -x[1]) if c >= 2]
    if hot:
        lines.append("## 🔥 최근 후기가 꼽은 인기 명소 (동적)")
        lines.append("최근 검색된 실제 여행 후기들이 공통으로 언급한 곳입니다 (검색 시점마다 갱신).")
        for name, c in hot[:6]:
            lines.append(f"- **{name}** — 후기 {c}건 언급")
        lines.append("> 방문객 순위가 아니라, 최근 검색된 후기들이 반복해서 언급한 신호입니다.")
        lines.append("")

    # 목적별 여행 방향 — 같은 목적이라도 세부 방향(데이트/휴식/취미 등)에 따라
    # 갈 곳이 달라지므로 방향을 함께 제시해 호스트 LLM 이 좁혀가게 함.
    # 단 일자별 코스가 이미 나갔다면 그 코스 자체가 방향의 구현물이라 중복이다.
    # 호스트 LLM 출력 예산이 한정적이라(실측 4,000자 → 600자) 코스를 살리려면 여기서 줄여야 한다.
    if has_day_plan:
        pass
    elif purpose and purpose in _PURPOSE_ANGLE:
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
    lines.extend(_render_country_traits_md(country, city, depart_month, basis))

    # 핵심 현지 팁 요약 — '준비물+일정'을 일정 툴 하나로 물어도 현지 팁이 빠지지 않게
    lines.extend(_render_local_tips_summary(_STATIC.get(country, {}), city_key=city))

    if not posts:
        lines.append("> 블로그 검색 결과가 없습니다. 네이버 API 키를 확인하거나 잠시 후 다시 시도해주세요.")
        return "\n".join(lines)

    lines.append("## 📝 실제 여행자 후기")
    lines.append("")
    # 집계는 전체(위 mentions)를 쓰되 노출은 최소로.
    # 코스가 이미 나간 경우 후기는 출처 역할만 하면 되므로 2건·한 줄 메타로 줄인다
    # (5건 = 전체의 24%를 차지했고, 압축 과정에서 코스 설명을 밀어냈다).
    show, snippet_len = (2, 0) if has_day_plan else (5, 110)
    for i, p in enumerate(posts[:show], 1):
        postdate = p.get("postdate", "")
        if len(postdate) == 8 and postdate.isdigit():
            postdate = f"{postdate[:4]}-{postdate[4:6]}-{postdate[6:]}"
        meta = " · ".join(v for v in (p.get("bloggername", ""), postdate) if v)
        if has_day_plan:
            suffix = f" · {meta}" if meta else ""
            lines.append(f"- [{p['title']}]({p['link']}){suffix}")
        else:
            desc = p["description"]
            desc = desc[:snippet_len] + "…" if len(desc) > snippet_len else desc
            lines.append(f"**{i}. [{p['title']}]({p['link']})**")
            if meta:
                lines.append(f"- {meta}")
            lines.append(f"- {desc}" if desc else "- (본문 미리보기 없음)")
            lines.append("")

    lines.append(f"> 검색 쿼리: `{query}`")
    lines.append("> 블로그 내용은 작성자 개인 경험 기준이며 실제와 다를 수 있습니다.")
    return "\n".join(lines)
