"""ffmpeg ni tayyorlaydi.

Tizimda ffmpeg bo'lmasa, `imageio-ffmpeg` paketi ichidagi binarni
`data/bin/ffmpeg(.exe)` nomi bilan nusxalaydi va PATH ga qo'shadi.
Shu tufayli foydalanuvchi ffmpeg ni qo'lda o'rnatishi shart emas.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from .config import BIN_DIR

log = logging.getLogger(__name__)

_EXE = ".exe" if sys.platform == "win32" else ""
_ffmpeg_path: str | None = None


def _copy_bundled() -> str | None:
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # noqa: BLE001
        log.warning("imageio-ffmpeg binarini olib bo'lmadi: %s", exc)
        return None
    if not src.exists():
        return None
    dst = BIN_DIR / f"ffmpeg{_EXE}"
    try:
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
            dst.chmod(0o755)
    except Exception as exc:  # noqa: BLE001
        log.warning("ffmpeg nusxalanmadi: %s", exc)
        return str(src)
    return str(dst)


def ensure_ffmpeg() -> str:
    """ffmpeg yo'lini qaytaradi va uning papkasini PATH ga qo'shadi."""
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path

    found = shutil.which("ffmpeg")
    if not found:
        found = _copy_bundled()
    if not found:
        raise RuntimeError(
            "ffmpeg topilmadi. `pip install imageio-ffmpeg` qiling "
            "yoki ffmpeg ni tizimga o'rnating."
        )

    folder = str(Path(found).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if folder not in path_parts:
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")

    # pydub/shazamio ba'zan shu o'zgaruvchilarga qaraydi
    os.environ.setdefault("FFMPEG_BINARY", found)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        os.environ.setdefault("FFPROBE_BINARY", ffprobe)

    _ffmpeg_path = found
    log.info("ffmpeg: %s", found)
    return found


def ffmpeg_dir() -> str:
    return str(Path(ensure_ffmpeg()).parent)
