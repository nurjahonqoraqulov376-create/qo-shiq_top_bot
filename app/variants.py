"""Qo'shiq variantlari: slowed, speed up, slowed + reverb, bass boost.

Topilgan qo'shiqning to'liq audiosi yuklab olinadi va ffmpeg filtrlari
yordamida turli variantlar tayyorlanadi.

`asetrate` sample rate'ni o'zgartiradi - bu tezlikni ham, tovush balandligini
(pitch) ham o'zgartiradi. Aynan shu narsa "slowed" va "nightcore" effektini
beradi. Manba audiosi doim 44100 Hz ga keltirilgani uchun natija bashorat
qilinadigan bo'ladi.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .audio import AudioError, run_ffmpeg
from .ffmpeg_setup import ensure_ffmpeg

log = logging.getLogger(__name__)

RATE = 44100


@dataclass(frozen=True, slots=True)
class Variant:
    key: str
    label: str          # tugmadagi yozuv
    suffix: str         # fayl nomiga qo'shiladi
    audio_filter: str | None
    note: str           # xabardagi izoh
    speed: float = 1.0  # davomiylikni hisoblash uchun

    @property
    def is_original(self) -> bool:
        return self.audio_filter is None


VARIANTS: dict[str, Variant] = {
    "orig": Variant(
        key="orig",
        label="🎧 Original",
        suffix="",
        audio_filter=None,
        note="original",
        speed=1.0,
    ),
    "slow": Variant(
        key="slow",
        label="🐌 Slowed",
        suffix=" (slowed)",
        audio_filter=f"asetrate={RATE}*0.86,aresample={RATE},atempo=1.0",
        note="slowed",
        speed=0.86,
    ),
    "reverb": Variant(
        key="reverb",
        label="🌊 Slowed + Reverb",
        suffix=" (slowed + reverb)",
        audio_filter=(
            f"asetrate={RATE}*0.85,aresample={RATE},"
            "aecho=0.85:0.9:35|55|85:0.4|0.28|0.18"
        ),
        note="slowed + reverb",
        speed=0.85,
    ),
    "fast": Variant(
        key="fast",
        label="⚡ Speed Up",
        suffix=" (sped up)",
        audio_filter=f"asetrate={RATE}*1.22,aresample={RATE}",
        note="sped up",
        speed=1.22,
    ),
    "night": Variant(
        key="night",
        label="🌙 Nightcore",
        suffix=" (nightcore)",
        audio_filter=f"asetrate={RATE}*1.30,aresample={RATE},atempo=1.05",
        note="nightcore",
        speed=1.365,
    ),
    "bass": Variant(
        key="bass",
        label="🔊 Bass Boost",
        suffix=" (bass boosted)",
        audio_filter="bass=g=12:f=90:w=0.7,alimiter=limit=0.95",
        note="bass boosted",
        speed=1.0,
    ),
}

# Tugmalar joylashuvi (har bir ichki ro'yxat - bitta qator)
KEYBOARD_ROWS: tuple[tuple[str, ...], ...] = (
    ("orig", "slow"),
    ("reverb", "fast"),
    ("night", "bass"),
)


def get(key: str) -> Variant | None:
    return VARIANTS.get(key)


async def make_variant(source: Path, out_path: Path, variant: Variant) -> Path:
    """Manba audiodan variant tayyorlaydi."""
    ffmpeg = ensure_ffmpeg()
    # Manba 48 kHz bo'lishi ham mumkin - shuning uchun avval 44100 ga
    # keltiramiz, aks holda `asetrate` sekinlashtirish darajasi o'zgarib ketadi.
    chain = [f"aresample={RATE}"]
    if variant.audio_filter:
        chain.append(variant.audio_filter)

    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-af", ",".join(chain),
    ]
    args += [
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-ar", str(RATE), "-ac", "2",
        "-map_metadata", "-1",
        str(out_path),
    ]
    await run_ffmpeg(args, timeout=300)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise AudioError("Variant tayyorlanmadi.")
    return out_path


async def prepare_thumbnail(image_path: Path, out_path: Path) -> Path | None:
    """Muqovani Telegram talab qiladigan ko'rinishga keltiradi (320x320 JPEG)."""
    ffmpeg = ensure_ffmpeg()
    try:
        await run_ffmpeg([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(image_path),
            "-vf", "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
            "-q:v", "4",
            str(out_path),
        ], timeout=60)
    except AudioError as exc:
        log.info("Muqova tayyorlanmadi: %s", exc)
        return None
    if not out_path.exists() or out_path.stat().st_size == 0:
        return None
    # Telegram thumbnail chegarasi - 200 KB
    if out_path.stat().st_size > 200 * 1024:
        log.info("Muqova juda katta, o'tkazib yuborildi")
        out_path.unlink(missing_ok=True)
        return None
    return out_path
