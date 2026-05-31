"""Bilibili series / collection client.

Fetches video series (合集) and season collections for a user through a
Playwright browser page.  Bilibili's public polymer/web-space APIs now
require a browser-originated request (cookies + referrer) to pass
risk-control checks.

Usage
-----
.. code-block:: python

    from playwright.sync_api import sync_playwright
    from videocp.bilibili_series import fetch_seasons_series_list, fetch_all_archives

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        series = fetch_seasons_series_list(page, mid=325864133)
        for s in series:
            print(s.meta_name, s.total)
        videos = fetch_all_archives(page, mid=325864133, season_id=6015227)
        for v in videos:
            print(v.bvid, v.title)
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Page, Response

BILIBILI_SPACE_HOST = "space.bilibili.com"
BILIBILI_API_BASE = "https://api.bilibili.com"

# Regex to extract mid from Bilibili space URLs
MID_FROM_URL_RE = re.compile(
    r"space\.bilibili\.com/(\d+)",
    re.IGNORECASE,
)

BILIBILI_VIDEO_URL_TEMPLATE = "https://www.bilibili.com/video/{bvid}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SeriesVideo:
    """A single video belonging to a series."""

    bvid: str
    """BV identifier, e.g. ``BV1xx411c7mD``."""

    aid: int = 0
    """Numeric aid (av号)."""

    title: str = ""
    """Video title."""

    cover: str = ""
    """Cover image URL."""

    duration_secs: int = 0
    """Duration in seconds."""

    stat_view: int = 0
    """View count."""

    stat_danmaku: int = 0
    """Danmaku (弹幕) count."""

    stat_reply: int = 0
    """Reply / comment count."""

    ctime: int = 0
    """Creation timestamp (Unix seconds)."""

    @property
    def video_url(self) -> str:
        """Canonical video page URL."""
        return BILIBILI_VIDEO_URL_TEMPLATE.format(bvid=self.bvid)


@dataclass(slots=True)
class SeriesInfo:
    """Metadata for a single series / season collection."""

    season_id: int
    """Series identifier used by the archives API."""

    mid: int = 0
    """Owner user mid."""

    meta_name: str = ""
    """Display name, e.g. ``"AI百问"``."""

    meta_description: str = ""
    """Optional description."""

    total: int = 0
    """Total number of videos in this series."""

    cover: str = ""
    """Cover image URL."""

    ctime: int = 0
    """Creation timestamp (Unix seconds)."""

    # The list endpoint also returns the first few archives inline.
    preview_bvids: list[str] = field(default_factory=list)

    @property
    def space_url(self) -> str:
        """URL to the series page on the user's space."""
        return f"https://space.bilibili.com/{self.mid}/channel/seriesdetail?sid={self.season_id}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def extract_mid_from_url(url: str) -> int | None:
    """Extract the numeric user ``mid`` from a Bilibili space URL.

    Supports:
    - ``space.bilibili.com/325864133``
    - ``https://space.bilibili.com/325864133/video``
    - ``https://space.bilibili.com/325864133/channel/seriesdetail?sid=6015227``
    """
    match = MID_FROM_URL_RE.search(url)
    if match:
        return int(match.group(1))
    return None


def _int(v: Any, default: int = 0) -> int:
    """Safely coerce *v* to int, returning *default* on failure."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _str(v: Any, default: str = "") -> str:
    """Safely coerce *v* to str, returning *default* on failure."""
    if v is None:
        return default
    return str(v)


def _parse_series_item(item: dict, mid: int) -> SeriesInfo | None:
    """Parse a single series item from API response.

    Handles both ``seasons_list`` items (meta.season_id) and
    ``series_list`` items (meta.series_id).
    """
    if not isinstance(item, dict):
        return None
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    # season_id / series_id lives inside meta, not at the item top level
    sid = _int(item.get("id")) or _int(item.get("season_id")) or _int(meta.get("season_id")) or _int(meta.get("series_id"))
    if not sid:
        return None

    archives = item.get("archives", [])
    preview_bvids: list[str] = []
    if isinstance(archives, list):
        for a in archives:
            if isinstance(a, dict) and a.get("bvid"):
                preview_bvids.append(str(a["bvid"]))

    ctime = _int(item.get("ctime"))
    if not ctime:
        ctime = _int(meta.get("ctime")) or _int(meta.get("ptime")) or _int(meta.get("mtime"))

    total = _int(meta.get("total"))
    # Some items may use 'video_count' or similar
    if not total:
        total = _int(item.get("total", 0))

    return SeriesInfo(
        season_id=sid,
        mid=mid,
        meta_name=_str(meta.get("name")) or _str(meta.get("title", "")),
        meta_description=_str(meta.get("description")),
        total=total,
        cover=_str(item.get("cover") or meta.get("cover")),
        ctime=ctime,
        preview_bvids=preview_bvids,
    )


def _parse_video_item(a: dict) -> SeriesVideo | None:
    """Parse a single video/archive item from API response."""
    if not isinstance(a, dict):
        return None
    stat = a.get("stat") if isinstance(a.get("stat"), dict) else {}
    return SeriesVideo(
        bvid=_str(a.get("bvid")),
        aid=_int(a.get("aid")),
        title=_str(a.get("title")),
        cover=_str(a.get("cover")),
        duration_secs=_int(a.get("duration")),
        stat_view=_int(stat.get("view")),
        stat_danmaku=_int(stat.get("danmaku")),
        stat_reply=_int(stat.get("reply")),
        ctime=_int(a.get("ctime")),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_seasons_series_list(
    page: Page,
    mid: int,
    *,
    timeout_secs: int = 15,
) -> list[SeriesInfo]:
    """Fetch all series / collections for a Bilibili user.

    Navigates to the user's space page and intercepts the
    ``seasons_series`` API response (captures any URL containing
    "seasons_series" to handle endpoint changes).

    Parameters
    ----------
    page : Page
        A Playwright page.
    mid : int
        Bilibili user mid (e.g. ``325864133``).
    timeout_secs : int
        Max wait time in seconds.

    Returns
    -------
    list[SeriesInfo]
        All series belonging to this user.
    """
    space_url = f"https://space.bilibili.com/{mid}"
    result_container: list[dict[str, Any]] = []
    intercept_event = threading.Event()

    def on_response(response: Response) -> None:
        if intercept_event.is_set():
            return
        url = response.url
        # Match any seasons_series API — both old and new endpoints
        if "api.bilibili.com" not in url:
            return
        if "seasons_series" not in url:
            return
        try:
            body = response.json()
        except Exception:
            return
        if isinstance(body, dict) and body.get("code") == 0:
            result_container.append(body)
            intercept_event.set()

    page.on("response", on_response)

    try:
        page.goto(space_url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_secs * 1000, 8000))
        except Exception:
            pass
        page.wait_for_timeout(2000)

        got_it = intercept_event.wait(timeout=max(2, timeout_secs - 3))
        if not got_it:
            # Fallback: scroll to trigger lazy-loaded API calls
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(2000)
            intercept_event.wait(timeout=3)

    finally:
        page.remove_listener("response", on_response)

    body = result_container[0] if result_container else {}
    data = body.get("data", {}) if isinstance(body, dict) else {}

    result: list[SeriesInfo] = []

    # Try multiple possible data structures (API response format may vary)
    if isinstance(data, dict):
        # New endpoint: data may have 'items_lists' or direct list
        items_lists = data.get("items_lists", {})
        if isinstance(items_lists, dict):
            for list_key in ("seasons_list", "series_list"):
                items = items_lists.get(list_key, [])
                if isinstance(items, list):
                    for item in items:
                        parsed = _parse_series_item(item, mid)
                        if parsed:
                            result.append(parsed)
        if not result:
            # Try top-level keys
            for key in ("seasons_list", "series_list", "list", "items"):
                items = data.get(key)
                if isinstance(items, list):
                    for item in items:
                        parsed = _parse_series_item(item, mid)
                        if parsed:
                            result.append(parsed)
    elif isinstance(data, list):
        for item in data:
            parsed = _parse_series_item(item, mid)
            if parsed:
                result.append(parsed)

    return result


# ---------------------------------------------------------------------------
# Archives / video list helpers (direct API calls via page.evaluate + fetch)
# ---------------------------------------------------------------------------

SEASONS_ARCHIVES_API = (
    f"{BILIBILI_API_BASE}/x/polymer/web-space/seasons_archives_list"
)


def _fetch_archives_page(
    page: Page,
    mid: int,
    season_id: int,
    page_num: int = 1,
    page_size: int = 30,
    timeout_secs: int = 15,
) -> tuple[list[SeriesVideo], int]:
    """Fetch one page of video archives for a series via in-browser fetch().

    Uses ``page.evaluate`` to run ``fetch()`` inside the browser context
    so that cookies and referrer are preserved.

    Returns
    -------
    (videos, total_aids)
        *videos* is the parsed list of :class:`SeriesVideo`.
        *total_aids* is the length of the ``aids`` array in the response
        (used as the total video count for pagination).
    """
    fetch_url = (
        f"{SEASONS_ARCHIVES_API}"
        f"?mid={mid}&season_id={season_id}"
        f"&page_num={page_num}&page_size={page_size}"
    )

    js = f"""
        (async () => {{
            const controller = new AbortController();
            const t = setTimeout(() => controller.abort(), {timeout_secs * 1000});
            try {{
                const resp = await fetch('{fetch_url}', {{
                    signal: controller.signal,
                    credentials: 'include',
                    headers: {{ 'Referer': window.location.href }},
                }});
                clearTimeout(t);
                return {{ ok: resp.ok, status: resp.status, body: await resp.text() }};
            }} catch (e) {{
                clearTimeout(t);
                return {{ ok: false, error: String(e), body: '' }};
            }}
        }})()
    """

    result = page.evaluate(js)
    if not isinstance(result, dict) or not result.get("ok"):
        return [], 0

    try:
        body = json.loads(result["body"])
    except Exception:
        return [], 0

    if not isinstance(body, dict) or body.get("code") != 0:
        return [], 0

    data = body.get("data")
    if not isinstance(data, dict):
        return [], 0

    archives = data.get("archives")
    if not isinstance(archives, list):
        return [], 0

    aids = data.get("aids")
    total = len(aids) if isinstance(aids, list) else len(archives)

    videos: list[SeriesVideo] = []
    for a in archives:
        parsed = _parse_video_item(a)
        if parsed:
            videos.append(parsed)

    return videos, total


def fetch_seasons_archives_list(
    page: Page,
    mid: int,
    season_id: int,
    *,
    page_num: int = 1,
    page_size: int = 30,
    timeout_secs: int = 15,
) -> tuple[list[SeriesVideo], int, int]:
    """Fetch one page of videos from a series.

    Parameters
    ----------
    page : Page
        A Playwright page (must already be on a bilibili.com page).
    mid : int
        Bilibili user mid.
    season_id : int
        Series identifier.
    page_num : int
        1-based page number.
    page_size : int
        Videos per page (max observed: 30).

    Returns
    -------
    tuple[list[SeriesVideo], int, int]
        (videos, total_count, current_page)
    """
    videos, total = _fetch_archives_page(
        page, mid, season_id, page_num, page_size, timeout_secs,
    )
    return videos, total, page_num


def fetch_all_archives(
    page: Page,
    mid: int,
    season_id: int,
    *,
    page_size: int = 30,
    timeout_secs: int = 15,
) -> list[SeriesVideo]:
    """Fetch **all** videos from a series (auto-paginate).

    All pages use direct in-browser ``fetch()`` calls — no page
    navigation is required beyond the initial space-page visit that
    establishes the session.

    Parameters
    ----------
    page : Page
        A Playwright page (must already be on a bilibili.com page).
    mid : int
        Bilibili user mid.
    season_id : int
        Series identifier.
    page_size : int
        Videos per request.

    Returns
    -------
    list[SeriesVideo]
        Every video in the series, in upload order.
    """
    all_videos: list[SeriesVideo] = []

    # Page 1
    videos, total_aids = _fetch_archives_page(
        page, mid, season_id, page_num=1, page_size=page_size,
        timeout_secs=timeout_secs,
    )
    if not videos:
        return []
    all_videos.extend(videos)

    if len(all_videos) >= total_aids:
        return all_videos

    # Remaining pages
    page_num = 2
    while len(all_videos) < total_aids:
        more, _ = _fetch_archives_page(
            page, mid, season_id, page_num=page_num, page_size=page_size,
            timeout_secs=timeout_secs,
        )
        if not more:
            break
        all_videos.extend(more)
        if len(more) < page_size:
            break
        page_num += 1

    return all_videos
