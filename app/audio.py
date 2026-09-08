"""Videodan audio ajratish va tahlil uchun bo'laklar kesish (ffmpeg orqali)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .ffmpeg_setup import ensure_ffmpeg

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # Shazam algoritmi 16 kHz bilan ishlaydi
CHANNELS = 1
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * 2   # pcm_s16le
WAV_HEADER = 44


class AudioError(RuntimeError):
    pass


async def _run(args: list[str], timeout: int = 180) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AudioError("ffmpeg juda uzoq ishladi (timeout).") from None
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-5:]
        raise AudioError("ffmpeg xatosi: " + " | ".join(tail))


async def extract_audio(video_path: Path, out_path: Path) -> Path:
    """Videodan to'liq audio yo'lakchani WAV (16 kHz, mono) sifatida ajratadi."""
    ffmpeg = ensure_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path),
        "-vn", "-sn", "-dn",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "wav",
        str(out_path),
    ])
    if not out_path.exists() or out_path.stat().st_size <= WAV_HEADER:
        raise AudioError("Bu videoda audio yo'lakcha topilmadi.")
    return out_path


def wav_duration(path: Path) -> float:
    """PCM WAV uzunligi (soniya) — fayl hajmidan aniq hisoblanadi."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0.0
    return max(0.0, (size - WAV_HEADER) / BYTES_PER_SEC)


async def cut_clip(src_wav: Path, out_path: Path, start: float, length: float) -> Path:
    """WAV dan `start` soniyadan boshlab `length` soniyalik bo'lak kesadi."""
    ffmpeg = ensure_ffmpeg()
    await _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.2f}",
        "-t", f"{length:.2f}",
        "-i", str(src_wav),
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "wav",
        str(out_path),
    ], timeout=60)
    if not out_path.exists() or out_path.stat().st_size <= WAV_HEADER:
        raise AudioError("Audio bo'lagini kesib bo'lmadi.")
    return out_path


async def ensure_mp4(video_path: Path) -> Path:
    """Fayl mp4 bo'lmasa, uni qayta kodlamasdan mp4 ga o'raydi.

    Telegram webm/mkv videolarni ba'zan oddiy fayl sifatida ko'rsatadi.
    O'rash muvaffaqiyatsiz bo'lsa, asl fayl qaytariladi.
    """
    if video_path.suffix.lower() == ".mp4":
        return video_path
    ffmpeg = ensure_ffmpeg()
    out_path = video_path.with_name(video_path.stem + "_tg.mp4")
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)]

    # 1-usul: qayta kodlamasdan o'rash (tez)
    # 2-usul: H.264/AAC ga qayta kodlash (VP8/VP9 kabi kodeklar uchun)
    attempts = (
        (base + ["-c", "copy", "-movflags", "+faststart", str(out_path)], 120),
        (base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", str(out_path)], 300),
    )
    for args, timeout in attempts:
        try:
            await _run(args, timeout=timeout)
        except AudioError as exc:
            log.info("mp4 ga o'tkazish urinishi muvaffaqiyatsiz: %s", exc)
            out_path.unlink(missing_ok=True)
            continue
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path

    log.info("mp4 ga o'tkazib bo'lmadi, asl fayl yuboriladi: %s", video_path.name)
    return video_path


def plan_offsets(duration: float, clip: float, max_attempts: int) -> list[float]:
    """Qo'shiqni topish ehtimoli yuqori bo'lgan boshlanish nuqtalarini tanlaydi.

    Odatda reels boshida gap/shovqin bo'ladi, shuning uchun avval o'rta
    qismlardan tekshiramiz.
    """
    if duration <= clip + 1:
        return [0.0]

    usable = max(0.0, duration - clip)
    # Nuqtalar orasidagi eng kichik masofa: uzun videoda bo'laklar bir-birini
    # takrorlamasin, qisqa videoda esa urinishlar soni kamayib ketmasin.
    spread = max(1.5, min(clip / 2, usable / max(1, max_attempts - 1)))
    ratios = [0.30, 0.55, 0.10, 0.75, 0.42, 0.88, 0.00]
    offsets: list[float] = []
    for r in ratios:
        point = round(min(usable, usable * r), 2)
        if all(abs(point - existing) >= spread for existing in offsets):
            offsets.append(point)
        if len(offsets) >= max_attempts:
            break
    return offsets or [0.0]
