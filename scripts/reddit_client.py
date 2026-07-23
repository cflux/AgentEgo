"""Central, fail-closed Reddit RSS and direct-media client.

Only RSS and direct Reddit-hosted media are implemented here. JSON, OAuth, and
browser transports must be added as separate adapters; they must never inherit
RSS credentials or cookies.
"""
from __future__ import annotations

import contextlib
import html
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from reddit_request_pacing import RedditRateLimited, RedditRequestGate, reddit_open

ATOM = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) "
    "Gecko/20100101 Firefox/130.0"
)
REDDIT_HOST = "www.reddit.com"
IMAGE_HOST = "i.redd.it"
VIDEO_HOST = "v.redd.it"
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_MEDIA_BYTES = 25 * 1024 * 1024
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DIRECT_IMAGE_RE = re.compile(
    r"https://i\.redd\.it/[^\s\"'<>]+?\.(?:jpe?g|png)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
DIRECT_VIDEO_RE = re.compile(r"https://v\.redd\.it/[^\s\"'<>]+", re.IGNORECASE)
PERMALINK_RE = re.compile(
    r"https://www\.reddit\.com/r/[^/\s\"'<>]+/comments/[^/\s\"'<>]+(?:/[^\s\"'<>]*)?/?",
    re.IGNORECASE,
)


class RedditClientError(RuntimeError):
    """Base class for safe, user-facing Reddit client failures."""


class RedditResponseError(RedditClientError):
    """Reddit returned an unusable response."""


class RedditMediaError(RedditClientError):
    """A media URL or downloaded payload failed validation."""


class UnsupportedRedditTransport(RedditClientError):
    """A transport that has not been approved or implemented was requested."""


@dataclass(frozen=True)
class RedditPost:
    title: str
    subreddit: str
    permalink: str
    media_url: str
    media_kind: Literal["image", "video"]

    @property
    def image_url(self) -> str:
        """Compatibility alias for existing image collectors."""
        return self.media_url

    @property
    def vreddit_url(self) -> str:
        """Compatibility alias for existing video collectors."""
        return self.media_url


def _safe_subreddit(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_+-]{1,80}", value):
        raise RedditClientError("invalid subreddit name")
    return value


def _validate_permalink(value: str) -> str:
    value = html.unescape(value).strip()
    match = PERMALINK_RE.search(value)
    if not match:
        return ""
    return match.group(0).rstrip("/") + "/"


def _validate_media_url(value: str, *, kind: str | None = None) -> tuple[str, Literal["image", "video"]]:
    value = html.unescape(value).strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise RedditMediaError("media URL is not a safe HTTPS URL")
    host = (parsed.hostname or "").lower()
    if host == IMAGE_HOST and Path(parsed.path).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
        return value, "image"
    if host == VIDEO_HOST and parsed.path.strip("/"):
        return value, "video"
    raise RedditMediaError("media URL is not a supported direct Reddit asset")


def _read_bounded(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RedditResponseError("Reddit response exceeded the safety size limit")
    return data


def _entry_permalink(entry: ET.Element, content: str) -> str:
    for link in entry.findall("atom:link", ATOM):
        candidate = _validate_permalink(link.get("href", ""))
        if candidate:
            return candidate
    return _validate_permalink(content)


def parse_rss(raw: str | bytes, subreddit: str) -> list[RedditPost]:
    """Normalize Atom entries into direct image/video Reddit posts."""
    subreddit = _safe_subreddit(subreddit)
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, TypeError) as error:
        raise RedditResponseError("Reddit RSS response was not valid Atom XML") from error

    posts: list[RedditPost] = []
    for entry in root.findall("atom:entry", ATOM):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM) or "").strip()
        content = entry.findtext("atom:content", default="", namespaces=ATOM) or ""
        if not title:
            continue
        permalink = _entry_permalink(entry, content)
        if not permalink:
            continue

        media_match = DIRECT_IMAGE_RE.search(content)
        media_kind: Literal["image", "video"] | None = None
        if media_match:
            media_url = media_match.group(0)
            media_kind = "image"
        else:
            video_match = DIRECT_VIDEO_RE.search(content)
            if not video_match:
                continue
            media_url = video_match.group(0).rstrip(".,)")
            media_kind = "video"
        try:
            media_url, checked_kind = _validate_media_url(media_url, kind=media_kind)
        except RedditMediaError:
            continue
        posts.append(RedditPost(title, subreddit, permalink, media_url, checked_kind))
    return posts


class RunLock:
    """Cross-process single-flight lock; contention fails closed."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = self.path.open("x")
        except FileExistsError as error:
            # A SIGKILL/command timeout can leave the marker behind. Recover
            # only when its recorded PID is definitely no longer alive.
            try:
                recorded_pid = int(self.path.read_text().strip())
                os.kill(recorded_pid, 0)
            except (FileNotFoundError, ProcessLookupError, ValueError):
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
                self._handle = self.path.open("x")
            except PermissionError:
                raise RedditClientError("another Reddit collector run is already active") from error
            else:
                raise RedditClientError("another Reddit collector run is already active") from error
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._handle is not None:
            self._handle.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        return False


class RedditClient:
    """RSS-only Reddit client with injectable network and timing dependencies."""

    def __init__(
        self,
        *,
        gate: RedditRequestGate | None = None,
        opener: Callable = urllib.request.urlopen,
        user_agent: str = DEFAULT_USER_AGENT,
        max_feed_bytes: int = MAX_FEED_BYTES,
        max_media_bytes: int = MAX_MEDIA_BYTES,
        lock_path: Path = Path("/tmp/hermes-reddit-client.lock"),
    ) -> None:
        self.gate = gate or RedditRequestGate()
        self.opener = opener
        self.user_agent = user_agent
        self.max_feed_bytes = max_feed_bytes
        self.max_media_bytes = max_media_bytes
        self.lock_path = lock_path

    def _request(self, url: str, *, nsfw: bool = False) -> urllib.request.Request:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != REDDIT_HOST:
            raise RedditClientError("RSS request must target www.reddit.com over HTTPS")
        headers = {"User-Agent": self.user_agent, "Accept": "application/atom+xml, application/xml"}
        if nsfw:
            headers["Cookie"] = "over18=1"
        return urllib.request.Request(url, headers=headers)

    def fetch_rss(
        self,
        subreddit: str,
        *,
        sort: str = "top",
        period: str = "day",
        nsfw: bool = False,
    ) -> list[RedditPost]:
        subreddit = _safe_subreddit(subreddit)
        if sort not in {"top", "hot", "new"} or period not in {"hour", "day", "week", "month", "year", "all"}:
            raise RedditClientError("invalid RSS sort or period")
        query = urllib.parse.urlencode({"t": period}) if sort == "top" else ""
        url = f"https://{REDDIT_HOST}/r/{urllib.parse.quote(subreddit, safe='')}/{sort}.rss"
        if query:
            url += f"?{query}"
        request = self._request(url, nsfw=nsfw)
        try:
            with reddit_open(request, timeout=30, gate=self.gate, opener=self.opener) as response:
                content_type = (response.headers.get("Content-Type", "") or "").lower()
                if content_type and "html" in content_type and "xml" not in content_type:
                    raise RedditResponseError("Reddit returned HTML instead of RSS")
                raw = _read_bounded(response, self.max_feed_bytes)
        except RedditRateLimited:
            raise
        except RedditClientError:
            raise
        except (urllib.error.URLError, OSError) as error:
            raise RedditResponseError("Reddit RSS request failed") from error
        return parse_rss(raw, subreddit)

    def download_media(self, media_url: str, destination_dir: Path, *, stem: str = "media") -> Path:
        checked_url, kind = _validate_media_url(media_url)
        if kind != "image":
            raise RedditMediaError("this downloader only accepts direct image assets")
        request = urllib.request.Request(checked_url, headers={"User-Agent": self.user_agent, "Accept": "image/*"})
        try:
            with reddit_open(request, timeout=30, gate=self.gate, opener=self.opener) as response:
                data = _read_bounded(response, self.max_media_bytes)
        except RedditRateLimited:
            raise
        except (RedditClientError, urllib.error.URLError, OSError) as error:
            if isinstance(error, RedditClientError):
                raise
            raise RedditMediaError("Reddit media download failed") from error

        if data.startswith(b"\xff\xd8\xff"):
            extension = ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = ".png"
        else:
            raise RedditMediaError("downloaded asset is not a JPEG or PNG")
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "media"
        destination = destination_dir / f"{safe_stem}{extension}"
        fd, temporary = tempfile.mkstemp(prefix=f".{safe_stem}-", suffix=".part", dir=destination_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        return destination

    def run_directory(self, prefix: str) -> tempfile.TemporaryDirectory:
        safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix).strip("-") or "reddit-run"
        return tempfile.TemporaryDirectory(prefix=f"{safe_prefix}-", dir="/tmp")

    def single_flight(self) -> RunLock:
        return RunLock(self.lock_path)


def unsupported_transport(name: str) -> None:
    raise UnsupportedRedditTransport(
        f"Reddit transport '{name}' is not implemented or approved; RSS credentials are not reused"
    )


__all__ = [
    "RedditClient", "RedditClientError", "RedditMediaError", "RedditPost",
    "RedditRateLimited", "RedditResponseError", "RunLock", "parse_rss",
    "unsupported_transport",
]
