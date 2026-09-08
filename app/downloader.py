"""Instagram / YouTube / TikTok va boshqa havolalardan video yuklab olish (yt-dlp)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from .config import (
    COOKIES_FILE,
    COOKIES_FROM_BROWSER,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    MAX_DURATION_MIN,
    MAX_FILESIZE_MB,
    MAX_VIDEO_HEIGHT,
)
from .ffmpeg_setup import ffmpeg_dir
from .security import SecurityError, validate_url

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class _YdlLogger:
    """yt-dlp xabarlarini konsolga emas, bot log'iga yo'naltiradi."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)


class DownloadError(RuntimeError):
    """Foydalanuvchiga ko'rsatish mumkin bo'lgan xato."""


class _Abort(Exception):
    """Yuklashni to'xtatish (vaqt tugadi yoki video juda uzun)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Video:
    path: Path
    work_dir: Path
    title: str
    uploader: str
    duration: float
    width: int
    height: int
    webpage_url: str
    thumbnail: str | None

    @property
    def size_mb(self) -> float:
        try:
            return self.path.stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,;\u201d\"'")


def _cookie_plan() -> list[tuple[str, str | None]]:
    """Qaysi tartibda cookie'lar bilan urinib ko'rish kerakligini aniqlaydi.

    ("file", None)        - loyihadagi cookies.txt
    ("browser", "chrome") - brauzerdan olingan cookie'lar
    ("none", None)        - cookie'siz (odatiy holat)
    """
    if COOKIES_FILE.exists():
        return [("file", None), ("none", None)]
    choice = COOKIES_FROM_BROWSER.lower()
    if choice == "auto":
        return [("none", None), ("browser", "chrome"), ("browser", "edge"),
                ("browser", "firefox")]
    if choice:
        return [("browser", choice), ("none", None)]
    return [("none", None)]


def _needs_cookies(message: str) -> bool:
    low = message.lower()
    return any(
        marker in low
        for marker in ("login", "log in", "cookies", "empty media response",
                       "rate-limit", "429", "sign in", "age-restricted",
                       "not a bot", "account")
    )


def _ydl_opts(work_dir: Path, cookies: tuple[str, str | None] = ("none", None)) -> dict:
    height = MAX_VIDEO_HEIGHT
    opts: dict = {
        "outtmpl": str(work_dir / "video.%(ext)s"),
        "format": (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={height}][ext=mp4]/"
            f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
        "restrictfilenames": True,
        "overwrites": True,
        "ffmpeg_location": ffmpeg_dir(),
        "http_headers": {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        "logger": _YdlLogger(),
    }
    kind, value = cookies
    if kind == "file":
        opts["cookiefile"] = str(COOKIES_FILE)
    elif kind == "browser" and value:
        opts["cookiesfrombrowser"] = (value,)
    return opts


def _friendly_error(message: str) -> str:
    low = message.lower()
    if "private" in low or "unavailable" in low or "has been removed" in low:
        return "Bu post yopiq (private) yoki o'chirilgan — video yuklab bo'lmadi."
    if _needs_cookies(low):
        return (
            "Sayt bu videoni ko'rsatish uchun akkauntga kirishni (cookies) talab qilyapti.\n"
            "Yechim: .env faylida COOKIES_FROM_BROWSER=auto deb yozib, botni qayta "
            "ishga tushiring (brauzeringizda o'sha saytga kirgan bo'lishingiz kerak).\n"
            "Agar post yopiq bo'lsa, ochiq havola yuboring."
        )
    if "max-filesize" in low or "larger than" in low:
        return (
            f"Video hajmi juda katta ({MAX_FILESIZE_MB} MB dan ortiq). "
            "Qisqaroq video yuboring."
        )
    if "404" in low or "not found" in low or "no video" in low:
        return (
            "Bu havolada video topilmadi. Reels/Shorts/TikTok video havolasini "
            "to'g'ridan-to'g'ri yuboring."
        )
    if "unsupported url" in low or "no suitable" in low or "unable to extract" in low:
        return "Bu havolani tanib bo'lmadi. Instagram, YouTube yoki TikTok havolasini yuboring."
    if "timed out" in low or "timeout" in low:
        return "Internet javob bermadi (timeout). Yana bir marta urinib ko'ring."
    if "geo" in low and "block" in low:
        return "Bu video sizning mintaqangizda bloklangan."
    return "Videoni yuklab bo'lmadi. Havolani tekshirib, qaytadan urinib ko'ring."


def _download_sync(url: str, work_dir: Path, cookies: tuple[str, str | None]) -> Video:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    max_seconds = MAX_DURATION_MIN * 60

    def deadline_hook(status: dict) -> None:
        """Yuklash cho'zilib ketsa, uni to'xtatadi (oqim osilib qolmasin)."""
        if time.monotonic() > deadline:
            raise _Abort(
                "Video juda sekin yuklanyapti (vaqt tugadi). "
                "Qaytadan urinib ko'ring yoki qisqaroq video yuboring."
            )

    def duration_filter(info: dict, *, incomplete: bool = False) -> None:
        duration = info.get("duration")
        if duration and duration > max_seconds:
            raise _Abort(
                f"Video juda uzun ({duration / 60:.0f} daqiqa). "
                f"{MAX_DURATION_MIN} daqiqagacha bo'lgan videolarni yuboring."
            )
        return None

    opts = _ydl_opts(work_dir, cookies)
    opts["progress_hooks"] = [deadline_hook]
    opts["match_filter"] = duration_filter

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except _Abort as exc:
        raise DownloadError(exc.reason) from exc

    if info is None:
        raise DownloadError("Videoni yuklab bo'lmadi.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("Havolada video topilmadi.")
        info = entries[0]

    path: Path | None = None
    for candidate in (info.get("filepath"), info.get("_filename")):
        if candidate and Path(candidate).exists():
            path = Path(candidate)
            break
    if path is None:
        for downloaded in info.get("requested_downloads") or []:
            candidate = downloaded.get("filepath")
            if candidate and Path(candidate).exists():
                path = Path(candidate)
                break
    if path is None:
        files = sorted(work_dir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
        if files:
            path = files[0]
    if path is None or not path.exists():
        raise DownloadError("Yuklangan fayl topilmadi.")

    return Video(
        path=path,
        work_dir=work_dir,
        title=(info.get("title") or "Video").strip(),
        uploader=(info.get("uploader") or info.get("channel") or "").strip(),
        duration=float(info.get("duration") or 0.0),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        webpage_url=info.get("webpage_url") or url,
        thumbnail=info.get("thumbnail"),
    )


async def download(url: str) -> Video:
    """Videoni yuklaydi; kerak bo'lsa cookie'lar bilan qayta urinadi.

    Havola avval xavfsizlik tekshiruvidan o'tadi (ichki tarmoq manzillari,
    noto'g'ri sxema va portlar rad etiladi).
    """
    try:
        url = await validate_url(url)
    except SecurityError as exc:
        log.warning("Havola rad etildi: %s (%s)", url[:120], exc)
        raise DownloadError(str(exc)) from exc

    loop = asyncio.get_running_loop()
    plan = _cookie_plan()
    last_error = "Videoni yuklab bo'lmadi."

    for index, cookies in enumerate(plan):
        work_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:12]
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            return await loop.run_in_executor(None, _download_sync, url, work_dir, cookies)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(work_dir, ignore_errors=True)
            raw = str(exc)
            if isinstance(exc, DownloadError):
                raise
            log.warning("Yuklash urinishi muvaffaqiyatsiz (%s): %s", cookies[0], raw)
            last_error = raw
            # Faqat autentifikatsiya bilan bog'liq xatoda keyingi usulga o'tamiz
            if index + 1 < len(plan) and _needs_cookies(raw):
                continue
            break

    raise DownloadError(_friendly_error(last_error))
