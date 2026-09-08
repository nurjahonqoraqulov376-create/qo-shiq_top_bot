r"""Botsiz tekshiruv: havoladan video -> audio -> qo'shiq.

Ishlatish:
    .\.venv\Scripts\python.exe selftest.py "https://www.youtube.com/shorts/XXXX"
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from app.audio import extract_audio, wav_duration
from app.downloader import download
from app.ffmpeg_setup import ensure_ffmpeg
from app.recognizer import identify


async def main(url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")

    print("1) ffmpeg tekshirilmoqda...")
    print("   ->", ensure_ffmpeg())

    print("2) Video yuklanmoqda...")
    video = await download(url)
    print(f"   -> {video.path.name} | {video.size_mb:.1f} MB | "
          f"{video.width}x{video.height} | {video.duration:.0f}s")
    print(f"   -> nomi: {video.title}")

    try:
        print("3) Audio ajratilmoqda...")
        wav = await extract_audio(video.path, video.work_dir / "audio.wav")
        print(f"   -> {wav.name} | {wav_duration(wav):.1f}s")

        print("4) Qo'shiq qidirilmoqda...")

        async def progress(step: int, total: int) -> None:
            print(f"   ... urinish {step}/{total}")

        track = await identify(wav, on_progress=progress)
        if track is None:
            print("\nNATIJA: qo'shiq TOPILMADI")
            return 1

        print("\nNATIJA:")
        print(f"   Qo'shiq   : {track.title}")
        print(f"   Ijrochi   : {track.artist}")
        print(f"   Albom     : {track.album}")
        print(f"   Chiqqan   : {track.released}")
        print(f"   Janr      : {track.genre}")
        print(f"   Manba     : {track.source} ({track.hits}/{track.attempts} moslik,"
              f" ishonch: {track.confidence})")
        print(f"   Shazam    : {track.url}")
        print(f"   Muqova    : {track.cover}")
        print(f"   Havolalar : {track.listen_links}")
        return 0
    finally:
        shutil.rmtree(video.work_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1])))
