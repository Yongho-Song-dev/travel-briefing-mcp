"""큐레이션·검색·일정 생성 회귀 테스트."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import tb_api
from tb_api import _fetch_naver_blog
from tb_config import _NAVER_BLOG_DISPLAY, city_meta
from tb_helpers import (
    _count_spot_mentions,
    _render_city_guide_md,
    _render_day_plan_md,
    _render_destinations_md,
    resolve_city_key,
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
