"""PlayMCP 등록 가이드의 로컬 검증 항목."""

from __future__ import annotations

import asyncio
import re
import unittest

from mcp import types
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from travel_briefing_mcp import mcp


class PlayMCPComplianceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = asyncio.run(mcp.list_tools())

    def test_transport_is_stateless_streamable_http(self) -> None:
        self.assertTrue(mcp.settings.stateless_http)
        self.assertTrue(mcp.settings.json_response)
        self.assertEqual(mcp.settings.streamable_http_path, "/mcp")

    def test_protocol_version_is_accepted(self) -> None:
        self.assertIn(types.LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS)
        self.assertGreaterEqual(types.LATEST_PROTOCOL_VERSION, "2025-03-26")
        self.assertLessEqual(types.LATEST_PROTOCOL_VERSION, "2025-11-25")

    def test_tool_count_and_names(self) -> None:
        self.assertNotIn("kakao", mcp.name.lower())
        self.assertGreaterEqual(len(self.tools), 3)
        self.assertLessEqual(len(self.tools), 10)
        names = [tool.name for tool in self.tools]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, re.compile(r"^[A-Za-z0-9_-]{1,128}$"))
            self.assertNotIn("kakao", name.lower())

    def test_description_excludes_internal_docs(self) -> None:
        """등록 description 에 개발자용 국문 문서(Args/Returns)가 새어나가지 않아야 한다."""
        for tool in self.tools:
            with self.subTest(tool=tool.name):
                description = tool.description or ""
                self.assertTrue(description)
                for marker in ("###", "Args:", "Returns:"):
                    self.assertNotIn(marker, description)

    def test_required_tool_metadata(self) -> None:
        annotation_fields = {
            "title",
            "readOnlyHint",
            "destructiveHint",
            "openWorldHint",
            "idempotentHint",
        }
        for tool in self.tools:
            with self.subTest(tool=tool.name):
                payload = tool.model_dump(by_alias=True, exclude_none=False)
                self.assertTrue(payload["description"])
                self.assertLessEqual(len(payload["description"]), 1024)
                self.assertIn("Travel Briefing(트래블 브리핑)", payload["description"])
                self.assertEqual(payload["inputSchema"].get("type"), "object")
                self.assertTrue(annotation_fields.issubset(payload["annotations"]))
                for field in annotation_fields:
                    self.assertIsNotNone(payload["annotations"][field])


if __name__ == "__main__":
    unittest.main()
