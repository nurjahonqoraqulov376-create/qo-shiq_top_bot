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
from urllib.parse import quote_plus

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
    URLInputFile,
)

from . import config, security, storage, texts, variants
from .audio import AudioError, ensure_mp4, extract_audio, wav_duration
from .downloader import DownloadError, Video, download, download_song, find_url
from .ffmpeg_setup import ensure_ffmpeg
from .recognizer import Track, identify
from .security import RateLimiter, safe_link
from .storage import Job

log = logging.getLogger("qushiq")

# Telegram bot tokeni ko'rinishi: 123456789:AA...
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")

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


def find_button(token: str, again: bool = False) -> InlineKeyboardMarkup:
    label = "🔄 Qayta urinish" if again else "🎵 Qo'shiqni top"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"song:{token}")]]
    )


def result_keyboard(track: Track, token: str) -> InlineKeyboardMarkup:
    query = quote_plus(f"{track.artist} {track.title}".strip()[:200])
    rows: list[list[InlineKeyboardButton]] = []

    # 1) Audio variantlari - asosiy imkoniyat
    for row in variants.KEYBOARD_ROWS:
        buttons = [
            InlineKeyboardButton(
                text=variants.VARIANTS[key].label,
                callback_data=f"v:{token}:{key}",
            )
            for key in row
            if key in variants.VARIANTS
        ]
        if buttons:
            rows.append(buttons)

    # 2) Tashqi havolalar (faqat http(s) qabul qilinadi)
    links: list[InlineKeyboardButton] = []
    shazam_url = safe_link(track.url)
    if shazam_url:
        links.append(InlineKeyboardButton(text="🔎 Shazam", url=shazam_url))
    apple = safe_link(
        track.listen_links.get("Apple Music") or track.listen_links.get("Applemusic")
    )
    if apple:
        links.append(InlineKeyboardButton(text="🍏 Apple Music", url=apple))
    if links:
        rows.append(links)

    rows.append([
        InlineKeyboardButton(
            text="▶️ YouTube",
            url=f"https://www.youtube.com/results?search_query={query}",
        ),
        InlineKeyboardButton(
            text="🟢 Spotify",
            url=f"https://open.spotify.com/search/{query}",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_track(track: Track) -> str:
    lines = [
        f"🎵 <b>{esc(track.title)}</b>",
        f"👤 {esc(track.artist)}",
    ]

    details = [esc(x) for x in (track.album, track.released, track.genre) if x]
    if details:
        lines.append("💿 " + " · ".join(details))

    lines += [
        "",
        f"📊 Ishonchlilik: <b>{track.confidence}</b> "
        f"({track.hits}/{max(track.attempts, track.hits)} moslik)",
        "",
        "👇 <b>Variantni tanlang:</b>",
    ]
    return "\n".join(lines)


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
@router.message(F.text & ~F.text.startswith("/"))
@router.message(F.caption)          # havola rasm/video izohida kelgan holat
async def on_link(message: Message) -> None:
    url = find_url(message.text or message.caption or "")
    if not url:
        await message.answer(texts.NO_URL)
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    if not security.is_allowed_user(user_id):
        await message.answer(texts.NOT_ALLOWED)
        return
    if user_id in _busy_users:
        await message.answer(texts.BUSY)
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

        # Audio darhol ajratiladi - tugma bosilganda javob tez bo'lishi uchun
        try:
            wav = await extract_audio(video.path, video.work_dir / "audio.wav")
            if wav_duration(wav) >= 1.0:
                job.audio_path = str(wav)
        except AudioError as exc:
            log.info("Audio ajratilmadi: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("Audio ajratishda kutilmagan xato")

        await storage.put(job)

        caption = f"🎬 <b>{esc(video.title[:200])}</b>"
        if video.uploader:
            caption += f"\n👤 {esc(video.uploader[:80])}"
        caption += f"\n\n{texts.CAPTION_HINT}"

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
async def send_result(message: Message, track: Track, token: str) -> None:
    text = format_track(track)
    keyboard = result_keyboard(track, token)
    cover = safe_link(track.cover)
    if cover:
        try:
            await message.answer_photo(
                URLInputFile(cover), caption=text, reply_markup=keyboard
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("Muqovani yuborib bo'lmadi: %s", exc)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


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
        await send_result(message, Track(**job.result), token)
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
        if total <= 0:                      # chuqurroq qidiruv bosqichi
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
    await send_result(message, track, token)


# --------------------------------------------------------------------------- #
# Audio variantlari: Original / Slowed / Reverb / Speed Up / Nightcore / Bass
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

    track = Track(**job.result)
    audio_title = f"{track.title}{variant.suffix}"
    caption = f"🎵 <b>{esc(audio_title)}</b>\n👤 {esc(track.artist)}"

    # 1) Avval yuborilgan bo'lsa - Telegram file_id orqali bir zumda qaytaramiz
    cached_id = job.audio_ids.get(key)
    if cached_id:
        try:
            await message.answer_audio(cached_id, caption=caption)
            await callback.answer("Tayyor ✅")
            return
        except TelegramAPIError as exc:
            log.info("Keshdagi audio yaroqsiz (%s), qaytadan tayyorlanadi", exc)
            job.audio_ids.pop(key, None)

    if token in _audio_busy:
        await callback.answer(texts.AUDIO_BUSY, show_alert=True)
        return
    warning = security.rate_check(_search_limiter, user_id)
    if warning:
        await callback.answer(warning[:190], show_alert=True)
        return

    work_dir = Path(job.work_dir)
    if not security.is_inside(work_dir, config.DOWNLOAD_DIR):
        await callback.answer()
        await message.answer(texts.EXPIRED)
        return

    _audio_busy.add(token)
    await callback.answer(f"{variant.label} tayyorlanmoqda...")
    status = await message.answer(texts.AUDIO_PREPARING.format(label=variant.label))
    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        # 2) Qo'shiqning to'liq audiosi (bir marta yuklanadi, keyin keshdan)
        song_path = Path(job.song_path) if job.song_path else None
        if song_path is None or not song_path.exists():
            await safe_edit(status, texts.AUDIO_SEARCHING)
            async with _download_sem:
                song = await download_song(
                    f"{track.artist} {track.title}", work_dir=work_dir
                )
            job.song_path = str(song.path)
            job.song_duration = song.duration
            await storage.update(job)
            song_path = song.path

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

        size_mb = out_path.stat().st_size / (1024 * 1024)
        if size_mb > config.MAX_UPLOAD_MB:
            await safe_edit(status, texts.AUDIO_TOO_BIG)
            return

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
            **kwargs,
        )
        if sent.audio:
            job.audio_ids[key] = sent.audio.file_id
            await storage.update(job)
        await safe_delete(status)

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
