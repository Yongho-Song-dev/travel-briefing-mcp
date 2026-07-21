"""큐레이션·검색·일정 생성 회귀 테스트."""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import Mock, patch

import tb_api
from tb_api import _fetch_naver_blog
from tb_config import (
    CURATED_COUNTRIES, _CITY_DAILY_COST, _NAVER_BLOG_DISPLAY, _STATIC, city_meta,
)
from tb_helpers import (
    _count_spot_mentions,
    _krw_per_unit,
    _pick_spots_for_purpose,
    _map_link,
    _render_alert_md,
    _render_city_guide_md,
    _render_cost_md,
    _render_country_traits_md,
    _render_day_plan_md,
    _render_destinations_md,
    _render_essentials_md,
    _render_itinerary_md,
    _render_season_advice_md,
    _trip_period_label,
    resolve_city_key,
    resolve_trip_dates,
)
from tb_scheduler import _warm_naver_blog_once


ROOT = Path(__file__).parent


class CurationLogicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (ROOT / "curation" / "destinations_jp.json").open(encoding="utf-8") as f:
            cls.data = json.load(f)

    def spots(self, city: str) -> list[dict]:
        return self.data["cities"][city]["spots"]

    def test_mentions_are_counted_once_per_result(self) -> None:
        posts = [
            {
                "title": "후시미 이나리 대사 후기",
                "description": "후시미 이나리 대사와 후시미이나리대사를 다시 방문",
            },
            {"title": "후시미이나리 산책", "description": "교토 추천 명소"},
        ]
        mentions = _count_spot_mentions(posts, self.spots("kyoto"))
        self.assertEqual(mentions["후시미 이나리 대사"], 2)

    def test_generic_city_name_does_not_match_a_specific_spot(self) -> None:
        posts = [{"title": "도쿄 여행 후기", "description": "도쿄에서 보낸 사흘"}]
        mentions = _count_spot_mentions(posts, self.spots("tokyo"))
        self.assertNotIn("도쿄 국립박물관 (우에노)", mentions)

    def test_city_names_resolve_to_internal_key(self) -> None:
        for value in ("kyoto", "교토", "京都"):
            with self.subTest(value=value):
                self.assertEqual(resolve_city_key("JP", value), "kyoto")
        self.assertIsNone(resolve_city_key("JP", "없는 도시"))

    def test_non_japan_city_guide_honors_selected_city(self) -> None:
        cities = city_meta("TW")
        keys = list(cities)
        self.assertGreaterEqual(len(keys), 2)
        md = _render_city_guide_md("TW", city=keys[0])
        self.assertIn(cities[keys[0]]["name_ko"], md)
        self.assertNotIn(cities[keys[1]]["name_ko"], md)

    def test_full_day_trip_uses_only_a_middle_day(self) -> None:
        md = "\n".join(_render_day_plan_md("도쿄", self.spots("tokyo"), "couple", 3))
        day1, rest = md.split("**Day 2**", 1)
        day2, rest = rest.split("**Day 3**", 1)
        _, day4 = rest.split("**Day 4**", 1)
        self.assertNotIn("하코네 온천", day1)
        self.assertIn("하코네 온천", day2)
        self.assertNotIn("하코네 온천", day4)

    def test_short_trip_omits_full_day_trip(self) -> None:
        md = "\n".join(_render_day_plan_md("도쿄", self.spots("tokyo"), "couple", 1))
        self.assertNotIn("하코네 온천", md)

    def test_food_time_and_area_constraints_are_applied(self) -> None:
        md = "\n".join(_render_day_plan_md("도쿄", self.spots("tokyo"), "couple", 3))
        self.assertNotIn("🌙 저녁 · [츠키지 장외시장]", md)
        self.assertIn("우에노 공원", md)
        self.assertIn("우에노 아메요코 시장", md)
        self.assertIn("🚶 우에노 구역 내 · 도보 약 10분", md)
        self.assertIn("대중교통 약 30분", md)

    def test_purpose_sights_per_day_changes_slots(self) -> None:
        friends = "\n".join(_render_day_plan_md("교토", self.spots("kyoto"), "friends", 2))
        solo = "\n".join(_render_day_plan_md("교토", self.spots("kyoto"), "solo", 2))
        self.assertIn("🌆 늦은 오후", friends)
        self.assertNotIn("🌆 늦은 오후", solo)

    def test_curation_schema_and_full_day_metadata(self) -> None:
        required = {
            "name_ko", "name_local", "category", "one_liner", "best_season",
            "stability", "status", "verified_date", "search_query",
        }
        allowed_categories = {"culture", "food", "nature", "shopping", "onsen"}
        allowed_statuses = {"active", "seasonal", "under_renovation"}
        for city, data in self.data["cities"].items():
            for spot in data["spots"]:
                with self.subTest(city=city, spot=spot["name_ko"]):
                    self.assertFalse(required - spot.keys())
                    self.assertIn(spot["category"], allowed_categories)
                    self.assertIn(spot["status"], allowed_statuses)
                    if "당일치기" in spot["name_ko"]:
                        self.assertEqual(spot.get("trip_scope"), "full_day")
                        self.assertGreaterEqual(spot.get("visit_min", 0), 360)
                        self.assertTrue(spot.get("avoid_first_day"))
                        self.assertTrue(spot.get("avoid_last_day"))

    def test_reviewed_status_changes_are_current(self) -> None:
        sapporo = {s["name_ko"]: s for s in self.spots("sapporo")}
        okinawa = {s["name_ko"]: s for s in self.spots("okinawa")}
        self.assertEqual(sapporo["홋카이도청 구본청사 (붉은벽돌)"]["status"], "active")
        self.assertEqual(okinawa["슈리성"]["status"], "under_renovation")
        self.assertEqual(okinawa["슈리성"]["reopen_date"], "2026-11-23")
        md = _render_destinations_md(
            self.data["cities"]["okinawa"], self.spots("okinawa"), None,
        )
        self.assertIn("일반 공개 예정 2026-11-23", md)

    def test_all_curated_spots_use_official_google_maps_search_urls(self) -> None:
        checked_spots = 0
        for country in CURATED_COUNTRIES:
            path = ROOT / "curation" / f"destinations_{country.lower()}.json"
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data.get("cities"), country)

            for city_key, city_data in data["cities"].items():
                rendered = _render_destinations_md(
                    city_data,
                    city_data["spots"],
                    None,
                    last_reviewed=data.get("last_reviewed", "-"),
                    country=country,
                )
                self.assertIn("[Google 지도에서 보기]", rendered)
                self.assertNotIn("카카오맵", rendered)
                self.assertNotIn("map.kakao.com", rendered)

                for spot in city_data["spots"]:
                    with self.subTest(
                        country=country, city=city_key, spot=spot["name_ko"],
                    ):
                        url = _map_link(country, spot["search_query"])
                        parsed = urlparse(url)
                        params = parse_qs(parsed.query)
                        self.assertEqual(parsed.scheme, "https")
                        self.assertEqual(parsed.netloc, "www.google.com")
                        self.assertEqual(parsed.path, "/maps/search/")
                        self.assertEqual(params.get("api"), ["1"])
                        self.assertEqual(params.get("query"), [spot["search_query"]])
                        self.assertIn(url, rendered)
                        checked_spots += 1

                itinerary = "\n".join(_render_day_plan_md(
                    city_data["name_ko"], city_data["spots"], "friends", 2,
                    country=country, city_key=city_key,
                ))
                self.assertNotIn("map.kakao.com", itinerary)
                for url in re.findall(r"\]\((https://[^)]+)\)", itinerary):
                    parsed = urlparse(url)
                    self.assertEqual(parsed.netloc, "www.google.com")
                    self.assertEqual(parsed.path, "/maps/search/")
                    self.assertEqual(parse_qs(parsed.query).get("api"), ["1"])

        self.assertGreater(checked_spots, 0)

    def test_failed_alert_lookup_is_not_shown_as_green(self) -> None:
        """조회 실패를 🟢 로 보이면 안전이 확인된 것으로 오인된다 (QA v2 P0-4)."""
        failed = {"level": 0, "level_name": "실시간 확인 실패",
                  "issued_at": "-", "note": "-", "ok": False}
        ok = {"level": 0, "level_name": "미발령",
              "issued_at": "2026-07-01", "note": "-", "ok": True}

        failed_lines = _render_essentials_md(None, None, failed, None, "JP")
        self.assertTrue(any("⚪ **안전**" in l for l in failed_lines))
        self.assertFalse(any("🟢" in l for l in failed_lines))
        self.assertTrue(any("0404.go.kr" in l for l in failed_lines))

        self.assertTrue(any("🟢 **안전**" in l for l in _render_essentials_md(None, None, ok, None, "JP")))

        self.assertNotIn("🟢", _render_alert_md("JP", failed))
        self.assertIn("🟢", _render_alert_md("JP", ok))

    def test_alert_fetch_failure_is_flagged_not_ok(self) -> None:
        """실패 폴백이 ok=False 를 달고 나와야 렌더러가 구분할 수 있다."""
        tb_api._cache._store.pop("mofa_alert:JP", None)
        with patch.dict(os.environ, {"MOFA_API_KEY": "key"}), \
             patch.object(tb_api, "_fetch_mofa_warn_all", return_value=None):
            alert = tb_api._fetch_mofa_alert("JP")
        self.assertFalse(alert["ok"])
        self.assertEqual(alert["level"], 0)

    def test_estimated_dates_are_not_shown_as_confirmed(self) -> None:
        """날짜 미입력 시 구체적 날짜를 노출하면 호스트 LLM 이 확정 일정으로 옮긴다 (QA v2 P0-3)."""
        none_basis = resolve_trip_dates(None, None, None, 2)
        self.assertEqual(none_basis["basis"], "none")
        month_basis = resolve_trip_dates(None, None, 8, 2)
        self.assertEqual(month_basis["basis"], "month")
        exact = resolve_trip_dates("2026-09-10", None, None, 3)
        self.assertEqual(exact["basis"], "exact")

        self.assertEqual(
            _trip_period_label("2026-08-16", "2026-08-18", "2박3일", "none", 8),
            "출발일 미정 · 2박3일 기준",
        )
        self.assertNotIn("2026-08-16", _trip_period_label("2026-08-16", "2026-08-18", "2박3일", "month", 8))
        self.assertIn("8월 여행 기준", _trip_period_label("2026-08-16", "2026-08-18", "2박3일", "month", 8))
        self.assertIn("2026-09-10", _trip_period_label("2026-09-10", "2026-09-13", "3박4일", "exact", 9))

    def test_itinerary_has_no_host_llm_directives_and_keeps_practical_info(self) -> None:
        """내부 지시문은 작은 LLM 이 못 따르면 미완성으로 노출된다 (QA v2 P1-4).
        동시에 준비사항(전압·시차·긴급번호)은 일정 툴 하나로 답할 수 있어야 한다 (P0-2)."""
        d = resolve_trip_dates(None, None, None, 2)
        md = _render_itinerary_md(
            "JP", "도쿄 여행", [], d["depart"].isoformat(), d["return"].isoformat(),
            None, "couple", None, "2박3일", "tokyo",
            depart_month=d["depart"].month, estimated=d["estimated"],
            spots=self.spots("tokyo"), nights=2,
            visa={"required": False, "duration_days": 90},
            alert={"level": 0, "level_name": "실시간 확인 실패",
                   "issued_at": "-", "note": "-", "ok": False},
            season={"season": "peak", "note": "여름 성수기"},
            basis=d["basis"],
        )
        for directive in ("안내해 주세요", "설명해 주세요", "해 주세요"):
            self.assertNotIn(directive, md)
        self.assertIn("출발일 미정", md)
        self.assertNotIn(d["depart"].isoformat(), md)
        self.assertIn("🔌 **전압**", md)
        self.assertIn("🕘 **시차**", md)
        self.assertIn("🚨 **긴급번호**", md)

    def test_itinerary_description_asks_host_to_preserve_sources(self) -> None:
        """PlayMCP 등록 description 은 1,024자 이내 + 서비스명 병기 + 보존 지시를 함께 만족해야 한다."""
        import asyncio

        import travel_briefing_mcp as server

        tools = asyncio.run(server.mcp.list_tools())
        desc = next(t for t in tools if t.name == "recommend_itinerary").description
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("Travel Briefing(트래블 브리핑)", desc)
        self.assertIn("preserve", desc.lower())
        self.assertIn("OUTPUT CONTRACT", desc)
        self.assertIn("먼저 보는 핵심 요약", desc)
        for kw in ("dates", "warnings", "map URLs"):
            self.assertIn(kw, desc)

    def test_frequently_mentioned_spots_reach_the_day_plan(self) -> None:
        """🔥 인기 명소로 뽑아 놓고 코스에는 없으면 응답이 자기모순이다.
        시부야는 후기 2건 언급인데도 배치 순서 꼬리에 밀려 잘렸던 실제 사례."""
        mentions = {"센소지 (아사쿠사)": 2, "긴자": 2, "시부야": 2}
        for nights in (2, 3):
            with self.subTest(nights=nights):
                md = "\n".join(_render_day_plan_md(
                    "도쿄", self.spots("tokyo"), "couple", nights,
                    mentions, "JP", "tokyo",
                ))
                for name in mentions:
                    self.assertIn(name, md)

    def test_mentions_do_not_disturb_order_when_absent(self) -> None:
        """언급 데이터가 없을 때(검색 실패·콜드스타트)는 기존 카테고리 순서를 그대로 지킨다."""
        spots = [s for s in self.spots("tokyo") if s["category"] != "food"]
        baseline = _pick_spots_for_purpose(spots, "couple", 7, None)
        empty = _pick_spots_for_purpose(spots, "couple", 7, {})
        self.assertEqual([s["name_ko"] for s in baseline], [s["name_ko"] for s in empty])
        # 카테고리 다양성 유지 — 첫 라운드에 목적 카테고리가 골고루 들어간다
        self.assertGreaterEqual(len({s["category"] for s in baseline[:4]}), 3)

    def test_nights_parameter_states_nights_not_days(self) -> None:
        """설명이 없으면 호스트 LLM 이 "2박3일"에서 뒤 숫자를 집어 nights=3 을 넘긴다 (실제 사고)."""
        import asyncio

        import travel_briefing_mcp as server

        tools = asyncio.run(server.mcp.list_tools())
        checked = 0
        for t in tools:
            prop = t.inputSchema["properties"].get("nights")
            if not prop:
                continue
            with self.subTest(tool=t.name):
                desc = prop.get("description", "")
                self.assertIn("NIGHTS", desc)
                self.assertIn("2박3일", desc)
            checked += 1
        self.assertGreaterEqual(checked, 3)

    def test_exchange_unit_is_divided_before_converting_cost(self) -> None:
        """수출입은행은 JPY·IDR 을 100단위로 고시한다 — 나누지 않으면 비용이 100배가 된다."""
        self.assertAlmostEqual(
            _krw_per_unit({"quoted": True, "rate": {"currency": "JPY(100)", "deal_bas_r": 913.27}}),
            9.1327, places=4,
        )
        self.assertAlmostEqual(
            _krw_per_unit({"quoted": True, "rate": {"currency": "IDR(100)", "deal_bas_r": 8.5}}),
            0.085, places=4,
        )
        # 단위 표기가 없는 통화는 1단위 그대로
        self.assertAlmostEqual(
            _krw_per_unit({"quoted": True, "rate": {"currency": "CNH", "deal_bas_r": 190.5}}),
            190.5, places=4,
        )
        # 미고시 통화는 환산 불가 → None
        self.assertIsNone(_krw_per_unit({"quoted": False, "rate": None, "usd": {"deal_bas_r": 1380}}))

    def test_daily_cost_covers_every_curated_city(self) -> None:
        """큐레이션 도시는 5키 일반형 스키마를 모두 갖춰야 도시 간 경비 밀도가 일관된다."""
        # 3키(transport/food/admission)로 되돌아가면 카페·간식·예비비가 빠져
        # 특정 도시만 합계가 낮게 나오는 불일치가 생긴다 (QA v3 P0-1).
        keys = ("transport", "food", "cafe_snack", "admission", "contingency")
        for country in CURATED_COUNTRIES:
            path = ROOT / "curation" / f"destinations_{country.lower()}.json"
            with path.open(encoding="utf-8") as f:
                cities = json.load(f)["cities"]
            for city_key in cities:
                with self.subTest(country=country, city=city_key):
                    cost = _CITY_DAILY_COST.get(city_key)
                    self.assertIsNotNone(cost)
                    for k in keys:
                        self.assertGreater(cost.get(k, 0), 0, f"{city_key}.{k}")

    def test_every_curated_country_answers_season_judgment(self) -> None:
        """계절 판단이 대만에만 있으면 주력국(일본) 여름 질문에 여행 적합성이 빠진다 (QA v3 P1-2)."""
        for country in CURATED_COUNTRIES:
            static = _STATIC.get(country, {})
            advice = static.get("monthly_advice", {})
            with self.subTest(country=country):
                self.assertTrue(advice, f"{country} monthly_advice 비어 있음")
                for month, entry in advice.items():
                    self.assertEqual(
                        set(entry), {"verdict", "schedule", "backup"},
                        f"{country}.{month} 키 불일치",
                    )
        # 월/날짜 단서가 있을 때만 노출되고, 단서가 없으면 조용히 생략한다.
        jp_aug = "\n".join(_render_season_advice_md("JP", 8, "month"))
        self.assertIn("8월 여행 판단", jp_aug)
        self.assertEqual(_render_season_advice_md("JP", 8, "none"), [])

    def test_cost_section_shows_totals_and_degrades_without_quote(self) -> None:
        """비용은 서버가 확정 숫자로 줘야 호스트 LLM 이 지어내지 않는다."""
        exch = {"quoted": True,
                "rate": {"currency": "JPY(100)", "deal_bas_r": 913.27,
                         "search_date": "2026-07-17", "is_stale": False}}
        md = "\n".join(_render_cost_md("JP", "tokyo", exch, 2))
        self.assertIn("16,000 JPY", md)
        self.assertIn("48,000 JPY", md)
        self.assertIn("약 146,000원", md)
        self.assertIn("항공·숙박·쇼핑 제외", md)

        # 미고시 통화는 원화 환산 없이 현지 통화로만
        vnd = "\n".join(_render_cost_md("VN", "danang", {"quoted": False, "rate": None}, 2))
        self.assertIn("1,020,000 VND", vnd)
        self.assertNotIn("원)", vnd)
        self.assertIn("미고시", vnd)

        # 비용 데이터가 없는 도시는 섹션 자체를 생략
        self.assertEqual(_render_cost_md("JP", None, exch, 2), [])

    def test_couple_cost_keeps_people_and_exclusion_assumptions(self) -> None:
        """카카오가 축약해도 커플 2인과 제외 항목이 숫자 곁에 남아야 한다."""
        md = "\n".join(_render_cost_md("JP", "tokyo", None, 2, "couple", None))
        self.assertIn("2인 하루 일반형", md)
        self.assertIn("32,000 JPY", md)
        self.assertIn("3일 2인 현지 체류비", md)
        self.assertIn("96,000 JPY", md)
        self.assertIn("인원 미입력 시 2명", md)
        self.assertIn("항공·숙박·쇼핑 제외", md)

        atomic = next(line for line in md.splitlines() if "필수 예산 요약" in line)
        for value in ("커플 2인", "32,000 JPY/일", "3일 96,000 JPY", "항공·숙박·쇼핑 제외"):
            self.assertIn(value, atomic)

    def test_priority_summary_keeps_judgment_budget_and_meals_together(self) -> None:
        """앞부분만 쓰는 카카오 응답에서도 계절 판단·2인 예산·Day별 음식이 남아야 한다."""
        with (ROOT / "curation" / "destinations_tw.json").open(encoding="utf-8") as f:
            spots = json.load(f)["cities"]["taipei"]["spots"]
        d = resolve_trip_dates(None, None, 8, 4)
        md = _render_itinerary_md(
            "TW", "타이베이 8월 커플 여행", [],
            d["depart"].isoformat(), d["return"].isoformat(), None,
            "couple", None, "4박5일", "taipei", depart_month=8,
            estimated=True, spots=spots, nights=4,
            visa={"required": False, "duration_days": 90},
            alert={"level": 0, "level_name": "미발령", "issued_at": "-",
                   "note": "-", "ok": True},
            season={"season": "off", "note": "여름 비수기"}, basis="month",
        )
        summary_start = md.index("## ✅ 먼저 보는 핵심 요약")
        summary_end = md.index("## ✈️ 출발 전 필수", summary_start)
        summary = md[summary_start:summary_end]
        self.assertLess(summary_start, md.index("## 🗓 4박5일 코스 제안"))
        for value in (
            "여행 판단", "여행은 가능", "커플 2인", "4,800 TWD/일",
            "5일 24,000 TWD", "항공·숙박·쇼핑 제외",
        ):
            self.assertIn(value, summary)
        self.assertEqual(summary.count("**Day "), 5)
        for line in (line for line in summary.splitlines() if line.startswith("- **Day ")):
            self.assertIn("식사 ", line)
            self.assertRegex(line, r"1인 약 [\d,]+~[\d,]+ TWD")

    def test_city_meals_are_specific_varied_and_priced(self) -> None:
        """'현지 로컬 맛집' 반복 대신 도시 대표 메뉴를 끼니별로 다르게 제안한다."""
        for country, city, city_ko, path in (
            ("JP", "tokyo", "도쿄", ROOT / "curation" / "destinations_jp.json"),
            ("TW", "taipei", "타이베이", ROOT / "curation" / "destinations_tw.json"),
        ):
            with path.open(encoding="utf-8") as f:
                spots = json.load(f)["cities"][city]["spots"]
            md = "\n".join(_render_day_plan_md(
                city_ko, spots, "couple", 4, country=country, city_key=city,
            ))
            with self.subTest(city=city):
                self.assertNotIn("현지 로컬 맛집", md)
                self.assertNotIn("대표 먹거리:", md)
                currency = {"JP": "JPY", "TW": "TWD"}[country]
                self.assertRegex(md, rf"1인 약 [\d,]+~[\d,]+ {currency}")
                menus = re.findall(r"\*\*([^*]+ — 1인 약 [^*]+)\*\*", md)
                self.assertGreaterEqual(len(menus), 5)
                self.assertEqual(len(menus), len(set(menus)))

    def test_taipei_long_trip_is_dense_and_separates_jiufen(self) -> None:
        with (ROOT / "curation" / "destinations_tw.json").open(encoding="utf-8") as f:
            spots = json.load(f)["cities"]["taipei"]["spots"]
        md = "\n".join(_render_day_plan_md(
            "타이베이", spots, "couple", 4, country="TW", city_key="taipei",
        ))
        self.assertNotIn("자유 시간 (카페·산책·쇼핑)", md)
        day2 = md.split("**Day 2**", 1)[1].split("**Day 3**", 1)[0]
        self.assertIn("지우펀", day2)
        self.assertIn("약 6시간", day2)
        self.assertNotIn("국립고궁박물원", day2)
        for name in ("디화제", "베이터우", "단수이"):
            self.assertIn(name, md)
        day4 = md.split("**Day 4**", 1)[1].split("**Day 5**", 1)[0]
        self.assertNotIn("국립고궁박물원", day4)

    def test_every_non_japan_curated_city_supports_a_long_trip(self) -> None:
        """일본 외 도시도 4박5일에 장소가 소진돼 기존 자유시간 문구로 도배되면 안 된다."""
        for country in (c for c in CURATED_COUNTRIES if c != "JP"):
            path = ROOT / "curation" / f"destinations_{country.lower()}.json"
            with path.open(encoding="utf-8") as f:
                cities = json.load(f)["cities"]
            for city_key, city_data in cities.items():
                spots = city_data["spots"]
                md = "\n".join(_render_day_plan_md(
                    city_data["name_ko"], spots, "couple", 4,
                    country=country, city_key=city_key,
                ))
                with self.subTest(country=country, city=city_key):
                    self.assertGreaterEqual(len(spots), 10)
                    self.assertEqual(md.count("**Day "), 5)
                    self.assertNotIn("자유 시간 (카페·산책·쇼핑)", md)
                    self.assertGreaterEqual(md.count("https://www.google.com/maps/search/"), 8)

                    full_day_names = {
                        s["name_ko"] for s in spots if s.get("trip_scope") == "full_day"
                    }
                    if full_day_names:
                        day1 = md.split("**Day 1**", 1)[1].split("**Day 2**", 1)[0]
                        day2 = md.split("**Day 2**", 1)[1].split("**Day 3**", 1)[0]
                        day5 = md.split("**Day 5**", 1)[1]
                        self.assertTrue(any(name in day2 for name in full_day_names))
                        self.assertFalse(any(name in day1 for name in full_day_names))
                        self.assertFalse(any(name in day5 for name in full_day_names))

    def test_august_taiwan_advice_answers_suitability_question(self) -> None:
        md = "\n".join(_render_season_advice_md("TW", 8, "month"))
        for keyword in ("8월 여행 판단", "여행은 가능", "폭염", "태풍", "박물관·쇼핑몰"):
            self.assertIn(keyword, md)
        self.assertEqual(_render_season_advice_md("TW", 8, "none"), [])

    def test_dated_event_is_hedged_when_departure_is_unknown(self) -> None:
        """출발일 미정인데 특정일 행사를 확정 추천하면 여행 기간과 어긋난다 (QA v2 P0-3)."""
        exact = "\n".join(_render_country_traits_md("JP", "tokyo", 8, "exact"))
        self.assertIn("스미다가와 불꽃축제", exact)
        self.assertNotIn("개최 기간을 확인", exact)

        for basis in ("none", "month"):
            with self.subTest(basis=basis):
                hedged = "\n".join(_render_country_traits_md("JP", "tokyo", 8, basis))
                self.assertIn("개최 기간을 확인", hedged)

    def test_itinerary_drops_redundant_sections_when_day_plan_exists(self) -> None:
        """호스트 LLM 출력 예산이 한정적이라(4,000자 → 600자) 코스를 살리려면 중복을 줄여야 한다."""
        posts = [{"title": f"후기 {i}", "link": f"https://b.test/{i}",
                  "description": "d" * 140, "bloggername": "b", "postdate": "20260709"}
                 for i in range(5)]
        d = resolve_trip_dates(None, None, None, 2)
        kwargs = dict(
            depart_month=8, estimated=True, nights=2,
            visa={"required": False, "duration_days": 90},
            alert={"level": 0, "level_name": "미발령",
                   "issued_at": "-", "note": "-", "ok": True},
            season={"season": "peak", "note": "성수기"}, basis=d["basis"],
        )
        with_plan = _render_itinerary_md(
            "JP", "q", posts, d["depart"].isoformat(), d["return"].isoformat(),
            None, "couple", None, "2박3일", "tokyo",
            spots=self.spots("tokyo"), **kwargs,
        )
        without_plan = _render_itinerary_md(
            "JP", "q", posts, d["depart"].isoformat(), d["return"].isoformat(),
            None, "couple", None, "2박3일", None, spots=None, **kwargs,
        )
        # 코스가 있으면 방향 제안은 중복 → 생략, 후기는 2건으로 축약
        self.assertNotIn("방향 제안", with_plan)
        self.assertEqual(with_plan.count("https://b.test/"), 2)
        self.assertNotIn("d" * 70, with_plan)
        self.assertIn("💰 예상 현지 경비", with_plan)
        # 코스가 없으면 방향 제안과 후기 5건이 그대로 살아 있어야 한다
        self.assertIn("방향 제안", without_plan)
        self.assertEqual(without_plan.count("https://b.test/"), 5)
        self.assertLess(len(with_plan), 5500)

    def test_naver_display_is_shared_by_fetch_and_warmer(self) -> None:
        default = inspect.signature(_fetch_naver_blog).parameters["display"].default
        self.assertEqual(default, _NAVER_BLOG_DISPLAY)
        self.assertIn("_NAVER_BLOG_DISPLAY", inspect.getsource(_warm_naver_blog_once))

    def test_naver_results_keep_date_and_remove_duplicate_links(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "items": [
                {
                    "title": "<b>교토</b> 후기",
                    "link": "https://example.test/post/1",
                    "description": "여행 후기",
                    "bloggername": "테스트 블로그",
                    "postdate": "20260701",
                },
                {
                    "title": "중복",
                    "link": "https://example.test/post/1",
                    "description": "같은 링크",
                    "bloggername": "테스트 블로그",
                    "postdate": "20260701",
                },
            ]
        }
        query = "qa-v1-duplicate-link-test"
        tb_api._cache._store.pop(f"naver_blog:{query}:2", None)
        with patch.dict(os.environ, {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "secret"}), \
             patch.object(tb_api._http, "get", return_value=response):
            items = _fetch_naver_blog(query, display=2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["postdate"], "20260701")
        self.assertEqual(items[0]["title"], "교토 후기")


if __name__ == "__main__":
    unittest.main()
