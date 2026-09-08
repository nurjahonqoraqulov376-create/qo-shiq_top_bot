"""Qo'shiqni aniqlash: Shazam (asosiy) + ACRCloud (ixtiyoriy zaxira).

Aniqlikni oshirish uchun audio bir nechta joyidan (o'rtasi, boshi, oxiri)
kesib olinadi va natijalar ovoz berish (voting) usulida solishtiriladi.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .audio import cut_clip, plan_offsets, wav_duration

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Track:
    title: str
    artist: str
    key: str = ""
    album: str | None = None
    released: str | None = None
    genre: str | None = None
    cover: str | None = None
    url: str | None = None
    source: str = "Shazam"
    hits: int = 1
    attempts: int = 0
    listen_links: dict[str, str] = field(default_factory=dict)

    @property
    def confidence(self) -> str:
        if self.hits >= 3:
            return "juda yuqori"
        if self.hits == 2:
            return "yuqori"
        return "o'rtacha"


# --------------------------------------------------------------------------- #
# Shazam
# --------------------------------------------------------------------------- #
_shazam = None
_shazam_lock = asyncio.Lock()
# Bir vaqtda ketadigan so'rovlar soni - Shazam IP'ni bloklamasligi uchun
_shazam_sem = asyncio.Semaphore(config.MAX_CONCURRENT_RECOGNITIONS)


async def _get_shazam():
    global _shazam
    if _shazam is None:
        async with _shazam_lock:
            if _shazam is None:
                from shazamio import Shazam  # sekin import - faqat kerak bo'lganda

                _shazam = Shazam(language="en-US", endpoint_country="GB")
    return _shazam


async def _shazam_recognize(clip: Path, timeout: float = 45) -> dict | None:
    shazam = await _get_shazam()
    recognize = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song")
    try:
        async with _shazam_sem:
            return await asyncio.wait_for(recognize(str(clip)), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Shazam javob bermadi (timeout): %s", clip.name)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Shazam xatosi (%s): %s", clip.name, exc)
        return None


def _parse_shazam(data: dict | None) -> Track | None:
    if not data:
        return None
    track = data.get("track") or {}
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None

    album = released = None
    for section in track.get("sections") or []:
        if section.get("type") != "SONG":
            continue
        for item in section.get("metadata") or []:
            name = (item.get("title") or "").lower()
            text = (item.get("text") or "").strip()
            if name == "album":
                album = text
            elif name == "released":
                released = text
            elif name == "label" and not album:
                album = text
    genre = ((track.get("genres") or {}).get("primary") or "").strip() or None

    images = track.get("images") or {}
    cover = (
        images.get("coverarthq")
        or images.get("coverart")
        or (track.get("share") or {}).get("image")
    )

    links: dict[str, str] = {}
    hub = track.get("hub") or {}
    for provider in hub.get("providers") or []:
        name = (provider.get("type") or "").strip()
        actions = provider.get("actions") or []
        uri = next(
            (a.get("uri") for a in actions if (a.get("uri") or "").startswith("http")),
            None,
        )
        if name and uri:
            links.setdefault(name.title(), uri)
    for option in hub.get("options") or []:
        for action in option.get("actions") or []:
            uri = action.get("uri") or ""
            if uri.startswith("https://music.apple.com"):
                links.setdefault("Apple Music", uri)

    return Track(
        title=title,
        artist=artist or "Noma'lum ijrochi",
        key=str(track.get("key") or f"{title}|{artist}").lower(),
        album=album,
        released=released,
        genre=genre,
        cover=cover,
        url=track.get("url") or (track.get("share") or {}).get("href"),
        source="Shazam",
        listen_links=links,
    )


# --------------------------------------------------------------------------- #
# ACRCloud (ixtiyoriy zaxira)
# --------------------------------------------------------------------------- #
async def _acrcloud_recognize(clip: Path) -> Track | None:
    if not config.ACR_ENABLED:
        return None
    import aiohttp

    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))
    string_to_sign = "\n".join(
        [
            http_method,
            http_uri,
            config.ACR_ACCESS_KEY,
            data_type,
            signature_version,
            timestamp,
        ]
    )
    signature = base64.b64encode(
        hmac.new(
            config.ACR_ACCESS_SECRET.encode("ascii"),
            string_to_sign.encode("ascii"),
            digestmod=hashlib.sha1,
        ).digest()
    ).decode("ascii")

    sample = clip.read_bytes()
    form = aiohttp.FormData()
    form.add_field("sample", sample, filename="sample.wav", content_type="audio/wav")
    form.add_field("sample_bytes", str(len(sample)))
    form.add_field("access_key", config.ACR_ACCESS_KEY)
    form.add_field("data_type", data_type)
    form.add_field("signature_version", signature_version)
    form.add_field("signature", signature)
    form.add_field("timestamp", timestamp)

    url = f"https://{config.ACR_HOST}{http_uri}"
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form) as resp:
                payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("ACRCloud xatosi: %s", exc)
        return None

    if (payload.get("status") or {}).get("code") != 0:
        return None
    musics = (payload.get("metadata") or {}).get("music") or []
    if not musics:
        return None
    music = musics[0]
    artists = ", ".join(a.get("name", "") for a in music.get("artists") or [])
    album = (music.get("album") or {}).get("name")
    acr_id = music.get("acrid") or music.get("title") or ""
    return Track(
        title=(music.get("title") or "").strip() or "Noma'lum",
        artist=artists or "Noma'lum ijrochi",
        key=f"acr|{acr_id}".lower(),
        album=album,
        released=music.get("release_date"),
        genre=next((g.get("name") for g in music.get("genres") or []), None),
        source="ACRCloud",
    )


# --------------------------------------------------------------------------- #
# Asosiy funksiya
# --------------------------------------------------------------------------- #
async def identify(wav_path: Path, on_progress=None) -> Track | None:
    """Audio fayldan qo'shiqni aniqlaydi. Topilmasa None qaytaradi."""
    duration = wav_duration(wav_path)
    if duration < 1.0:
        return None

    clip_len = float(config.CLIP_SECONDS)
    offsets = plan_offsets(duration, clip_len, config.MAX_ATTEMPTS)
    clips_dir = wav_path.parent / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    votes: dict[str, Track] = {}
    attempts = 0

    for index, offset in enumerate(offsets, start=1):
        attempts = index
        if on_progress:
            try:
                await on_progress(index, len(offsets))
            except Exception:  # noqa: BLE001
                pass

        clip_path = clips_dir / f"clip_{index}.wav"
        try:
            length = min(clip_len, max(1.0, duration - offset))
            await cut_clip(wav_path, clip_path, offset, length)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bo'lak kesilmadi (%.1fs): %s", offset, exc)
            continue

        track = _parse_shazam(await _shazam_recognize(clip_path))
        clip_path.unlink(missing_ok=True)

        if track:
            existing = votes.get(track.key)
            if existing:
                existing.hits += 1
                if existing.hits >= 2:  # ikki bo'lak bir xil natija berdi
                    existing.attempts = attempts
                    return existing
            else:
                votes[track.key] = track

        if index < len(offsets):
            await asyncio.sleep(0.7)  # Shazam limitlariga hurmat

    if votes:
        best = max(votes.values(), key=lambda t: t.hits)
        best.attempts = attempts
        return best

    # Oxirgi urinish: kattaroq bo'lak (shazamio uni o'zi qismlarga bo'lib qidiradi)
    if duration > 3.0:
        if on_progress:
            try:
                await on_progress(0, 0)   # 0, 0 = "chuqurroq qidiruv" belgisi
            except Exception:  # noqa: BLE001
                pass
        long_clip = clips_dir / "long.wav"
        try:
            length = min(60.0, duration)
            start = max(0.0, (duration - length) / 2)
            await cut_clip(wav_path, long_clip, start, length)
            track = _parse_shazam(await _shazam_recognize(long_clip, timeout=120))
            long_clip.unlink(missing_ok=True)
            if track:
                track.attempts = attempts + 1
                return track
        except Exception as exc:  # noqa: BLE001
            log.warning("Uzun bo'lak bo'yicha qidiruv o'tkazib yuborildi: %s", exc)

    # Shazam topa olmadi - ACRCloud zaxirasi (agar sozlangan bo'lsa)
    if config.ACR_ENABLED:
        clip_path = clips_dir / "acr.wav"
        try:
            await cut_clip(wav_path, clip_path, offsets[0], min(clip_len, duration))
            track = await _acrcloud_recognize(clip_path)
            clip_path.unlink(missing_ok=True)
            if track:
                track.attempts = attempts + 1
                return track
        except Exception as exc:  # noqa: BLE001
            log.warning("ACRCloud bosqichi o'tkazib yuborildi: %s", exc)

    return None
