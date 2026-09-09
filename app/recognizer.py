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
from .audio import SAMPLE_RATE, cut_clip, plan_offsets, wav_duration

log = logging.getLogger(__name__)

# Past yoki shovqinli yozuvlar uchun: quyi shovqinni kesib, ovozni tenglashtirish
ENHANCE_FILTER = "highpass=f=90,dynaudnorm=p=0.9:s=5"

# Reels/TikTok'da musiqa ko'pincha tezlashtirilgan (yoki sekinlashtirilgan)
# bo'ladi. Shazam bunday audioni tanimaydi, shuning uchun tezlikni qaytaramiz.
# Koeffitsient - asl tezlikka qaytarish uchun ko'paytiruvchi.
SPEED_FIXES: tuple[float, ...] = (0.80, 0.87, 1.15, 1.25)


def speed_filter(factor: float) -> str:
    """Audio tezligini (va tovush balandligini) `factor` marta o'zgartiradi."""
    return f"asetrate={SAMPLE_RATE}*{factor:.3f},aresample={SAMPLE_RATE}"


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
    preview: str | None = None      # Apple'ning ~30s audio parchasi (zaxira manba)
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

    # Apple preview - YouTube bloklangan holatda zaxira audio manbasi
    hub = track.get("hub") or {}
    preview = None
    for action in hub.get("actions") or []:
        uri = (action.get("uri") or "").strip()
        if uri.startswith("https://") and ".m4a" in uri:
            preview = uri
            break

    links: dict[str, str] = {}
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
        preview=preview,
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
    """Audio fayldan qo'shiqni aniqlaydi. Topilmasa None qaytaradi.

    Bir marta tinglab qo'ya qolmaydi - topilmasa bosqichma-bosqich
    qiyinlashtirib qayta tinglaydi:

    1. oddiy bo'laklar (tez);
    2. ovozi kuchaytirilgan, uzunroq bo'laklar (past/shovqinli yozuvlar uchun);
    3. tezligi qaytarilgan bo'laklar (reels'da musiqa ko'pincha tezlashtirilgan
       yoki sekinlashtirilgan bo'ladi - Shazam bunday audioni tanimaydi);
    4. bitta uzun bo'lak;
    5. ACRCloud (agar sozlangan bo'lsa).

    `on_progress(step, total)`: total > 0 - oddiy qadam, 0 - chuqur tinglash,
    -1 - tezlikni tekshirish bosqichi.
    """
    duration = wav_duration(wav_path)
    if duration < 1.0:
        return None

    clip_len = float(config.CLIP_SECONDS)
    offsets = plan_offsets(duration, clip_len, config.MAX_ATTEMPTS)
    clips_dir = wav_path.parent / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    votes: dict[str, Track] = {}
    attempts = 0

    async def note(step: int, total: int) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(step, total)
        except Exception:  # noqa: BLE001
            pass

    async def probe(
        offset: float,
        length: float,
        audio_filter: str | None,
        name: str,
        timeout: float = 45,
    ) -> Track | None:
        """Bitta bo'lakni tinglaydi. Ikkinchi bir xil natija chiqsa - tasdiq."""
        nonlocal attempts
        attempts += 1
        clip_path = clips_dir / f"{name}.wav"
        try:
            span = min(length, max(1.0, duration - offset))
            await cut_clip(wav_path, clip_path, offset, span, audio_filter)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bo'lak kesilmadi (%s): %s", name, exc)
            return None

        track = _parse_shazam(await _shazam_recognize(clip_path, timeout=timeout))
        clip_path.unlink(missing_ok=True)
        if track is None:
            return None

        existing = votes.get(track.key)
        if existing is None:
            votes[track.key] = track
            return None
        existing.hits += 1
        return existing if existing.hits >= 2 else None

    def best_so_far() -> Track | None:
        if not votes:
            return None
        best = max(votes.values(), key=lambda t: t.hits)
        best.attempts = attempts
        return best

    # 1-bosqich: oddiy bo'laklar
    for index, offset in enumerate(offsets, start=1):
        await note(index, len(offsets))
        found = await probe(offset, clip_len, None, f"a{index}")
        if found:
            found.attempts = attempts
            return found
        if index < len(offsets):
            await asyncio.sleep(0.7)      # Shazam limitlariga hurmat
    if votes:
        return best_so_far()

    # 2-bosqich: ovozni kuchaytirib, uzunroq bo'laklar bilan qayta tinglaymiz
    await note(0, 0)
    for index, offset in enumerate(offsets[:3], start=1):
        found = await probe(offset, clip_len * 1.6, ENHANCE_FILTER, f"b{index}")
        if found:
            found.attempts = attempts
            return found
        await asyncio.sleep(0.7)
    if votes:
        return best_so_far()

    # 3-bosqich: tezligi kuchli o'zgartirilgan musiqa.
    # Diqqat: bu bosqichda tovush balandligi sun'iy o'zgargani uchun noto'g'ri
    # moslik chiqishi mumkin, shuning uchun natija faqat ikki marta
    # takrorlansa qabul qilinadi.
    if duration >= 6.0:
        await note(0, -1)
        speed_offsets = offsets[:2] or [0.0]
        for factor in SPEED_FIXES:
            for index, offset in enumerate(speed_offsets, start=1):
                name = f"c{str(factor).replace('.', '')}_{index}"
                found = await probe(offset, clip_len * 1.3, speed_filter(factor), name)
                if found:
                    log.info("Tezligi %.2fx ga qaytarilgandan keyin topildi", factor)
                    found.attempts = attempts
                    return found
                await asyncio.sleep(0.5)

    # 4-bosqich: bitta uzun bo'lak (shazamio uni o'zi qismlarga bo'ladi)
    if duration > 3.0:
        await note(0, 0)
        found = await probe(
            max(0.0, (duration - min(60.0, duration)) / 2),
            min(60.0, duration),
            None,
            "long",
            timeout=120,
        )
        if found:
            found.attempts = attempts
            return found

    # Tasdiqlanmagan bo'lsa ham, bitta moslik bo'lsa - shuni qaytaramiz
    if votes:
        return best_so_far()

    # 5-bosqich: ACRCloud zaxirasi (agar sozlangan bo'lsa)
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


# --------------------------------------------------------------------------- #
# Nom bo'yicha qidirish (havolasiz)
# --------------------------------------------------------------------------- #
_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


async def search_by_name(query: str) -> Track | None:
    """Qo'shiq nomi bo'yicha ma'lumot topadi (Apple/iTunes katalogi).

    Shazam'ning katalog qidiruvi ishlamay qolgani uchun iTunes API
    ishlatiladi: u bepul, kalitsiz va muqova hamda audio parchani ham beradi.
    """
    import aiohttp

    clean = " ".join((query or "").split())[:120]
    if len(clean) < 2:
        return None

    params = {"term": clean, "entity": "song", "limit": "1"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_ITUNES_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    log.info("iTunes qidiruvi javob bermadi: HTTP %s", resp.status)
                    return None
                payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("iTunes qidiruvida xato: %s", exc)
        return None

    results = (payload or {}).get("results") or []
    if not results:
        return None
    item = results[0]

    title = (item.get("trackName") or "").strip()
    artist = (item.get("artistName") or "").strip()
    if not title:
        return None

    cover = (item.get("artworkUrl100") or "").replace("100x100", "600x600") or None
    released = (item.get("releaseDate") or "")[:4] or None
    return Track(
        title=title,
        artist=artist or "Noma'lum ijrochi",
        key=f"itunes|{item.get('trackId') or title}".lower(),
        album=(item.get("collectionName") or "").strip() or None,
        released=released,
        genre=(item.get("primaryGenreName") or "").strip() or None,
        cover=cover,
        url=item.get("trackViewUrl"),
        preview=item.get("previewUrl"),
        source="Apple Music",
    )
