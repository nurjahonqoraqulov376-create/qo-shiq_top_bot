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
from urllib.parse import urlsplit

import yt_dlp

from .config import (
    COOKIES_FILE,
    COOKIES_FROM_BROWSER,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT,
    MAX_DURATION_MIN,
    MAX_FILESIZE_MB,
    MAX_VIDEO_HEIGHT,
    SONG_MAX_MB,
    SONG_MAX_MIN,
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


def _is_age_gated(message: str) -> bool:
    low = message.lower()
    return "confirm your age" in low or "age-restricted" in low


def admin_hint(message: str) -> str | None:
    """Bot egasi uchun maslahat (foydalanuvchiga emas, log'ga yoziladi)."""
    if _is_age_gated(message):
        return (
            "Yosh cheklovi: buni faqat 18+ akkaunt cookie'lari hal qiladi. "
            "cookies.txt ni COOKIES_B64 sozlamasiga joylang."
        )
    if _needs_cookies(message):
        return (
            "Sayt login talab qilyapti. Serverda COOKIES_B64, kompyuterda esa "
            "COOKIES_FROM_BROWSER=auto sozlamasidan foydalaning."
        )
    return None


def _friendly_error(message: str) -> str:
    low = message.lower()
    # Foydalanuvchiga faqat tushunarli xabar beriladi - texnik maslahatlar log'da
    if _is_age_gated(low):
        return (
            "🔞 Bu videoda yosh cheklovi bor. YouTube uni faqat tizimga kirgan "
            "foydalanuvchilarga ko'rsatadi, shuning uchun yuklab bo'lmaydi.\n"
            "Boshqa havola yuborib ko'ring."
        )
    if "private" in low or "unavailable" in low or "has been removed" in low:
        return "Bu post yopiq (private) yoki o'chirilgan — video yuklab bo'lmadi."
    if _needs_cookies(low):
        # Foydalanuvchiga ishlaydigan yo'l ko'rsatamiz: videoning o'zini yuborish
        return (
            "🔒 Sayt bu videoni yuklab olishga ruxsat bermadi.\n\n"
            "✅ <b>Yechim:</b> videoni menga to'g'ridan-to'g'ri yuboring — "
            "qo'shig'ini shundan topib beraman.\n"
            "<i>Instagram'da: video ostidagi «Share» → Telegram → shu bot.</i>"
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
        # Diqqat: yt-dlp alohida bo'laklarni "video.f137.mp4" deb nomlaydi.
        # Ular ovozsiz bo'lishi mumkin, shuning uchun avval birlashtirilgan
        # faylni ("video.mp4") qidiramiz.
        merged = [f for f in work_dir.glob("video.*") if f.stem == "video"]
        parts = [f for f in work_dir.glob("video.*") if f.stem != "video"]
        for group in (merged, parts):
            files = sorted(group, key=lambda p: p.stat().st_size, reverse=True)
            if files:
                path = files[0]
                break
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


@dataclass(slots=True)
class Song:
    """Topilgan qo'shiqning audiosi."""

    path: Path
    title: str
    duration: float
    webpage_url: str
    source: str = "YouTube"
    is_preview: bool = False        # faqat ~30 soniyalik parcha bo'lsa True


# Qo'shiq audiosi qayerdan qidiriladi. Serverning IP manzilini YouTube ba'zan
# "bot" deb bloklaydi, shuning uchun zaxira manbalar ham bor.
SONG_SOURCES: tuple[tuple[str, str], ...] = (
    ("YouTube", "ytsearch1:"),
    ("SoundCloud", "scsearch1:"),
)

# Apple preview faqat shu manzillardan yuklanadi (SSRF himoyasi)
_PREVIEW_HOST_SUFFIXES = (".apple.com", ".mzstatic.com")
_PREVIEW_MAX_MB = 20


def _song_opts(work_dir: Path, cookies: tuple[str, str | None]) -> dict:
    """Qo'shiq audiosini qidirib yuklash uchun sozlamalar."""
    opts = _ydl_opts(work_dir, cookies)
    opts.update({
        "outtmpl": str(work_dir / "song.%(ext)s"),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "default_search": "ytsearch1",
        "max_filesize": SONG_MAX_MB * 1024 * 1024,
        "noplaylist": True,
    })
    opts.pop("merge_output_format", None)
    return opts


def _download_song_sync(query: str, work_dir: Path, cookies: tuple[str, str | None]) -> Song:
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT

    def deadline_hook(status: dict) -> None:
        if time.monotonic() > deadline:
            raise _Abort("Qo'shiqni yuklash juda uzoq davom etdi.")

    def duration_filter(info: dict, *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        # Juda uzun natijalar odatda "mix"/"album" bo'ladi - ularni o'tkazamiz
        if duration and duration > SONG_MAX_MIN * 60:
            return "juda uzun"
        return None

    opts = _song_opts(work_dir, cookies)
    opts["progress_hooks"] = [deadline_hook]
    opts["match_filter"] = duration_filter

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=True)
    except _Abort as exc:
        raise DownloadError(exc.reason) from exc

    if info is None:
        raise DownloadError("Bu qo'shiqning audiosi topilmadi.")
    if info.get("_type") in ("playlist", "compat_list"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("Bu qo'shiqning audiosi topilmadi.")
        info = entries[0]

    path: Path | None = None
    for downloaded in info.get("requested_downloads") or []:
        candidate = downloaded.get("filepath")
        if candidate and Path(candidate).exists():
            path = Path(candidate)
            break
    if path is None:
        files = sorted(work_dir.glob("song.*"), key=lambda p: p.stat().st_size, reverse=True)
        if files:
            path = files[0]
    if path is None or not path.exists():
        raise DownloadError("Qo'shiq fayli yuklanmadi.")

    return Song(
        path=path,
        title=(info.get("title") or "").strip(),
        duration=float(info.get("duration") or 0.0),
        webpage_url=info.get("webpage_url") or "",
    )


async def download_song(query: str, work_dir: Path | None = None) -> Song:
    """Qo'shiq nomi bo'yicha YouTube'dan to'liq audiosini yuklaydi.

    `query` - foydalanuvchi havolasi emas, balki Shazam qaytargan
    "ijrochi - nom" matni, shuning uchun u qidiruv so'rovi sifatida ishlatiladi.

    `work_dir` berilsa, fayl o'sha papkaga tushadi va xato bo'lganda papka
    o'chirilmaydi (unda chaqiruvchining boshqa fayllari bo'lishi mumkin).
    """
    clean = " ".join((query or "").split())[:150]
    if not clean:
        raise DownloadError("Qo'shiq nomi bo'sh.")

    owns_dir = work_dir is None
    loop = asyncio.get_running_loop()
    plan = _cookie_plan()
    last_error = "Qo'shiq audiosini topib bo'lmadi."

    # Har bir manba (YouTube, SoundCloud) o'z navbatida sinaladi. YouTube
    # server IP'sini bloklasa, qolganlari ishlashda davom etadi.
    for source_name, prefix in SONG_SOURCES:
        for index, cookies in enumerate(plan):
            if owns_dir:
                target = DOWNLOAD_DIR / f"song_{uuid.uuid4().hex[:12]}"
            else:
                target = work_dir  # type: ignore[assignment]
            target.mkdir(parents=True, exist_ok=True)
            try:
                song = await loop.run_in_executor(
                    None, _download_song_sync, prefix + clean, target, cookies
                )
                song.source = source_name
                if source_name != SONG_SOURCES[0][0]:
                    log.info("Qo'shiq %s dan olindi: %s", source_name, clean)
                return song
            except DownloadError as exc:
                if owns_dir:
                    shutil.rmtree(target, ignore_errors=True)
                last_error = str(exc)
                break            # bu manbada topilmadi - keyingisiga o'tamiz
            except Exception as exc:  # noqa: BLE001
                if owns_dir:
                    shutil.rmtree(target, ignore_errors=True)
                raw = str(exc)
                log.warning("Qo'shiq yuklanmadi (%s, %s): %s",
                            source_name, cookies[0], raw)
                hint = admin_hint(raw)
                if hint:
                    log.warning("Bot egasiga maslahat: %s", hint)
                last_error = raw
                if index + 1 < len(plan) and _needs_cookies(raw):
                    continue     # cookie'lar bilan qayta urinamiz
                break            # keyingi manbaga o'tamiz

    raise DownloadError(_friendly_error(last_error))


def _download_track_audio_sync(
    url: str, work_dir: Path, cookies: tuple[str, str | None]
) -> Path:
    opts = _ydl_opts(work_dir, cookies)
    opts.update({
        "outtmpl": str(work_dir / "sound.%(ext)s"),
        "format": "bestaudio/best",
        "max_filesize": SONG_MAX_MB * 1024 * 1024,
    })
    opts.pop("merge_output_format", None)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info is None:
        raise DownloadError("Ovoz yo'lakchasi topilmadi.")
    if info.get("_type") in ("playlist", "compat_list"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("Ovoz yo'lakchasi topilmadi.")
        info = entries[0]

    for downloaded in info.get("requested_downloads") or []:
        candidate = downloaded.get("filepath")
        if candidate and Path(candidate).exists():
            return Path(candidate)
    files = sorted(work_dir.glob("sound.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise DownloadError("Ovoz fayli yuklanmadi.")
    return files[0]


async def download_track_audio(url: str, work_dir: Path) -> Path:
    """Videoning ovoz yo'lakchasini alohida yuklaydi.

    yt-dlp video va audioni birlashtira olmagan (yoki sayt faqat ovozsiz
    ko'rinish bergan) hollarda ishlatiladi.
    """
    loop = asyncio.get_running_loop()
    work_dir.mkdir(parents=True, exist_ok=True)
    last_error = "Ovoz yo'lakchasini yuklab bo'lmadi."

    for index, cookies in enumerate(_cookie_plan()):
        try:
            return await loop.run_in_executor(
                None, _download_track_audio_sync, url, work_dir, cookies
            )
        except DownloadError as exc:
            last_error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            raw = str(exc)
            log.info("Ovozni alohida yuklash muvaffaqiyatsiz (%s): %s",
                     cookies[0], raw[:160])
            last_error = raw
            if index + 1 < len(_cookie_plan()) and _needs_cookies(raw):
                continue
            break

    raise DownloadError(_friendly_error(last_error))


async def download_preview(url: str, work_dir: Path) -> Song:
    """Apple'ning ~30 soniyalik parchasini yuklaydi (oxirgi zaxira).

    YouTube ham, SoundCloud ham ishlamaganda ishlatiladi. Havola Shazam'dan
    keladi, shuning uchun manzil Apple domenlari bilan cheklangan.
    """
    import aiohttp

    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(_PREVIEW_HOST_SUFFIXES):
        raise DownloadError("Qo'shiq parchasi uchun havola yaroqsiz.")

    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = work_dir / "preview.m4a"
    limit = _PREVIEW_MAX_MB * 1024 * 1024
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": _UA}) as resp:
                if resp.status != 200:
                    raise DownloadError(f"Parcha yuklanmadi (HTTP {resp.status}).")
                size = 0
                with out_path.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise DownloadError("Parcha kutilganidan katta.")
                        fh.write(chunk)
    except DownloadError:
        out_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        out_path.unlink(missing_ok=True)
        log.warning("Apple parchasi yuklanmadi: %s", exc)
        raise DownloadError("Qo'shiq parchasini yuklab bo'lmadi.") from exc

    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise DownloadError("Qo'shiq parchasi bo'sh chiqdi.")

    log.info("Apple parchasi ishlatildi (%.1f KB)", out_path.stat().st_size / 1024)
    return Song(
        path=out_path,
        title="",
        duration=0.0,
        webpage_url=url,
        source="Apple Music",
        is_preview=True,
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
            hint = admin_hint(raw)
            if hint:
                log.warning("Bot egasiga maslahat: %s", hint)
            last_error = raw
            # Faqat autentifikatsiya bilan bog'liq xatoda keyingi usulga o'tamiz
            if index + 1 < len(plan) and _needs_cookies(raw):
                continue
            break

    raise DownloadError(_friendly_error(last_error))
