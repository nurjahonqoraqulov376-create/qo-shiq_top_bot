"""Yuklangan videolar haqidagi ma'lumotni saqlash (tugma bosilganda kerak bo'ladi).

Ma'lumot xotirada saqlanadi va JSON faylga yozib boriladi - shu tufayli bot
qayta ishga tushsa ham eski tugmalar ishlashda davom etadi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DATA_DIR, DOWNLOAD_DIR, FILE_TTL_SECONDS
from .security import is_inside

log = logging.getLogger(__name__)

_INDEX_FILE = DATA_DIR / "jobs.json"
_lock = asyncio.Lock()
_jobs: dict[str, "Job"] = {}


@dataclass
class Job:
    token: str
    user_id: int
    chat_id: int
    url: str
    title: str
    work_dir: str
    audio_path: str | None = None
    created_at: float = field(default_factory=time.time)
    result: dict | None = None          # topilgan qo'shiq (keshlanadi)
    not_found: bool = False             # qidirildi, lekin topilmadi
    song_path: str | None = None        # qo'shiqning to'liq audiosi
    song_duration: float = 0.0
    song_is_preview: bool = False   # faqat 30 soniyalik parcha bo'lsa
    thumb_path: str | None = None       # muqova (Telegram uchun 320x320)
    # variant kaliti -> Telegram file_id (qayta yuborishda tez ishlaydi)
    audio_ids: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > FILE_TTL_SECONDS


def _safe_rmtree(path: str | None) -> None:
    """Faqat yuklamalar papkasi ichidagi papkani o'chiradi.

    Yozuv fayli buzilgan yoki qo'lda o'zgartirilgan bo'lsa ham, bot
    tashqaridagi papkani o'chirib yubormasligi kerak.
    """
    if not path:
        return
    if not is_inside(path, DOWNLOAD_DIR):
        log.error("Xavfsizlik: papkani o'chirish rad etildi (%s)", path)
        return
    shutil.rmtree(path, ignore_errors=True)


def disk_usage_mb() -> float:
    """Vaqtinchalik fayllar egallagan joy (MB)."""
    total = 0
    if not DOWNLOAD_DIR.exists():
        return 0.0
    for item in DOWNLOAD_DIR.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def new_token() -> str:
    return uuid.uuid4().hex[:16]


def _save_unlocked() -> None:
    try:
        tmp = _INDEX_FILE.with_suffix(".tmp")
        payload = {token: asdict(job) for token, job in _jobs.items()}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _INDEX_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs.json saqlanmadi: %s", exc)


def load() -> None:
    """Bot ishga tushganda eski yozuvlarni tiklaydi."""
    if not _INDEX_FILE.exists():
        return
    try:
        raw = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs.json o'qilmadi: %s", exc)
        return
    known = {f.name for f in Job.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    for token, data in (raw or {}).items():
        try:
            job = Job(**{k: v for k, v in data.items() if k in known})
        except Exception:  # noqa: BLE001
            continue
        # Yozuv buzilgan bo'lsa (yo'l boshqa joyni ko'rsatsa) - qabul qilmaymiz
        if not is_inside(job.work_dir, DOWNLOAD_DIR):
            log.warning("Shubhali yozuv o'tkazib yuborildi: %s", job.work_dir)
            continue
        if job.audio_path and not is_inside(job.audio_path, DOWNLOAD_DIR):
            job.audio_path = None
        if job.song_path and not is_inside(job.song_path, DOWNLOAD_DIR):
            job.song_path = None
        if job.thumb_path and not is_inside(job.thumb_path, DOWNLOAD_DIR):
            job.thumb_path = None
        if not isinstance(job.audio_ids, dict):
            job.audio_ids = {}
        _jobs[token] = job
    log.info("%d ta eski yozuv tiklandi", len(_jobs))


async def put(job: Job) -> None:
    async with _lock:
        _jobs[job.token] = job
        _save_unlocked()


async def get(token: str) -> Job | None:
    async with _lock:
        return _jobs.get(token)


async def update(job: Job) -> None:
    async with _lock:
        _jobs[job.token] = job
        _save_unlocked()


async def drop(token: str, remove_files: bool = True) -> None:
    async with _lock:
        job = _jobs.pop(token, None)
        _save_unlocked()
    if job and remove_files:
        _safe_rmtree(job.work_dir)


async def cleanup_once() -> int:
    """Muddati o'tgan yozuv va fayllarni o'chiradi."""
    async with _lock:
        expired = [token for token, job in _jobs.items() if job.expired]
        dirs = [_jobs[token].work_dir for token in expired]
        for token in expired:
            _jobs.pop(token, None)
        if expired:
            _save_unlocked()
        active = {Path(job.work_dir).resolve() for job in _jobs.values()}

    for path in dirs:
        _safe_rmtree(path)

    # Indeksda yo'q, lekin diskda qolib ketgan papkalarni ham tozalaymiz
    now = time.time()
    for folder in DOWNLOAD_DIR.iterdir() if DOWNLOAD_DIR.exists() else []:
        try:
            if not folder.is_dir() or folder.resolve() in active:
                continue
            if now - folder.stat().st_mtime > FILE_TTL_SECONDS:
                _safe_rmtree(str(folder))
        except OSError:
            continue
    return len(expired)


async def cleanup_loop(interval: int = 600, on_tick=None) -> None:
    """Davriy tozalash. `on_tick` - har safar chaqiriladigan qo'shimcha vazifa."""
    while True:
        try:
            removed = await cleanup_once()
            if removed:
                log.info("Tozalandi: %d ta yozuv", removed)
            if on_tick is not None:
                on_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Tozalashda xato: %s", exc)
        await asyncio.sleep(interval)
