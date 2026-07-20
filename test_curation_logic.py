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
from tb_config import CURATED_COUNTRIES, _NAVER_BLOG_DISPLAY, city_meta
from tb_helpers import (
    _count_spot_mentions,
    _map_link,
    _render_alert_md,
    _render_city_guide_md,
    _render_day_plan_md,
    _render_destinations_md,
    _render_essentials_md,
    _render_itinerary_md,
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
        self.assertIn("🚶 같은 구역 · 도보 이동권", md)
        self.assertNotIn("대중교통 약 30분", md)

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
        for kw in ("dates", "warnings", "map URLs"):
            self.assertIn(kw, desc)

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
