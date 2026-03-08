from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, replace
from typing import Callable, Optional


VERSION_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class UpdateStatus:
    state: str
    current_version: str
    latest_version: str = ""
    repo_url: str = ""
    release_url: str = ""
    error_message: str = ""

    @property
    def is_available(self) -> bool:
        return self.state == "available"

    def summary(self) -> str:
        if self.state == "disabled":
            return "disabled"
        if self.state == "checking":
            return "checking"
        if self.state == "available" and self.latest_version:
            return f"{self.latest_version} available"
        if self.state == "current":
            return "current"
        if self.state == "error":
            return "check failed"
        return "-"


def normalize_repo_url(raw_url: str) -> str:
    value = (raw_url or "").strip()
    if not value:
        return ""
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:") :]
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def parse_version_tag(tag: str) -> Optional[tuple[int, int, int]]:
    match = VERSION_TAG_RE.match(tag.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_newer_version(current_version: str, latest_version: str) -> bool:
    current = parse_version_tag(current_version)
    latest = parse_version_tag(latest_version)
    if current is None or latest is None:
        return False
    return latest > current


def build_release_api_url(repo_url: str) -> str:
    normalized = normalize_repo_url(repo_url)
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        raise ValueError("update repo URL must be a GitHub repository URL")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        raise ValueError("update repo URL must include owner and repository name")
    owner, repo = path_parts[0], path_parts[1]
    return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"


def fetch_latest_release(repo_url: str, *, timeout: float = 3.0) -> tuple[str, str]:
    api_url = build_release_api_url(repo_url)
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pingtop-update-check",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag_name = str(payload.get("tag_name", "")).strip()
    html_url = str(payload.get("html_url", "")).strip()
    if parse_version_tag(tag_name) is None:
        raise ValueError("latest GitHub release did not contain a semantic version tag")
    if not html_url:
        html_url = normalize_repo_url(repo_url) + "/releases"
    return tag_name, html_url


class UpdateManager:
    def __init__(
        self,
        *,
        current_version: str,
        repo_url: str,
        enabled: bool,
        fetcher: Optional[Callable[..., tuple[str, str]]] = None,
    ) -> None:
        self.current_version = current_version
        self.repo_url = normalize_repo_url(repo_url)
        self.enabled = enabled and bool(self.repo_url)
        self.fetcher = fetcher or fetch_latest_release
        initial_state = "checking" if self.enabled else "disabled"
        self.lock = threading.RLock()
        self.status = UpdateStatus(
            state=initial_state,
            current_version=current_version,
            repo_url=self.repo_url,
            release_url=self.repo_url + "/releases" if self.repo_url else "",
        )
        self.thread: Optional[threading.Thread] = None
        self.started = False

    def start(self) -> None:
        with self.lock:
            if not self.enabled or self.started:
                return
            self.started = True
            self.thread = threading.Thread(target=self._run, name="update-check", daemon=True)
            self.thread.start()

    def snapshot(self) -> UpdateStatus:
        with self.lock:
            return replace(self.status)

    def open_page(self) -> tuple[bool, str]:
        status = self.snapshot()
        target_url = status.release_url or status.repo_url
        if not target_url:
            return False, "No project URL configured for update checks"
        opened = webbrowser.open_new_tab(target_url)
        if opened:
            return True, target_url
        return False, f"Unable to open browser for {target_url}"

    def _run(self) -> None:
        try:
            latest_version, release_url = self.fetcher(self.repo_url, timeout=3.0)
            if is_newer_version(self.current_version, latest_version):
                next_status = UpdateStatus(
                    state="available",
                    current_version=self.current_version,
                    latest_version=latest_version,
                    repo_url=self.repo_url,
                    release_url=release_url,
                )
            else:
                next_status = UpdateStatus(
                    state="current",
                    current_version=self.current_version,
                    latest_version=latest_version,
                    repo_url=self.repo_url,
                    release_url=release_url,
                )
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
            next_status = UpdateStatus(
                state="error",
                current_version=self.current_version,
                repo_url=self.repo_url,
                release_url=self.repo_url + "/releases" if self.repo_url else "",
                error_message=str(exc),
            )
        with self.lock:
            self.status = next_status
