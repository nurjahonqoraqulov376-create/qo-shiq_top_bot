"""Telegram bot: video havolasi -> video + "Qo'shiqni top" tugmasi -> qo'shiq nomi."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import config, security, storage, texts, variants
from .audio import (
    AudioError,
    ensure_mp4,
    extract_audio,
    has_audio_stream,
    merge_audio,
    wav_duration,
)
from .downloader import (
    DownloadError,
    Song,
    Video,
    download,
    download_preview,
    download_song,
    download_track_audio,
    find_url,
)
from .ffmpeg_setup import ensure_ffmpeg
from .recognizer import Track, identify, search_by_name
from .security import RateLimiter, safe_link
from .storage import Job

log = logging.getLogger("qushiq")

# Bot username'i - izohlar va "Guruhga qo'shish" havolasi uchun
BOT_USERNAME: str = ""

# Telegram bot tokeni ko'rinishi: 123456789:AA...
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

# Telegram bot API orqali fayl yuklab olish chegarasi (20 MB)
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

router = Router()

_download_sem = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
_busy_users: set[int] = set()
_searching: set[str] = set()
_audio_busy: set[str] = set()

# Qidiruv tugmasi uchun alohida, yumshoqroq cheklov (yuklashsiz, arzonroq amal)
_search_limiter = RateLimiter(config.RATE_LIMIT_PER_HOUR * 2, cooldown=3)


# --------------------------------------------------------------------------- #
# Yordamchi funksiyalar
# --------------------------------------------------------------------------- #
def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def group_button() -> list[InlineKeyboardButton]:
    """«Guruhga qo'shish» tugmasi (bot nomi ma'lum bo'lsa)."""
    if not BOT_USERNAME:
        return []
    return [
        InlineKeyboardButton(
            text="Guruhga qo'shish 🎵",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
        )
    ]


def find_button(token: str, again: bool = False) -> InlineKeyboardMarkup:
    label = "🔄 Qayta urinish" if again else "📥 Qo'shiqni yuklab olish"
    rows = [[InlineKeyboardButton(text=label, callback_data=f"song:{token}")]]
    group = group_button()
    if group:
        rows.append(group)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def variant_keyboard(token: str, exclude: str) -> InlineKeyboardMarkup:
    """Audio ostidagi tugmalar: yuborilgandan boshqa variantlar."""
    keys = variants.other_keys(exclude)
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(keys), 2):
        rows.append([
            InlineKeyboardButton(
                text=variants.VARIANTS[key].label,
                callback_data=f"v:{token}:{key}",
            )
            for key in keys[index:index + 2]
        ])
    group = group_button()
    if group:
        rows.append(group)
    return InlineKeyboardMarkup(inline_keyboard=rows)






async def safe_edit(message: Message | None, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text)
    except TelegramBadRequest:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("Xabarni tahrirlab bo'lmadi: %s", exc)


async def safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Buyruqlar
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START, disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, disable_web_page_preview=True)


# --------------------------------------------------------------------------- #
# Havola qabul qilish
# --------------------------------------------------------------------------- #
async def restore_audio(video: Video, url: str) -> None:
    """Ovozsiz yuklangan videoga ovozni qaytaradi.

    yt-dlp ba'zan video va audio oqimlarini birlashtira olmaydi - natijada
    ovozsiz fayl qoladi. Bunda ham video jimjit chiqadi, ham qo'shiqni
    aniqlab bo'lmaydi. Shuning uchun ovozni alohida yuklab, qayta qo'shamiz.
    """
    try:
        if await has_audio_stream(video.path):
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("Ovozni tekshirib bo'lmadi: %s", exc)
        return

    log.info("Video ovozsiz keldi, ovoz alohida yuklanmoqda: %s", url)
    try:
        sound = await download_track_audio(url, video.work_dir)
        merged = video.work_dir / "video_sound.mp4"
        await merge_audio(video.path, sound, merged)
    except (DownloadError, AudioError) as exc:
        log.info("Ovozni qaytarib bo'lmadi (%s): %s", url, exc)
        return
    except Exception:  # noqa: BLE001
        log.exception("Ovozni qaytarishda kutilmagan xato")
        return

    video.path = merged
    log.info("Videoga ovoz qo'shildi: %s", merged.name)


def split_song_title(raw: str) -> tuple[str, str]:
    """YouTube sarlavhasidan "Ijrochi - Nom" ni ajratadi."""
    cleaned = " ".join((raw or "").split())
    for separator in (" - ", " – ", " — ", " | "):
        if separator in cleaned:
            artist, title = cleaned.split(separator, 1)
            if artist.strip() and title.strip():
                return artist.strip()[:80], title.strip()[:80]
    return "", cleaned[:80]


async def find_song_by_text(query: str) -> Track:
    """Matn bo'yicha qo'shiq ma'lumotini tayyorlaydi.

    Avval Apple katalogidan qidiriladi (muqova, albom, parcha bilan).
    U yerda bo'lmasa (masalan mahalliy qo'shiqlar), matnning o'zi qidiruv
    so'rovi sifatida ishlatiladi - audio manbasi uni YouTube'dan topadi.
    """
    track = await search_by_name(query)
    if track is not None:
        return track
    log.info("Apple katalogida topilmadi, matn bo'yicha qidiriladi: %s", query[:80])
    return Track(
        title=" ".join(query.split())[:80],
        artist="",
        key=f"query|{query.strip().lower()}"[:120],
        source="Qidiruv",
    )


async def handle_song_query(message: Message, query: str, user_id: int) -> None:
    """Foydalanuvchi qo'shiq nomini yozganda ishlaydi (havolasiz)."""
    status = await message.answer(texts.SONG_SEARCHING)
    work_dir = config.DOWNLOAD_DIR / f"q_{storage.new_token()[:12]}"
    try:
        track = await find_song_by_text(query)

        job = Job(
            token=storage.new_token(),
            user_id=user_id,
            chat_id=message.chat.id,
            url="",
            title=query[:200],
            work_dir=str(work_dir),
            result=asdict(track),
        )
        await storage.put(job)
        await safe_delete(status)
        status = None

        sent = await deliver_variant(message, job, track, variants.AUTO_KEY)
        if not sent:
            await storage.drop(job.token)
    except DownloadError as exc:
        await safe_edit(status, f"❌ {esc(str(exc))}")
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.exception("Nom bo'yicha qidirishda xato")
        await safe_edit(status, texts.ERROR)
        shutil.rmtree(work_dir, ignore_errors=True)


@router.message(F.text & ~F.text.startswith("/"))
@router.message(F.caption)          # havola rasm/video izohida kelgan holat
async def on_link(message: Message) -> None:
    """Havola bo'lsa - video yuklanadi, aks holda matn qo'shiq nomi deb qidiriladi."""
    text = (message.text or message.caption or "").strip()
    url = find_url(text)

    user_id = message.from_user.id if message.from_user else message.chat.id
    if not security.is_allowed_user(user_id):
        await message.answer(texts.NOT_ALLOWED)
        return
    if user_id in _busy_users:
        await message.answer(texts.BUSY)
        return

    if url is None:
        if len(text) < 3:
            await message.answer(texts.NO_URL)
            return
        warning = security.rate_check(security.limiter, user_id)
        if warning:
            await message.answer(warning)
            return
        _busy_users.add(user_id)
        try:
            await handle_song_query(message, text, user_id)
        finally:
            _busy_users.discard(user_id)
        return

    warning = security.rate_check(security.limiter, user_id)
    if warning:
        await message.answer(warning)
        return

    _busy_users.add(user_id)
    status = await message.answer(texts.DOWNLOADING)
    video: Video | None = None
    token: str | None = None
    try:
        # Disk to'lib ketmasligi uchun tekshiruv
        used_mb = await asyncio.to_thread(storage.disk_usage_mb)
        if used_mb > config.MAX_DISK_MB:
            await storage.cleanup_once()
            used_mb = await asyncio.to_thread(storage.disk_usage_mb)
            if used_mb > config.MAX_DISK_MB:
                log.warning("Disk chegarasi: %.0f MB / %s MB", used_mb, config.MAX_DISK_MB)
                await safe_edit(status, texts.DISK_BUSY)
                return

        async with _download_sem:
            await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
            video = await download(url)

        token = storage.new_token()
        job = Job(
            token=token,
            user_id=user_id,
            chat_id=message.chat.id,
            url=video.webpage_url,
            title=video.title,
            work_dir=str(video.work_dir),
        )

        # Yuklangan faylda ovoz bo'lmasa (yt-dlp birlashtira olmagan bo'lsa),
        # ovozni alohida yuklab, videoga qaytadan qo'shamiz
        await restore_audio(video, url)

        # Audio darhol ajratiladi - tugma bosilganda javob tez bo'lishi uchun
        try:
            wav = await extract_audio(video.path, video.work_dir / "audio.wav")
            if wav_duration(wav) >= 1.0:
                job.audio_path = str(wav)
        except AudioError as exc:
            log.info("Audio ajratilmadi (%s): %s", video.webpage_url, exc)
        except Exception:  # noqa: BLE001
            log.exception("Audio ajratishda kutilmagan xato")

        await storage.put(job)

        caption = texts.video_caption(BOT_USERNAME)

        if video.size_mb > config.MAX_UPLOAD_MB:
            await safe_edit(status, texts.TOO_BIG.format(limit=config.MAX_UPLOAD_MB))
            await message.answer(caption, reply_markup=find_button(token))
            return

        await safe_edit(status, texts.UPLOADING)
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)

        kwargs: dict = {}
        if video.width and video.height:
            kwargs.update(width=video.width, height=video.height)
        if video.duration:
            kwargs["duration"] = int(video.duration)

        # webm/mkv bo'lsa mp4 ga o'raymiz - Telegram to'g'ri ko'rsatishi uchun
        send_path = await ensure_mp4(video.path)

        try:
            await message.answer_video(
                FSInputFile(send_path, filename=send_path.name),
                caption=caption,
                reply_markup=find_button(token),
                supports_streaming=True,
                **kwargs,
            )
        except (TelegramAPIError, OSError) as exc:
            log.warning("Video yuborilmadi (%s), matn bilan davom etamiz", exc)
            await message.answer(
                caption + "\n\n⚠️ <i>Videoni yuborib bo'lmadi, lekin qo'shiqni topa olaman.</i>",
                reply_markup=find_button(token),
            )
        await safe_delete(status)

    except DownloadError as exc:
        await safe_edit(status, f"❌ {esc(str(exc))}")
    except Exception:  # noqa: BLE001
        log.exception("Havolani qayta ishlashda xato")
        await safe_edit(status, texts.ERROR)
        # Yarim qolgan ish uchun tugma qoldirmaymiz: yozuv va fayllarni o'chiramiz
        if token is not None:
            await storage.drop(token)
        elif video is not None:
            shutil.rmtree(video.work_dir, ignore_errors=True)
    finally:
        _busy_users.discard(user_id)


# --------------------------------------------------------------------------- #
# "Qo'shiqni top" tugmasi
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("song:"))
async def on_find_song(callback: CallbackQuery) -> None:
    token = (callback.data or "").split(":", 1)[1]
    message = callback.message
    if message is None:
        await callback.answer()
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not security.is_allowed_user(user_id):
        await callback.answer(texts.NOT_ALLOWED, show_alert=True)
        return

    job = await storage.get(token)
    if job is None:
        await callback.answer("Ma'lumot eskirgan", show_alert=False)
        await message.answer(texts.EXPIRED)
        return

    # Tugma faqat o'zi tug'ilgan suhbatda ishlaydi
    if job.chat_id != message.chat.id:
        await callback.answer(texts.WRONG_CHAT, show_alert=True)
        return

    # Avval topilgan bo'lsa - darhol qaytaramiz (fayllar o'chgan bo'lsa ham)
    if job.result:
        await callback.answer("Natija tayyor ✅")
        await deliver_variant(message, job, Track(**job.result), variants.AUTO_KEY)
        return

    if job.expired:
        await callback.answer("Ma'lumot eskirgan", show_alert=False)
        await message.answer(texts.EXPIRED)
        return

    if token in _searching:
        await callback.answer(texts.ALREADY_SEARCHING, show_alert=True)
        return

    # Audio fayl haqiqatan ham o'z papkamiz ichidami (buzilgan yozuvga qarshi)
    audio = Path(job.audio_path) if job.audio_path else None
    if (
        audio is None
        or not security.is_inside(audio, config.DOWNLOAD_DIR)
        or not audio.exists()
    ):
        await callback.answer()
        await message.answer(texts.NO_AUDIO)
        return

    warning = security.rate_check(_search_limiter, user_id)
    if warning:
        await callback.answer(warning[:190], show_alert=True)
        return

    _searching.add(token)
    await callback.answer("Qidirilmoqda... 🔎")
    status = await message.answer(texts.SEARCHING)

    async def on_progress(step: int, total: int) -> None:
        if total < 0:                       # tezlik tuzatish bosqichi
            await safe_edit(status, texts.SEARCHING_SPEED)
        elif total == 0:                    # chuqurroq tinglash bosqichi
            await safe_edit(status, texts.SEARCHING_DEEP)
        else:
            await safe_edit(status, texts.SEARCHING_STEP.format(step=step, total=total))

    try:
        track = await identify(audio, on_progress=on_progress)
    except Exception:  # noqa: BLE001
        log.exception("Qo'shiq qidirishda xato")
        await safe_edit(status, texts.ERROR)
        return
    finally:
        _searching.discard(token)

    await safe_delete(status)

    if track is None:
        job.not_found = True
        await storage.update(job)
        await message.answer(texts.NOT_FOUND, reply_markup=find_button(token, again=True))
        return

    job.result = asdict(track)
    job.not_found = False
    await storage.update(job)

    # Qo'shiq audiosi to'g'ridan-to'g'ri yuboriladi - qo'shimcha bosish shart emas
    await deliver_variant(message, job, track, variants.AUTO_KEY)


# --------------------------------------------------------------------------- #
# Audio variantlari: Original / Slowed / Slowed + Reverb / Speed Up
# --------------------------------------------------------------------------- #
def safe_filename(name: str) -> str:
    """Fayl nomidan xavfli belgilarni olib tashlaydi."""
    cleaned = "".join(ch for ch in name if ch not in '\\/:*?"<>|\n\r\t')
    cleaned = " ".join(cleaned.split()).strip(". ")
    return cleaned[:80] or "audio"


async def fetch_cover(url: str, work_dir: Path) -> Path | None:
    """Muqovani yuklab, Telegram uchun 320x320 JPEG ga aylantiradi."""
    link = safe_link(url)
    if not link:
        return None
    try:
        await security.validate_url(link)
    except security.SecurityError:
        return None

    raw = work_dir / "cover_raw"
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(link) as response:
                if response.status != 200:
                    return None
                data = await response.content.read(5 * 1024 * 1024)
        if not data:
            return None
        raw.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        log.debug("Muqova yuklanmadi: %s", exc)
        return None

    thumb = await variants.prepare_thumbnail(raw, work_dir / "cover.jpg")
    raw.unlink(missing_ok=True)
    return thumb


async def fetch_song_audio(track: Track, work_dir: Path) -> Song:
    """Qo'shiq audiosini topadi: YouTube → SoundCloud → Apple parchasi.

    YouTube server IP'sini "bot" deb bloklashi mumkin, shuning uchun
    bir nechta manba ketma-ket sinaladi. Oxirgi zaxira - Shazam bergan
    ~30 soniyalik Apple parchasi: u hech qachon bloklanmaydi.
    """
    query = f"{track.artist} {track.title}".strip()
    try:
        return await download_song(query, work_dir=work_dir)
    except DownloadError as exc:
        if not track.preview:
            raise
        log.info("To'liq audio topilmadi (%s), Apple parchasi ishlatiladi", exc)

    return await download_preview(track.preview, work_dir)


async def deliver_variant(message: Message, job: Job, track: Track, key: str) -> bool:
    """Variantni tayyorlab, audio qilib yuboradi. Yuborilsa True qaytaradi."""
    variant = variants.get(key)
    if variant is None:
        return False

    token = job.token
    audio_title = f"{track.title}{variant.suffix}"
    caption = texts.audio_caption(
        esc(track.artist), esc(audio_title), BOT_USERNAME, job.song_is_preview
    )
    keyboard = variant_keyboard(token, key)

    # 1) Avval yuborilgan bo'lsa - Telegram file_id orqali bir zumda qaytaramiz
    cached_id = job.audio_ids.get(key)
    if cached_id:
        try:
            await message.answer_audio(
                cached_id, caption=caption, reply_markup=keyboard
            )
            return True
        except TelegramAPIError as exc:
            log.info("Keshdagi audio yaroqsiz (%s), qaytadan tayyorlanadi", exc)
            job.audio_ids.pop(key, None)

    if token in _audio_busy:
        await message.answer(texts.AUDIO_BUSY)
        return False

    work_dir = Path(job.work_dir)
    if not security.is_inside(work_dir, config.DOWNLOAD_DIR):
        await message.answer(texts.EXPIRED)
        return False

    _audio_busy.add(token)
    status = await message.answer(texts.AUDIO_PREPARING.format(label=variant.label))
    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        # 2) Qo'shiqning to'liq audiosi (bir marta yuklanadi, keyin keshdan)
        song_path = Path(job.song_path) if job.song_path else None
        if song_path is None or not song_path.exists():
            await safe_edit(status, texts.AUDIO_SEARCHING)
            async with _download_sem:
                song = await fetch_song_audio(track, work_dir)
            job.song_path = str(song.path)
            job.song_duration = song.duration
            job.song_is_preview = song.is_preview

            # Nom bo'yicha qidirilgan bo'lsa, aniq nomini manbadan olamiz
            if track.source == "Qidiruv" and song.title:
                artist, title = split_song_title(song.title)
                track.title = title or track.title
                track.artist = artist or track.artist or "Noma'lum ijrochi"
                job.result = asdict(track)
                audio_title = f"{track.title}{variant.suffix}"

            await storage.update(job)
            song_path = song.path
            caption = texts.audio_caption(
                esc(track.artist), esc(audio_title), BOT_USERNAME, song.is_preview
            )

        # 3) Muqova (ixtiyoriy - bo'lmasa ham audio yuboriladi)
        thumb = Path(job.thumb_path) if job.thumb_path else None
        if (thumb is None or not thumb.exists()) and track.cover:
            thumb = await fetch_cover(track.cover, work_dir)
            if thumb:
                job.thumb_path = str(thumb)
                await storage.update(job)

        # 4) Variantni tayyorlaymiz (tayyor bo'lsa - qayta ishlamaymiz)
        out_path = work_dir / f"variant_{key}.mp3"
        if not out_path.exists() or out_path.stat().st_size == 0:
            await safe_edit(status, texts.AUDIO_MAKING.format(label=variant.label))
            await variants.make_variant(song_path, out_path, variant)

        if out_path.stat().st_size / (1024 * 1024) > config.MAX_UPLOAD_MB:
            await safe_edit(status, texts.AUDIO_TOO_BIG)
            return False

        await safe_edit(status, texts.AUDIO_SENDING)
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)

        kwargs: dict = {}
        if job.song_duration:
            kwargs["duration"] = max(1, int(job.song_duration / variant.speed))
        if thumb and thumb.exists():
            kwargs["thumbnail"] = FSInputFile(thumb)

        sent = await message.answer_audio(
            FSInputFile(
                out_path,
                filename=safe_filename(f"{track.artist} - {audio_title}") + ".mp3",
            ),
            title=audio_title[:64],
            performer=track.artist[:64],
            caption=caption,
            reply_markup=keyboard,
            **kwargs,
        )
        if sent.audio:
            job.audio_ids[key] = sent.audio.file_id
            await storage.update(job)
        await safe_delete(status)
        return True

    except DownloadError as exc:
        await safe_edit(status, f"❌ {esc(str(exc))}")
    except AudioError as exc:
        log.warning("Variant tayyorlanmadi: %s", exc)
        await safe_edit(status, texts.AUDIO_FAILED)
    except Exception:  # noqa: BLE001
        log.exception("Audio variantida kutilmagan xato")
        await safe_edit(status, texts.ERROR)
    finally:
        _audio_busy.discard(token)
    return False


@router.callback_query(F.data.startswith("v:"))
async def on_variant(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    message = callback.message
    if len(parts) != 3 or message is None:
        await callback.answer()
        return

    _, token, key = parts
    variant = variants.get(key)
    if variant is None:
        await callback.answer()
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if not security.is_allowed_user(user_id):
        await callback.answer(texts.NOT_ALLOWED, show_alert=True)
        return

    job = await storage.get(token)
    if job is None or not job.result:
        await callback.answer("Ma'lumot eskirgan", show_alert=False)
        await message.answer(texts.EXPIRED)
        return
    if job.chat_id != message.chat.id:
        await callback.answer(texts.WRONG_CHAT, show_alert=True)
        return

    # Tayyor variant uchun cheklov qo'llanmaydi - u shunchaki keshdan keladi
    if key not in job.audio_ids:
        if token in _audio_busy:
            await callback.answer(texts.AUDIO_BUSY, show_alert=True)
            return
        warning = security.rate_check(_search_limiter, user_id)
        if warning:
            await callback.answer(warning[:190], show_alert=True)
            return

    await callback.answer(f"{variant.label} tayyorlanmoqda...")
    await deliver_variant(message, job, Track(**job.result), key)


@router.message(F.video | F.video_note | F.audio | F.voice | F.document)
async def on_media(message: Message) -> None:
    """Foydalanuvchi videoni (yoki ovozni) to'g'ridan-to'g'ri yuborganda.

    Instagram/YouTube havolani bermay qo'yganda ham ishlaydigan yo'l:
    foydalanuvchi videoni «Share → Telegram» orqali botga yuboradi.
    """
    media = (
        message.video
        or message.video_note
        or message.audio
        or message.voice
        or message.document
    )
    if media is None:
        return

    mime = (getattr(media, "mime_type", "") or "").lower()
    if message.document and not (mime.startswith("video/") or mime.startswith("audio/")):
        await message.answer(texts.NO_URL)
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    if not security.is_allowed_user(user_id):
        await message.answer(texts.NOT_ALLOWED)
        return
    if user_id in _busy_users:
        await message.answer(texts.BUSY)
        return

    size = getattr(media, "file_size", 0) or 0
    if size > TELEGRAM_DOWNLOAD_LIMIT:
        await message.answer(texts.MEDIA_TOO_BIG)
        return

    warning = security.rate_check(security.limiter, user_id)
    if warning:
        await message.answer(warning)
        return

    _busy_users.add(user_id)
    status = await message.answer(texts.MEDIA_RECEIVED)
    work_dir = config.DOWNLOAD_DIR / f"m_{storage.new_token()[:12]}"
    token: str | None = None
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        source = work_dir / "input.bin"
        await message.bot.download(media, destination=source)

        wav = work_dir / "audio.wav"
        try:
            await extract_audio(source, wav)
        except AudioError as exc:
            log.info("Yuborilgan faylda ovoz yo'q: %s", exc)
            await safe_edit(status, texts.NO_AUDIO)
            shutil.rmtree(work_dir, ignore_errors=True)
            return

        token = storage.new_token()
        job = Job(
            token=token,
            user_id=user_id,
            chat_id=message.chat.id,
            url="",
            title=texts.MEDIA_TITLE,
            work_dir=str(work_dir),
            audio_path=str(wav),
        )
        await storage.put(job)

        async def on_progress(step: int, total: int) -> None:
            if total < 0:
                await safe_edit(status, texts.SEARCHING_SPEED)
            elif total == 0:
                await safe_edit(status, texts.SEARCHING_DEEP)
            else:
                await safe_edit(
                    status, texts.SEARCHING_STEP.format(step=step, total=total)
                )

        track = await identify(wav, on_progress=on_progress)
        if track is None:
            await safe_edit(status, texts.NOT_FOUND)
            await storage.drop(token)
            return

        job.result = asdict(track)
        await storage.update(job)
        await safe_delete(status)
        status = None
        if not await deliver_variant(message, job, track, variants.AUTO_KEY):
            await storage.drop(token)

    except TelegramAPIError as exc:
        log.warning("Faylni olishda xato: %s", exc)
        await safe_edit(status, texts.MEDIA_FAILED)
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.exception("Yuborilgan faylni qayta ishlashda xato")
        await safe_edit(status, texts.ERROR)
        if token:
            await storage.drop(token)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        _busy_users.discard(user_id)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(texts.NO_URL)


# --------------------------------------------------------------------------- #
# Ishga tushirish
# --------------------------------------------------------------------------- #
async def main() -> None:
    # Windows konsoli emoji va boshqa belgilarni ko'tara olishi uchun
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi. Loyiha papkasidagi .env fayliga "
            "BOT_TOKEN=... yozing (.env.example dan nusxa oling)."
        )
    if not TOKEN_RE.fullmatch(config.BOT_TOKEN):
        raise SystemExit(
            "BOT_TOKEN ko'rinishi noto'g'ri. U «123456789:AA...» shaklida bo'lishi kerak."
        )
    if config.ALLOWED_USERS:
        log.info("Yopiq rejim: faqat %d ta foydalanuvchi", len(config.ALLOWED_USERS))

    ensure_ffmpeg()
    storage.load()

    session = AiohttpSession(timeout=300)
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    cleaner = asyncio.create_task(
        storage.cleanup_loop(on_tick=security.limiter.forget_old)
    )
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            raise SystemExit(
                "BOT_TOKEN noto'g'ri yoki bekor qilingan. @BotFather dan yangi "
                "token oling va .env fayliga yozing."
            ) from None
        global BOT_USERNAME
        BOT_USERNAME = me.username or ""
        log.info("Bot ishga tushdi: @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=True)

        # Server (Railway, VPS) to'xtatish signali yuborganda toza yopilamiz -
        # aks holda Telegram bilan ulanish osilib qoladi va keyingi nusxa
        # "conflict" xatosini oladi.
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signal_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows'da qo'llab-quvvatlanmaydi - Ctrl+C ishlaydi

        polling = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        )
        waiter = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {polling, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if polling in done:
                polling.result()          # xato bo'lsa shu yerda ko'tariladi
            else:
                log.info("To'xtatish signali keldi, bot yopilmoqda...")
                await dp.stop_polling()
                with suppress(Exception):
                    await polling
        except TelegramConflictError:
            raise SystemExit(
                "Bu bot allaqachon boshqa joyda ishlab turibdi. Avvalgi nusxasini "
                "to'xtating (bir vaqtda faqat bitta nusxa ishlashi mumkin)."
            ) from None
        finally:
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
    finally:
        cleaner.cancel()
        await asyncio.gather(cleaner, return_exceptions=True)
        await bot.session.close()


def run() -> None:
    # Diqqat: Windows'da standart ProactorEventLoop qoldiriladi -
    # SelectorEventLoop subprocess (ffmpeg) ni qo'llab-quvvatlamaydi.
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        print("\nBot to'xtatildi.")


if __name__ == "__main__":
    run()
