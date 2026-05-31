from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import qrcode
import requests
from qrcode.image.svg import SvgImage

from videocp.browser import BrowserConfig, BrowserSession, build_cdp_url, find_free_local_port
from videocp.config import WatermarkConfig
from videocp.downloader import download_best_candidate
from videocp.errors import DownloadError
from videocp.models import (
    DownloadArtifact,
    ExtractionResult,
    MediaCandidate,
    MediaKind,
    TrackType,
    VideoMetadata,
    WatermarkMode,
)
from videocp.providers import BILIBILI_VIDEO_ID_RE, extract_id_from_url
from videocp.runtime_log import log_info, log_warn

TV_LOGIN_AUTH_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
TV_LOGIN_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
TV_PLAYURL_API = "https://api.bilibili.com/x/tv/playurl"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
TV_API_SIGN_SECRET = "59b43e04ad6965f34319062b478f83dd"

# 第三方反代回退域名（当官方 API 不可用时使用）
TV_LOGIN_AUTH_URL_FALLBACK = "https://passport.snm0516.aisee.tv/x/passport-tv-login/qrcode/auth_code"
TV_PLAYURL_API_FALLBACK = "https://api.snm0516.aisee.tv/x/tv/playurl"

# Bilibili TV API 鉴权相关错误码
AUTH_ERROR_CODES = {-101, -104, -400, -401, -404}
TV_LOGIN_WAIT_SECS = 300
TV_LOGIN_POLL_INTERVAL_SECS = 1
TV_TOKEN_FILE = "BBDownTV.data"
WEB_COOKIE_FILE = "BilibiliWebCookies.json"

# Bilibili Web API
WEB_PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"
WBI_INDEX_NAV_API = "https://api.bilibili.com/x/web-interface/nav"

# WBI 混肴密钥映射表
_WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]
TV_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; OnePlus7TPro Build/QKQ1.190716.003) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

# Web API 下载 CDN 流时使用桌面浏览器 UA，避免被 CDN 风控
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

BASE_URL_PORT_RE = re.compile(r"http.*:\d+", re.IGNORECASE)
_TV_LOGIN_LOCK = threading.Lock()

QUALITY_PRIORITY = {
    "127": 13,
    "126": 12,
    "125": 11,
    "120": 10,
    "116": 9,
    "112": 8,
    "100": 7,
    "80": 6,
    "74": 5,
    "64": 4,
    "48": 3,
    "32": 2,
    "16": 1,
    "6": 0,
    "5": -1,
}

CODEC_COMPAT_PRIORITY = {
    7: 3,   # AVC / H.264
    12: 2,  # HEVC / H.265
    13: 1,  # AV1
}

_QUALITY_DISPLAY = {
    "127": "8K超高清",
    "126": "4K超清",
    "125": "4K HDR",
    "120": "4K",
    "116": "1080P60帧",
    "112": "1080P高码率",
    "100": "1080P",
    "80": "1080P",
    "74": "720P60帧",
    "64": "720P",
    "48": "720P",
    "32": "480P",
    "16": "360P",
    "6": "240P",
    "5": "144P",
}


def _qn_display_name(qn: str) -> str:
    return _QUALITY_DISPLAY.get(qn, f"未知画质(qn={qn})")


@dataclass(slots=True)
class BilibiliPageInfo:
    aid: str
    bvid: str
    cid: str
    page_index: int
    title: str
    desc: str
    author: str
    duration_secs: int = 0


def bbdown_state_dir(profile_dir: Path) -> Path:
    return profile_dir.parent / "bbdown"


def bbdown_tv_token_path(profile_dir: Path) -> Path:
    return bbdown_state_dir(profile_dir) / TV_TOKEN_FILE


def load_bbdown_tv_token(profile_dir: Path) -> str:
    token_path = bbdown_tv_token_path(profile_dir)
    if not token_path.is_file():
        return ""
    raw = token_path.read_text(encoding="utf-8").strip()
    if raw.startswith("access_token="):
        return raw[len("access_token="):].strip()
    return raw


def save_bbdown_tv_token(profile_dir: Path, token: str) -> Path:
    state_dir = bbdown_state_dir(profile_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = bbdown_tv_token_path(profile_dir)
    token_path.write_text(f"access_token={token.strip()}\n", encoding="utf-8")
    return token_path


def clear_bbdown_tv_token(profile_dir: Path) -> None:
    token_path = bbdown_tv_token_path(profile_dir)
    if token_path.is_file():
        token_path.unlink()


def _is_token_expired_error(exc: DownloadError) -> bool:
    msg = str(exc).lower()
    for code in AUTH_ERROR_CODES:
        if f"code={code}" in msg:
            return True
    for kw in ("access_token", "access_key", "not login", "未登录", "未登入", "token", "expired", "过期", "请先登录", "请求错误"):
        if kw in msg:
            return True
    return False


# ── Bilibili Web API cookie helpers ──────────────────────────────────────────


def web_cookie_path(profile_dir: Path) -> Path:
    return bbdown_state_dir(profile_dir) / WEB_COOKIE_FILE


def load_web_cookies(profile_dir: Path) -> dict[str, str]:
    path = web_cookie_path(profile_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if v}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_web_cookies(profile_dir: Path, cookies: dict[str, str]) -> None:
    state_dir = bbdown_state_dir(profile_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    web_cookie_path(profile_dir).write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_cookies_from_browser(browser_config: BrowserConfig) -> dict[str, str]:
    """打开浏览器访问 B 站以提取 Cookie (SESSDATA 等)。
    使用非 headless 模式并复用已有 profile，以便利用种子数据的登录态。
    如果 profile 中没有 B 站登录态，会提示用户手动登录。"""
    from videocp.profile import prepare_profile_seed_once
    prepare_profile_seed_once(browser_config.profile_dir, browser_config.browser_path)

    cookie_config = BrowserConfig(
        profile_dir=browser_config.profile_dir,
        browser_path=browser_config.browser_path,
        headless=False,
    )
    log_info("bbdown.cookie.extract.start", profile_dir=str(browser_config.profile_dir))

    def _read_cookies(context) -> dict[str, str]:
        result: dict[str, str] = {}
        for c in context.cookies():
            if c.get("name") and c.get("value"):
                result[str(c["name"])] = str(c["value"])
        return result

    MAX_WAIT_LOGIN_SECS = 120
    with BrowserSession(cookie_config, prepare_seed=False, terminate_on_close=True) as session:
        if session.context is None:
            log_warn("bbdown.cookie.extract.no_context")
            return {}

        page = session.new_page()
        try:
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)

            # 首次检查是否已有登录态
            cookies = _read_cookies(session.context)
            if "SESSDATA" in cookies:
                log_info("bbdown.cookie.extract.already_logged_in")
            else:
                # 显示登录提示，等待用户手动登录
                _show_cookie_login_overlay(page, "请在浏览器中登录哔哩哔哩 (扫码或账号密码)，登录后自动继续...")
                log_info("bbdown.cookie.extract.waiting_login", timeout_secs=MAX_WAIT_LOGIN_SECS)
                deadline = time.monotonic() + MAX_WAIT_LOGIN_SECS
                while time.monotonic() < deadline:
                    time.sleep(1)
                    cookies = _read_cookies(session.context)
                    if "SESSDATA" in cookies:
                        _show_cookie_login_overlay(page, "登录成功！正在继续下载...")
                        page.wait_for_timeout(1_500)
                        break
                else:
                    log_warn("bbdown.cookie.extract.login_timeout")
        finally:
            page.close()

    has_login = "SESSDATA" in cookies or "bili_jct" in cookies
    log_info(
        "bbdown.cookie.extract.result",
        cookie_count=len(cookies),
        has_sessdata="SESSDATA" in cookies,
        has_bili_jct="bili_jct" in cookies,
        effective=has_login,
    )
    return cookies


def _show_cookie_login_overlay(page, message: str) -> None:
    """在页面上显示登录提示覆盖层。"""
    try:
        page.evaluate(
            """([msg]) => {
                let overlay = document.getElementById('vc-cookie-overlay');
                if (!overlay) {
                    overlay = document.createElement('div');
                    overlay.id = 'vc-cookie-overlay';
                    overlay.style.cssText = [
                        'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999',
                        'display:flex;align-items:center;justify-content:center',
                        'background:rgba(0,0,0,0.55);pointer-events:none',
                    ].join(';');
                    const box = document.createElement('div');
                    box.style.cssText = [
                        'padding:32px 48px;border-radius:20px',
                        'background:rgba(255,255,255,0.95);backdrop-filter:blur(10px)',
                        'box-shadow:0 20px 60px rgba(0,0,0,0.2)',
                        'font-size:20px;font-weight:600;color:#1c2430;text-align:center',
                        'font-family:"PingFang SC","Microsoft YaHei",sans-serif',
                    ].join(';');
                    box.textContent = msg;
                    overlay.appendChild(box);
                    document.body.appendChild(overlay);
                } else {
                    overlay.querySelector('div').textContent = msg;
                }
            }""",
            [message],
        )
    except Exception:
        pass


def _cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _web_cookies_to_cookie_list(cookies: dict[str, str]) -> list[dict[str, str]]:
    """将 web cookie dict 转为下载器需要的 cookie list 格式。"""
    result: list[dict[str, str]] = []
    for name, value in cookies.items():
        if not value:
            continue
        domain = ".bilibili.com"
        # SESSDATA 需要 secure 标记
        path = "/"
        result.append({"name": name, "value": value, "domain": domain, "path": path})
    return result


# ── WBI signing ────────────────────────────────────────────────────────────


def _fetch_wbi_mixin_key(cookies: dict[str, str] | None, timeout_secs: int) -> tuple[str, str]:
    """获取 WBI 签名所需的 img_key 和 sub_key。"""
    headers = {"User-Agent": TV_API_HEADERS["User-Agent"], "Referer": "https://www.bilibili.com/"}
    if cookies:
        headers["Cookie"] = _cookies_to_header(cookies)
    response = requests.get(WBI_INDEX_NAV_API, headers=headers, timeout=timeout_secs)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img") or {}

    # B 站 nav API 可能返回 img_url/sub_url 或 img_key/sub_key
    if isinstance(wbi_img, dict):
        img_raw = wbi_img.get("img_key") or wbi_img.get("img_url") or ""
        sub_raw = wbi_img.get("sub_key") or wbi_img.get("sub_url") or ""
    else:
        img_raw = ""
        sub_raw = ""

    img_key = str(img_raw).rsplit("/", 1)[-1].split(".")[0] if img_raw else ""
    sub_key = str(sub_raw).rsplit("/", 1)[-1].split(".")[0] if sub_raw else ""

    if not img_key or not sub_key:
        raise DownloadError(
            f"Failed to fetch WBI mixin key from nav API: "
            f"code={payload.get('code')} wbi_img_keys={list(wbi_img.keys()) if isinstance(wbi_img, dict) else 'none'}"
        )
    return img_key, sub_key


def _wbi_mixin(raw: str) -> str:
    """WBI 混肴：从 raw 字符串中按映射表提取字符。"""
    return "".join(raw[pos] for pos in _WBI_MIXIN_KEY_ENC_TAB if pos < len(raw))[:32]


def _wbi_sign_params(params: dict[str, str], mixin_key: str) -> dict[str, str]:
    """对参数进行 WBI 签名，添加 wts 和 w_rid。"""
    signed = dict(params)
    signed["wts"] = _timestamp_secs()
    sorted_keys = sorted(signed.keys())
    query_string = "&".join(f"{k}={signed[k]}" for k in sorted_keys)
    signed["w_rid"] = hashlib.md5((query_string + mixin_key).encode("utf-8")).hexdigest()
    return signed

# ── Web API playurl fetching ────────────────────────────────────────────────


def fetch_bilibili_web_candidates(
    page_info: BilibiliPageInfo,
    cookies: dict[str, str],
    timeout_secs: int,
) -> list[MediaCandidate]:
    """通过 B 站 Web API (wbi/playurl) 获取候选媒体流。支持大会员 4K/8K。"""
    img_key, sub_key = _fetch_wbi_mixin_key(cookies, timeout_secs)
    mixin_key = _wbi_mixin(img_key + sub_key)

    params: dict[str, str] = {}
    params["avid"] = page_info.aid
    params["bvid"] = page_info.bvid
    params["cid"] = page_info.cid
    params["fnval"] = "4048"
    params["fnver"] = "0"
    params["fourk"] = "1"
    params["qn"] = "0"
    params["platform"] = "web"
    params = _wbi_sign_params(params, mixin_key)

    cookie_header = _cookies_to_header(cookies)
    headers = dict(TV_API_HEADERS)
    headers["Cookie"] = cookie_header

    log_info(
        "bbdown.web_api.request",
        endpoint=WEB_PLAYURL_API,
        aid=page_info.aid,
        bvid=page_info.bvid,
        cid=page_info.cid,
        cookies_present="SESSDATA" in cookies,
    )
    response = requests.get(WEB_PLAYURL_API, params=params, headers=headers, timeout=timeout_secs)
    response.raise_for_status()
    payload = response.json()
    code = int(payload.get("code", 0))
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or "unknown error")
        raise DownloadError(f"Bilibili Web playurl request failed (code={code}): {message}")

    data = payload.get("data") or payload.get("result") or {}
    dash = data.get("dash")
    if not isinstance(dash, dict):
        raise DownloadError("Bilibili Web API returned no DASH data.")

    videos = dash.get("video") or []
    audios = dash.get("audio") or []

    # 记录 Web API 返回的可用画质
    available_qn = sorted(
        {str(stream.get("id") or "") for stream in videos},
        key=lambda q: QUALITY_PRIORITY.get(q, -1),
        reverse=True,
    )
    quality_hints = {q: _qn_display_name(q) for q in available_qn}
    log_info(
        "bbdown.web_api.quality",
        available_qn=available_qn,
        highest_qn=available_qn[0] if available_qn else "none",
        quality_hints=quality_hints,
        stream_count=len(videos),
    )

    candidates: list[MediaCandidate] = []
    # 按画质优先级排序视频流
    for stream in sorted(videos, key=_video_stream_sort_key, reverse=True):
        primary_url = _pick_primary_url(stream)
        if not primary_url:
            continue
        quality_id = str(stream.get("id") or "")
        bandwidth = int(stream.get("bandwidth") or 0)
        codecs = str(stream.get("codecs") or "").strip()
        codecid = int(stream.get("codecid") or 0)
        candidates.append(
            MediaCandidate(
                url=primary_url,
                kind=MediaKind.MP4,
                track_type=TrackType.VIDEO_ONLY,
                watermark_mode=WatermarkMode.NO_WATERMARK,
                source="web_api",
                observed_via="api",
                note=f"qn={quality_id};codecid={codecid};codecs={codecs};bandwidth={bandwidth}",
            )
        )
    # 添加最佳音频流
    best_audio = _pick_best_audio_stream(audios)
    if best_audio is not None:
        audio_url = _pick_primary_url(best_audio)
        if audio_url:
            candidates.append(
                MediaCandidate(
                    url=audio_url,
                    kind=MediaKind.MP4,
                    track_type=TrackType.AUDIO_ONLY,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="web_api",
                    observed_via="api",
                    note=f"id={best_audio.get('id')};bandwidth={best_audio.get('bandwidth')}",
                )
            )
    return candidates


def ensure_bbdown_tv_token(browser_config: BrowserConfig, timeout_secs: int) -> str:
    token = load_bbdown_tv_token(browser_config.profile_dir)
    if token:
        return token
    with _TV_LOGIN_LOCK:
        token = load_bbdown_tv_token(browser_config.profile_dir)
        if token:
            return token
        token = _login_tv_in_browser(browser_config.browser_path, timeout_secs=max(timeout_secs, TV_LOGIN_WAIT_SECS))
        save_bbdown_tv_token(browser_config.profile_dir, token)
        return token


def infer_bbdown_select_page(url: str) -> str:
    page_values = parse_qs(urlparse(url).query).get("p")
    if page_values and page_values[0].strip():
        return page_values[0].strip()
    return "1"


def download_bilibili_with_bbdown(
    *,
    source_url: str,
    browser_config: BrowserConfig,
    output_dir: Path,
    timeout_secs: int,
    watermark: WatermarkConfig | None = None,
    metadata_seed: VideoMetadata | None = None,
    author_hint: str = "",
    max_video_duration_secs: int = 0,
    bilibili_download_mode: str = "tv",
) -> tuple[ExtractionResult, DownloadArtifact]:
    page_info = fetch_bilibili_page_info(source_url, timeout_secs=timeout_secs, author_hint=author_hint)
    metadata = build_bbdown_metadata(
        source_url,
        page_info=page_info,
        metadata_seed=metadata_seed,
        author_hint=author_hint,
    )

    candidates: list[MediaCandidate] = []
    download_mode = "tv_api"
    current_web_cookies: dict[str, str] = {}

    # ── web 模式：优先尝试 Web API + Cookie（可获取 4K/8K） ──
    if bilibili_download_mode == "web":
        # 策略 1：尝试缓存 Cookie
        web_cookies = load_web_cookies(browser_config.profile_dir)
        if web_cookies and "SESSDATA" in web_cookies:
            try:
                candidates = fetch_bilibili_web_candidates(page_info, cookies=web_cookies, timeout_secs=timeout_secs)
                if candidates:
                    download_mode = "web_api"
                    current_web_cookies = web_cookies
                    log_info("bbdown.mode.selected", mode="web_api", reason="cookie_present", candidate_count=len(candidates))
            except DownloadError as exc:
                log_warn("bbdown.web_api.failed", error=str(exc), action="falling_back_to_tv_api")
            except Exception as exc:
                log_warn("bbdown.web_api.unexpected", error=str(exc), action="falling_back_to_tv_api")
        else:
            log_info("bbdown.web_api.no_cached_cookies", cached=bool(web_cookies), action="trying_browser_extraction")

        # 策略 2：Web API 不可用时尝试从浏览器获取新 Cookie
        if not candidates:
            try:
                fresh_cookies = _extract_cookies_from_browser(browser_config)
                if fresh_cookies and "SESSDATA" in fresh_cookies:
                    save_web_cookies(browser_config.profile_dir, fresh_cookies)
                    candidates = fetch_bilibili_web_candidates(page_info, cookies=fresh_cookies, timeout_secs=timeout_secs)
                    if candidates:
                        download_mode = "web_api"
                        current_web_cookies = fresh_cookies
                        log_info("bbdown.mode.selected", mode="web_api", reason="fresh_browser_cookies", candidate_count=len(candidates))
                else:
                    log_info("bbdown.web_api.no_browser_cookies", cookie_count=len(fresh_cookies), has_sessdata="SESSDATA" in fresh_cookies, action="falling_back_to_tv_api")
            except DownloadError as exc:
                log_warn("bbdown.web_api.fresh_failed", error=str(exc), action="falling_back_to_tv_api")
            except Exception as exc:
                log_warn("bbdown.web_api.fresh_unexpected", error=str(exc), action="falling_back_to_tv_api")

    # ── 策略 3：回退到 TV API ──
    if not candidates:
        token = ensure_bbdown_tv_token(browser_config, timeout_secs=timeout_secs)
        max_token_retries = 1
        for token_attempt in range(max_token_retries + 1):
            try:
                candidates = fetch_bilibili_tv_candidates(page_info, token=token, timeout_secs=timeout_secs)
                break
            except DownloadError as exc:
                if token_attempt >= max_token_retries or not _is_token_expired_error(exc):
                    raise
                log_warn(
                    "bbdown.token.expired",
                    attempt=f"{token_attempt + 1}/{max_token_retries + 1}",
                    error=str(exc),
                    action="clearing_cache",
                )
                clear_bbdown_tv_token(browser_config.profile_dir)
                token = ensure_bbdown_tv_token(browser_config, timeout_secs=timeout_secs)

    if not candidates:
        raise DownloadError("Bilibili API returned no playable candidates (web + tv both exhausted).")

    # Web API 模式：传递 Cookie + 桌面浏览器 UA 以通过 CDN 验证
    if download_mode == "web_api" and current_web_cookies:
        extraction_cookies = _web_cookies_to_cookie_list(current_web_cookies)
        extraction_user_agent = WEB_USER_AGENT
    else:
        extraction_cookies = []
        extraction_user_agent = TV_API_HEADERS["User-Agent"]

    extraction = ExtractionResult(
        metadata=metadata,
        candidates=candidates,
        cookies=extraction_cookies,
        user_agent=extraction_user_agent,
        diagnostics={
            "downloader": "bbdown_python",
            "mode": download_mode,
            "aid": page_info.aid,
            "bvid": page_info.bvid,
            "cid": page_info.cid,
            "page_index": page_info.page_index,
        },
    )
    if max_video_duration_secs > 0 and metadata.duration_ms > max_video_duration_secs * 1000:
        duration_secs = metadata.duration_ms / 1000
        log_info(
            "download.skip_duration",
            site=metadata.site,
            content_id=metadata.content_id or "unknown",
            duration_secs=f"{duration_secs:.1f}",
            max_video_duration_secs=max_video_duration_secs,
        )
        raise DownloadError(
            "video duration exceeds limit: "
            f"duration_secs={duration_secs:.1f} max_video_duration_secs={max_video_duration_secs}"
        )
    artifact = download_best_candidate(
        extraction,
        output_dir=output_dir,
        timeout_secs=timeout_secs,
        watermark=watermark,
    )
    return extraction, artifact


def fetch_bilibili_page_info(source_url: str, timeout_secs: int, author_hint: str = "") -> BilibiliPageInfo:
    video_id = _extract_video_id(source_url)
    params = {"bvid": video_id} if video_id.upper().startswith("BV") else {"aid": video_id.removeprefix("av").removeprefix("AV")}
    response = requests.get(BILIBILI_VIEW_API, params=params, headers=TV_API_HEADERS, timeout=timeout_secs)
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code", 0)) != 0:
        message = str(payload.get("message") or payload.get("msg") or "unknown error")
        raise DownloadError(f"Failed to fetch Bilibili video info: {message}")
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    selected_page = _select_bilibili_page(source_url, pages, str(data.get("cid") or ""))
    return BilibiliPageInfo(
        aid=str(data.get("aid") or ""),
        bvid=str(data.get("bvid") or video_id),
        cid=str(selected_page.get("cid") or data.get("cid") or ""),
        page_index=int(selected_page.get("page") or 1),
        title=str(data.get("title") or "").strip(),
        desc=str(data.get("desc") or "").strip(),
        author=str((data.get("owner") or {}).get("name") or author_hint).strip(),
        duration_secs=int(data.get("duration") or 0),
    )


def build_bbdown_metadata(
    source_url: str,
    *,
    page_info: BilibiliPageInfo,
    metadata_seed: VideoMetadata | None = None,
    author_hint: str = "",
) -> VideoMetadata:
    metadata = VideoMetadata(
        source_url=source_url,
        site="bilibili",
        canonical_url=metadata_seed.canonical_url if metadata_seed and metadata_seed.canonical_url else source_url,
        page_url=metadata_seed.page_url if metadata_seed and metadata_seed.page_url else source_url,
        aweme_id=page_info.bvid or page_info.aid,
        author=page_info.author or (metadata_seed.author if metadata_seed else "") or author_hint,
        desc=page_info.desc or (metadata_seed.desc if metadata_seed else ""),
        title=page_info.title or (metadata_seed.title if metadata_seed else ""),
        duration_ms=page_info.duration_secs * 1000 if page_info.duration_secs > 0 else 0,
    )
    if author_hint and not metadata.author:
        metadata.author = author_hint
    if metadata_seed is not None:
        if metadata_seed.title and not metadata.title:
            metadata.title = metadata_seed.title
        if metadata_seed.desc and not metadata.desc:
            metadata.desc = metadata_seed.desc
    return metadata


def fetch_bilibili_tv_candidates(page_info: BilibiliPageInfo, token: str, timeout_secs: int) -> list[MediaCandidate]:
    play_payload = _fetch_bilibili_tv_playinfo(page_info, token=token, timeout_secs=timeout_secs)
    root = _resolve_playinfo_root(play_payload)
    dash = root.get("dash")
    if isinstance(dash, dict):
        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        # 记录 TV API 返回的可用画质（方便观察大会员是否提升画质）
        available_qn = sorted(
            {str(stream.get("id") or "") for stream in videos},
            key=lambda q: QUALITY_PRIORITY.get(q, -1),
            reverse=True,
        )
        quality_hints = {q: _qn_display_name(q) for q in available_qn}
        log_info(
            "bbdown.tv_api.quality",
            available_qn=available_qn,
            highest_qn=available_qn[0] if available_qn else "none",
            quality_hints=quality_hints,
            stream_count=len(videos),
        )

        candidates: list[MediaCandidate] = []
        for stream in sorted(videos, key=_video_stream_sort_key, reverse=True):
            primary_url = _pick_primary_url(stream)
            if not primary_url:
                continue
            quality_id = str(stream.get("id") or "")
            bandwidth = int(stream.get("bandwidth") or 0)
            codecs = str(stream.get("codecs") or "").strip()
            codecid = int(stream.get("codecid") or 0)
            candidates.append(
                MediaCandidate(
                    url=primary_url,
                    kind=MediaKind.MP4,
                    track_type=TrackType.VIDEO_ONLY,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="tv_api",
                    observed_via="api",
                    note=f"qn={quality_id};codecid={codecid};codecs={codecs};bandwidth={bandwidth}",
                )
            )
        best_audio = _pick_best_audio_stream(audios)
        if best_audio is not None:
            audio_url = _pick_primary_url(best_audio)
            if audio_url:
                candidates.append(
                    MediaCandidate(
                        url=audio_url,
                        kind=MediaKind.MP4,
                        track_type=TrackType.AUDIO_ONLY,
                        watermark_mode=WatermarkMode.NO_WATERMARK,
                        source="tv_api",
                        observed_via="api",
                        note=f"id={best_audio.get('id')};bandwidth={best_audio.get('bandwidth')}",
                    )
                )
        return candidates

    durl = root.get("durl") or []
    if isinstance(durl, list) and durl:
        if len(durl) > 1:
            raise DownloadError("Bilibili TV API returned multi-clip FLV data, which is not supported yet.")
        url = str((durl[0] or {}).get("url") or "").strip()
        if url:
            return [
                MediaCandidate(
                    url=url,
                    kind=MediaKind.MP4,
                    track_type=TrackType.MUXED,
                    watermark_mode=WatermarkMode.NO_WATERMARK,
                    source="tv_api",
                    observed_via="api",
                    note="durl",
                )
            ]
    return []


def _fetch_bilibili_tv_playinfo(page_info: BilibiliPageInfo, token: str, timeout_secs: int) -> dict:
    params: dict[str, str] = {}
    if token:
        params["access_key"] = token
    params["appkey"] = "4409e2ce8ffd12b8"
    params["build"] = "106500"
    params["cid"] = page_info.cid
    params["device"] = "android"
    params["fnval"] = "4048"
    params["fnver"] = "0"
    params["fourk"] = "1"
    params["mid"] = "0"
    params["mobi_app"] = "android_tv_yst"
    params["object_id"] = page_info.aid
    params["platform"] = "android"
    params["playurl_type"] = "1"
    params["qn"] = "0"
    params["ts"] = _timestamp_secs()
    unsigned_query = urlencode(params)

    # 优先使用官方域名，失败则回退到第三方反代
    api_endpoints = [TV_PLAYURL_API, TV_PLAYURL_API_FALLBACK]
    last_error: DownloadError | None = None

    for endpoint in api_endpoints:
        url = f"{endpoint}?{unsigned_query}&sign={_sign_query(unsigned_query)}"
        log_info(
            "bbdown.tv_api.request",
            endpoint=endpoint,
            aid=page_info.aid,
            cid=page_info.cid,
            page=page_info.page_index,
            access_token="present" if token else "missing",
        )
        try:
            response = requests.get(url, headers=TV_API_HEADERS, timeout=timeout_secs)
            response.raise_for_status()
            payload = response.json()
            code = int(payload.get("code", 0))
            if code != 0:
                message = str(payload.get("message") or payload.get("msg") or "unknown error")
                raise DownloadError(f"Bilibili TV playurl request failed (code={code}): {message}")
            return payload
        except DownloadError as exc:
            last_error = exc
            # 鉴权错误不重试其他域名（token 问题换域名也没用）
            if _is_token_expired_error(exc):
                raise
            log_warn(
                "bbdown.tv_api.endpoint_failed",
                endpoint=endpoint,
                error=str(exc),
                action="trying_fallback" if endpoint != api_endpoints[-1] else "all_failed",
            )
        except requests.RequestException as exc:
            last_error = DownloadError(f"Request failed: {exc}")
            log_warn(
                "bbdown.tv_api.network_error",
                endpoint=endpoint,
                error=str(exc),
                action="trying_fallback" if endpoint != api_endpoints[-1] else "all_failed",
            )

    if last_error is not None:
        raise last_error
    raise DownloadError("Bilibili TV playurl request failed: all endpoints exhausted")


def _resolve_playinfo_root(payload: dict) -> dict:
    root = payload.get("result") or payload.get("data") or payload
    if isinstance(root, dict) and isinstance(root.get("video_info"), dict):
        return root["video_info"]
    return root if isinstance(root, dict) else {}


def _video_stream_sort_key(stream: dict) -> tuple[int, int]:
    quality_id = str(stream.get("id") or "")
    quality_rank = QUALITY_PRIORITY.get(quality_id, int(quality_id or 0))
    codec_rank = _codec_compat_rank(stream)
    bandwidth = int(stream.get("bandwidth") or 0)
    return (quality_rank, codec_rank, bandwidth)


def _codec_compat_rank(stream: dict) -> int:
    codecid = int(stream.get("codecid") or 0)
    if codecid in CODEC_COMPAT_PRIORITY:
        return CODEC_COMPAT_PRIORITY[codecid]
    codecs = str(stream.get("codecs") or "").lower()
    if codecs.startswith("avc1"):
        return CODEC_COMPAT_PRIORITY[7]
    if codecs.startswith("hev1") or codecs.startswith("hvc1"):
        return CODEC_COMPAT_PRIORITY[12]
    if codecs.startswith("av01"):
        return CODEC_COMPAT_PRIORITY[13]
    return 0


def _pick_best_audio_stream(streams: list[dict]) -> dict | None:
    if not isinstance(streams, list) or not streams:
        return None
    return max(streams, key=lambda stream: (int(stream.get("bandwidth") or 0), int(stream.get("id") or 0)))


def _pick_primary_url(stream: dict) -> str:
    urls: list[str] = []
    for key in ("base_url", "baseUrl"):
        value = str(stream.get(key) or "").strip()
        if value:
            urls.append(value)
    backup = stream.get("backup_url") or stream.get("backupUrl") or []
    if isinstance(backup, list):
        urls.extend(str(item).strip() for item in backup if str(item).strip())
    if not urls:
        return ""
    preferred = next((item for item in urls if not BASE_URL_PORT_RE.match(item)), "")
    return preferred or urls[0]


def _select_bilibili_page(source_url: str, pages: list[dict], default_cid: str) -> dict:
    page_param = infer_bbdown_select_page(source_url)
    try:
        page_index = int(page_param)
    except ValueError:
        page_index = 1
    if isinstance(pages, list):
        for page in pages:
            if int(page.get("page") or 0) == page_index:
                return page
        for page in pages:
            if str(page.get("cid") or "") == default_cid:
                return page
        if pages:
            return pages[0]
    return {"page": page_index, "cid": default_cid}


def _extract_video_id(url: str) -> str:
    bvid = extract_id_from_url(url, (BILIBILI_VIDEO_ID_RE,))
    if bvid:
        return bvid
    aid_match = re.search(r"/video/av(\d+)", url, re.IGNORECASE)
    if aid_match:
        return aid_match.group(1)
    raise DownloadError(f"Unsupported Bilibili video URL: {url}")


def _login_tv_in_browser(browser_path: str, timeout_secs: int) -> str:
    payload = _build_tv_login_payload()

    # 优先官方域名，失败回退第三方反代
    last_error: DownloadError | None = None
    for auth_endpoint in (TV_LOGIN_AUTH_URL, TV_LOGIN_AUTH_URL_FALLBACK):
        try:
            response = requests.post(auth_endpoint, data=payload, headers=TV_API_HEADERS, timeout=15)
            response.raise_for_status()
            data = _parse_tv_login_response(response.text)
            break
        except (requests.RequestException, DownloadError) as exc:
            last_error = DownloadError(f"TV login auth request to {auth_endpoint} failed: {exc}")
            log_warn(
                "bbdown.login.auth_endpoint_failed",
                endpoint=auth_endpoint,
                error=str(exc),
                action="trying_fallback" if auth_endpoint != TV_LOGIN_AUTH_URL_FALLBACK else "all_failed",
            )
    else:
        raise last_error or DownloadError("Bilibili TV login auth request failed: all endpoints exhausted")

    login_url = data["url"]
    auth_code = data["auth_code"]
    payload["auth_code"] = auth_code
    payload["ts"] = _timestamp_secs()
    payload.pop("sign", None)
    payload["sign"] = _sign_query(urlencode(payload))

    with tempfile.TemporaryDirectory(prefix="videocp-bbdown-login-browser-") as temp_profile_raw:
        login_config = BrowserConfig(
            profile_dir=Path(temp_profile_raw),
            browser_path=browser_path,
            cdp_url=build_cdp_url(find_free_local_port()),
            headless=False,
        )
        with BrowserSession(login_config, prepare_seed=False, terminate_on_close=True) as browser:
            page = browser.new_page()
            page.set_viewport_size({"width": 760, "height": 920})
            _render_login_page(page, login_url)
            last_status = ""
            deadline = time.monotonic() + timeout_secs
            while time.monotonic() < deadline:
                time.sleep(TV_LOGIN_POLL_INTERVAL_SECS)
                poll_response = requests.post(TV_LOGIN_POLL_URL, data=payload, headers=TV_API_HEADERS, timeout=15)
                poll_response.raise_for_status()
                poll_payload = poll_response.json()
                code = str(poll_payload.get("code", ""))
                if code == "86039":
                    if last_status != code:
                        _set_login_status(page, "等待扫码", "请使用哔哩哔哩 App 扫描二维码。")
                        last_status = code
                    continue
                if code == "86038":
                    _set_login_status(page, "二维码已过期", "请重新发起登录。")
                    raise DownloadError("Bilibili TV login QR code expired. Retry the download to generate a new code.")
                access_token = str(poll_payload.get("data", {}).get("access_token", "")).strip()
                if access_token:
                    _set_login_status(page, "扫码成功", "已获取 TV access_token，正在继续下载。")
                    page.wait_for_timeout(1500)
                    return access_token
                message = str(poll_payload.get("message") or poll_payload.get("msg") or "unknown login status")
                log_warn("bbdown.login.poll.unexpected", code=code or "none", message=message)
            _set_login_status(page, "登录超时", "等待扫码超时，请重试。")
            raise DownloadError("Timed out waiting for Bilibili TV QR-code login.")


def _build_tv_login_payload() -> dict[str, str]:
    now = datetime.now()
    device_id = _random_string(20)
    buvid = _random_string(37)
    millis = int(now.microsecond / 1000)
    fingerprint = f"{now:%Y%m%d%H%M%S}{millis:03d}{_random_string(45)}"
    payload = {
        "appkey": "4409e2ce8ffd12b8",
        "auth_code": "",
        "bili_local_id": device_id,
        "build": "102801",
        "buvid": buvid,
        "channel": "master",
        "device": "OnePlus",
        "device_id": device_id,
        "device_name": "OnePlus7TPro",
        "device_platform": "Android10OnePlusHD1910",
        "fingerprint": fingerprint,
        "guid": buvid,
        "local_fingerprint": fingerprint,
        "local_id": buvid,
        "mobi_app": "android_tv_yst",
        "networkstate": "wifi",
        "platform": "android",
        "sys_ver": "29",
        "ts": _timestamp_secs(),
    }
    payload["sign"] = _sign_query(urlencode(payload))
    return payload


def _parse_tv_login_response(body: str) -> dict[str, str]:
    payload = json.loads(body)
    data = payload.get("data", {})
    login_url = str(data.get("url", "")).strip()
    auth_code = str(data.get("auth_code", "")).strip()
    if not login_url or not auth_code:
        message = str(payload.get("message") or payload.get("msg") or "missing tv login payload")
        raise DownloadError(f"Failed to create Bilibili TV login QR code: {message}")
    return {"url": login_url, "auth_code": auth_code}


def _sign_query(query: str) -> str:
    return hashlib.md5(f"{query}{TV_API_SIGN_SECRET}".encode("utf-8")).hexdigest()


def _timestamp_secs() -> str:
    return str(int(time.time()))


def _random_string(length: int) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def _render_login_page(page, login_url: str) -> None:
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(login_url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    escaped_url = html.escape(login_url)
    page.set_content(
        f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>videocp Bilibili TV Login</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: "SF Pro Display", "PingFang SC", "Helvetica Neue", sans-serif;
        background:
          radial-gradient(circle at top left, #ffe6b6, transparent 34%),
          radial-gradient(circle at top right, #c3efff, transparent 32%),
          linear-gradient(180deg, #f7f3e9, #fffaf2 42%, #f2f7ff 100%);
      }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        color: #1c2430;
      }}
      main {{
        width: min(92vw, 760px);
        padding: 32px;
        border-radius: 28px;
        background: rgba(255, 255, 255, 0.88);
        box-shadow: 0 24px 80px rgba(28, 36, 48, 0.12);
        backdrop-filter: blur(14px);
      }}
      h1 {{
        margin: 0 0 10px;
        font-size: 34px;
        line-height: 1.08;
      }}
      p {{
        margin: 0;
        color: #516174;
        line-height: 1.6;
      }}
      .layout {{
        margin-top: 28px;
        display: grid;
        grid-template-columns: minmax(260px, 320px) 1fr;
        gap: 28px;
        align-items: center;
      }}
      .qr {{
        padding: 18px;
        border-radius: 24px;
        background: linear-gradient(180deg, #ffffff, #f6f7fb);
        box-shadow: inset 0 0 0 1px rgba(28, 36, 48, 0.06);
      }}
      .qr img {{
        width: 100%;
        height: auto;
        display: block;
      }}
      .status {{
        margin-top: 18px;
        padding: 14px 16px;
        border-radius: 18px;
        background: #fff4d6;
        color: #8a5710;
        font-weight: 600;
      }}
      .hint {{
        margin-top: 10px;
        font-size: 14px;
      }}
      code {{
        display: block;
        margin-top: 16px;
        padding: 12px 14px;
        border-radius: 16px;
        background: #0f1720;
        color: #eef4ff;
        word-break: break-all;
        font-size: 12px;
      }}
      @media (max-width: 720px) {{
        main {{
          width: calc(100vw - 32px);
          padding: 20px;
        }}
        .layout {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      <p>videocp 正在为 B 站下载获取 TV token</p>
      <h1>请用哔哩哔哩 App 扫码</h1>
      <p>扫码并确认后，本窗口会自动继续下载。token 会缓存到本地，后续下载不需要重复扫码。</p>
      <div class="layout">
        <div class="qr"><img alt="Bilibili TV Login QR Code" src="{qr_data_url}" /></div>
        <div>
          <div id="status" class="status">等待扫码</div>
          <p id="hint" class="hint">如果二维码失效，重新发起下载即可生成新的二维码。</p>
          <code>{escaped_url}</code>
        </div>
      </div>
    </main>
  </body>
</html>
        """,
        wait_until="load",
    )


def _set_login_status(page, status: str, hint: str) -> None:
    try:
        page.evaluate(
            """([status, hint]) => {
                const statusNode = document.getElementById("status");
                const hintNode = document.getElementById("hint");
                if (statusNode) statusNode.textContent = status;
                if (hintNode) hintNode.textContent = hint;
            }""",
            [status, hint],
        )
    except Exception:
        pass
