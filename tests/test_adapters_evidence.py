"""GeminiGroundedEvidence — 실제 API 없이 상태 분류·키 가림·출처 추출을 확인한다."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters

KEY = "SECRET-KEY-123"


class _Types:
    GenerateContentConfig = staticmethod(lambda **kw: NS(**kw))
    Tool = staticmethod(lambda **kw: NS(**kw))
    GoogleSearch = staticmethod(lambda: NS())
    HttpOptions = staticmethod(lambda **kw: NS(**kw))


def _client_factory(behavior):
    class _Client:
        def __init__(self, api_key):
            self.models = NS(generate_content=behavior)
    return NS(Client=_Client)


def _ev(log=None):
    return adapters.GeminiGroundedEvidence(KEY, log or (lambda m: None), "gemini-2.5-flash", 90_000)


class EvidenceAdapterTests(unittest.TestCase):
    def _patch(self, behavior):
        return mock.patch.multiple(adapters, GEMINI_AVAILABLE=True,
                                   google_genai=_client_factory(behavior), genai_types=_Types)

    def test_success_extracts_text_and_sources(self):
        resp = NS(text="判定: 自然", candidates=[NS(grounding_metadata=NS(grounding_chunks=[
            NS(web=NS(title="nii.ac.jp", uri="u1")), NS(web=None)]))])
        with self._patch(lambda **kw: resp):
            r = _ev().check("p")
        self.assertEqual((r.status, r.text), ("ok", "判定: 自然"))
        self.assertEqual([s["host"] for s in r.sources], ["nii.ac.jp"])

    def test_429_is_reported_as_quota_without_raising(self):
        def boom(**kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED. quota")
        with self._patch(boom):
            self.assertEqual(_ev().check("p").status, "quota")

    def test_other_errors_are_error_and_key_is_not_logged(self):
        logged = []

        def boom(**kw):
            raise RuntimeError(f"503 UNAVAILABLE for key={KEY}")
        with self._patch(boom):
            r = _ev(logged.append).check("p")
        self.assertEqual(r.status, "error")
        self.assertTrue(logged)
        self.assertFalse(any(KEY in line for line in logged))

    def test_no_key_or_library_is_unavailable(self):
        with mock.patch.object(adapters, "GEMINI_AVAILABLE", False):
            self.assertEqual(_ev().check("p").status, "unavailable")
        empty = adapters.GeminiGroundedEvidence("", lambda m: None, "m", 1)
        with mock.patch.object(adapters, "GEMINI_AVAILABLE", True):
            self.assertEqual(empty.check("p").status, "unavailable")

    def test_timeout_and_search_tool_are_configured(self):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            return NS(text="x", candidates=[])
        with self._patch(capture):
            _ev().check("p")
        self.assertEqual(seen["config"].http_options.timeout, 90_000)
        self.assertTrue(seen["config"].tools)


if __name__ == "__main__":
    unittest.main()
