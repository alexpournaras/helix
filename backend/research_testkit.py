"""Shared test support for test_research_*.py (PLAN Part D conventions).

- No-network guard: every real httpx transport raises, so a test can only reach a MockTransport,
  a ReplayTransport or the fake provider.
- Patched keys: provider keys are fake values, never read from backend/.env.
- Temporary data folder: each test case gets its own research.sqlite3.
Only synthetic data is used in tests and fixtures.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.research.transport import NetworkBlocked

FAKE_KEYS = {
    "OPENAI_API_KEY": "sk-test-openai",
    "ANTHROPIC_API_KEY": "sk-test-anthropic",
    "NCBI_API_KEY": "",
    "OPENALEX_API_KEY": "",
    "UNPAYWALL_EMAIL": "",
    "SEMANTIC_SCHOLAR_API_KEY": "",
}


def _blocked_sync(self, request):
    raise NetworkBlocked(f"Network access blocked in tests: {request.url}")


async def _blocked_async(self, request):
    raise NetworkBlocked(f"Network access blocked in tests: {request.url}")


_guard_installed = False


def install_network_guard() -> None:
    global _guard_installed
    if _guard_installed:
        return
    httpx.HTTPTransport.handle_request = _blocked_sync  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = _blocked_async  # type: ignore[method-assign]
    _guard_installed = True


install_network_guard()


class ResearchTestCase(unittest.IsolatedAsyncioTestCase):
    """Temp data dir + fake keys + a fresh ResearchDB at `self.db`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="helix-research-test-"))
        self._env = patch.dict(os.environ, {**FAKE_KEYS, "HELIX_DATA_DIR": str(self.tmp),
                                            "HELIX_QUOTA_DB": str(self.tmp / "quota.sqlite3")})
        self._env.start()
        from app.config import ProviderKeys
        self._keys = patch.object(ProviderKeys, "model_config", {**ProviderKeys.model_config, "env_file": None})
        self._keys.start()
        from app.research.db import ResearchDB
        self.db = ResearchDB(self.tmp / "research.sqlite3")
        self.db.init()

    def tearDown(self) -> None:
        self._keys.stop()
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
