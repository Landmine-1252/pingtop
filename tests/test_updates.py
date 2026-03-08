from __future__ import annotations

import json
import unittest
from unittest import mock

from pingtop.updates import (
    UpdateManager,
    build_release_api_url,
    fetch_latest_release,
    is_newer_version,
    normalize_repo_url,
    parse_version_tag,
)
from pingtop.version import __version__


class UpdateHelpersTests(unittest.TestCase):
    def test_code_version_matches_release_tag_format(self) -> None:
        self.assertNotIn("v", __version__)
        self.assertIsNotNone(parse_version_tag(f"v{__version__}"))

    def test_normalize_repo_url_handles_https_and_ssh(self) -> None:
        self.assertEqual(
            normalize_repo_url("https://github.com/Landmine-1252/pingtop.git"),
            "https://github.com/Landmine-1252/pingtop",
        )
        self.assertEqual(
            normalize_repo_url("git@github.com:Landmine-1252/pingtop.git"),
            "https://github.com/Landmine-1252/pingtop",
        )

    def test_parse_and_compare_versions(self) -> None:
        self.assertEqual(parse_version_tag("v1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_version_tag("1.2.3"))
        self.assertTrue(is_newer_version("v0.1.0", "v0.2.0"))
        self.assertFalse(is_newer_version("v0.2.0", "v0.1.0"))

    def test_build_release_api_url_requires_github_repo(self) -> None:
        self.assertEqual(
            build_release_api_url("https://github.com/Landmine-1252/pingtop"),
            "https://api.github.com/repos/Landmine-1252/pingtop/releases/latest",
        )
        with self.assertRaises(ValueError):
            build_release_api_url("https://example.com/notgithub/repo")


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class UpdateFetchTests(unittest.TestCase):
    @mock.patch("pingtop.updates.urllib.request.urlopen")
    def test_fetch_latest_release_parses_github_payload(self, urlopen_mock: mock.Mock) -> None:
        urlopen_mock.return_value = _FakeResponse(
            {
                "tag_name": "v0.2.0",
                "html_url": "https://github.com/Landmine-1252/pingtop/releases/tag/v0.2.0",
            }
        )
        version, url = fetch_latest_release("https://github.com/Landmine-1252/pingtop")
        self.assertEqual(version, "v0.2.0")
        self.assertIn("/releases/tag/v0.2.0", url)


class UpdateManagerTests(unittest.TestCase):
    def test_update_manager_marks_available_version(self) -> None:
        manager = UpdateManager(
            current_version="v0.1.0",
            repo_url="https://github.com/Landmine-1252/pingtop",
            enabled=True,
            fetcher=lambda repo_url, timeout=3.0: (
                "v0.2.0",
                "https://github.com/Landmine-1252/pingtop/releases/tag/v0.2.0",
            ),
        )
        manager._run()
        status = manager.snapshot()
        self.assertEqual(status.state, "available")
        self.assertEqual(status.latest_version, "v0.2.0")

    @mock.patch("pingtop.updates.webbrowser.open_new_tab")
    def test_update_manager_opens_release_or_repo_page(self, open_mock: mock.Mock) -> None:
        open_mock.return_value = True
        manager = UpdateManager(
            current_version="v0.1.0",
            repo_url="https://github.com/Landmine-1252/pingtop",
            enabled=True,
            fetcher=lambda repo_url, timeout=3.0: (
                "v0.2.0",
                "https://github.com/Landmine-1252/pingtop/releases/tag/v0.2.0",
            ),
        )
        manager._run()
        ok, url = manager.open_page()
        self.assertTrue(ok)
        self.assertIn("/releases/tag/v0.2.0", url)
        open_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
