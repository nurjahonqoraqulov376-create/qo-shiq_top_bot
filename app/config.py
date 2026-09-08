"""Bot sozlamalari (.env fayldan o'qiladi).

Barcha sonli sozlamalar xavfsiz o'qiladi: noto'g'ri qiymat yozilgan bo'lsa
bot ishdan chiqmaydi, standart qiymatga qaytadi va cheklovlar doirasida
ushlab turiladi.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("%s noto'g'ri qiymat (%r), %s ishlatiladi", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        log.warning("%s chegaradan tashqarida (%s), %s..%s oralig'iga keltirildi",
                    name, value, minimum, maximum)
    return max(minimum, min(maximum, value))


def _id_set(name: str) -> set[int]:
    """Vergul bilan ajratilgan Telegram ID ro'yxatini o'qiydi."""
    result: set[int] = set()
    for part in (os.getenv(name) or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            log.warning("%s ichida noto'g'ri ID: %r", name, part)
    return result


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
BIN_DIR = DATA_DIR / "bin"
COOKIES_FILE = Path(os.getenv("COOKIES_FILE") or (BASE_DIR / "cookies.txt"))
# Masalan: chrome | edge | firefox | auto  (Instagram login talab qilsa foydali)
COOKIES_FROM_BROWSER: str = os.getenv("COOKIES_FROM_BROWSER", "").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _materialize_cookies() -> Path | None:
    """Serverda cookie'larni muhit o'zgaruvchisidan faylga yozadi.

    Railway/VPS'da brauzer yo'q, shuning uchun cookies.txt ni to'g'ridan-to'g'ri
    sozlamaga joylash kerak bo'ladi:
      * COOKIES_B64 - base64 ga o'girilgan cookies.txt (tavsiya etiladi);
      * COOKIES_TXT - cookies.txt ning o'zi (ko'p qatorli qiymat).
    """
    encoded = os.getenv("COOKIES_B64", "").strip()
    plain = os.getenv("COOKIES_TXT", "")
    data: bytes | None = None

    if encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            log.warning("COOKIES_B64 base64 emas - e'tiborsiz qoldirildi")
    elif plain.strip():
        data = plain.encode("utf-8")

    if not data:
        return None

    path = DATA_DIR / "cookies_env.txt"
    try:
        path.write_bytes(data)
        path.chmod(0o600)
    except OSError as exc:
        log.warning("Cookie fayli yozilmadi: %s", exc)
        return None
    log.info("Cookie'lar sozlamadan o'qildi (%d bayt)", len(data))
    return path


_ENV_COOKIES = _materialize_cookies()
if _ENV_COOKIES is not None and not COOKIES_FILE.exists():
    COOKIES_FILE = _ENV_COOKIES

# Telegram bot API orqali yuborish chegarasi (50 MB), biroz zaxira qoldiramiz
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 48, 1, 49)
MAX_VIDEO_HEIGHT = _int("MAX_VIDEO_HEIGHT", 720, 144, 2160)
# Diskka yuklashning qattiq chegarasi (Telegram chegarasidan katta bo'lishi mumkin:
# katta video bo'lsa ham qo'shig'ini topib berish uchun)
MAX_FILESIZE_MB = _int("MAX_FILESIZE_MB", 200, 10, 2000)

# Qo'shiq qidirishda tahlil qilinadigan bo'lak uzunligi va urinishlar soni
CLIP_SECONDS = _int("CLIP_SECONDS", 12, 5, 30)
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 5, 1, 12)

# Vaqtinchalik fayllar necha soniyadan keyin o'chirilsin
FILE_TTL_SECONDS = _int("FILE_TTL_SECONDS", 7200, 300, 604800)

# Bir vaqtda nechta yuklab olish ishlasin
MAX_CONCURRENT_DOWNLOADS = _int("MAX_CONCURRENT_DOWNLOADS", 3, 1, 20)

# Bitta yuklashga ajratilgan maksimal vaqt (soniya) va video uzunligi (daqiqa)
DOWNLOAD_TIMEOUT = _int("DOWNLOAD_TIMEOUT", 300, 30, 3600)
MAX_DURATION_MIN = _int("MAX_DURATION_MIN", 30, 1, 600)

# Qo'shiq audiosini yuklash chegaralari (slowed/reverb variantlari uchun)
SONG_MAX_MB = _int("SONG_MAX_MB", 60, 5, 300)
SONG_MAX_MIN = _int("SONG_MAX_MIN", 12, 1, 60)

# Shazam'ga bir vaqtda nechta so'rov yuborilsin (IP bloklanmasligi uchun)
MAX_CONCURRENT_RECOGNITIONS = _int("MAX_CONCURRENT_RECOGNITIONS", 2, 1, 10)

# --------------------------------------------------------------------------- #
# Xavfsizlik sozlamalari
# --------------------------------------------------------------------------- #
# Bo'sh bo'lsa - bot hamma uchun ochiq. Aks holda faqat shu ID'lar foydalanadi.
ALLOWED_USERS: set[int] = _id_set("ALLOWED_USERS")
# Xato haqida xabar oladigan egalar (ixtiyoriy)
ADMIN_USERS: set[int] = _id_set("ADMIN_USERS")

# Spamga qarshi: bitta foydalanuvchi uchun soatlik limit va pauza (soniya)
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 20, 1, 1000)
RATE_LIMIT_COOLDOWN = _int("RATE_LIMIT_COOLDOWN", 5, 0, 600)

# Vaqtinchalik fayllar egallashi mumkin bo'lgan maksimal joy (MB)
MAX_DISK_MB = _int("MAX_DISK_MB", 2048, 100, 500000)

# Ixtiyoriy: ACRCloud (Shazam topa olmagan holatlar uchun zaxira)
ACR_HOST = os.getenv("ACR_HOST", "").strip()
ACR_ACCESS_KEY = os.getenv("ACR_ACCESS_KEY", "").strip()
ACR_ACCESS_SECRET = os.getenv("ACR_ACCESS_SECRET", "").strip()
ACR_ENABLED = bool(ACR_HOST and ACR_ACCESS_KEY and ACR_ACCESS_SECRET)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
BIN_DIR.mkdir(parents=True, exist_ok=True)
