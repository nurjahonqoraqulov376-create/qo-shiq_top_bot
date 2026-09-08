"""Xavfsizlik qatlami.

Bu modul botni quyidagilardan himoya qiladi:

* **SSRF** - foydalanuvchi ichki tarmoq manzilini (127.0.0.1, 192.168.x.x,
  bulut metadata serveri 169.254.169.254 va h.k.) yuborib, botni o'sha
  manzilga so'rov yuborishga majburlashi;
* **Spam / DoS** - bitta odam cheksiz so'rov yuborib, diskni va internetni
  band qilib qo'yishi;
* **Yo'l manipulyatsiyasi (path traversal)** - saqlangan yozuvdagi fayl
  yo'li buzilgan bo'lsa, tashqaridagi papka o'chib ketishi;
* **Zararli havolalar** - tugmalarga `javascript:` kabi havola tushishi.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from . import config

log = logging.getLogger(__name__)

# Faqat oddiy veb portlariga ruxsat (ichki xizmatlar odatda boshqa portlarda)
_ALLOWED_PORTS = {80, 443}
_ALLOWED_SCHEMES = {"http", "https"}
# Ichki tarmoqqa ishora qiluvchi nomlar
_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localdomain", ".onion", ".home")
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}

MAX_URL_LENGTH = 2048


class SecurityError(Exception):
    """Foydalanuvchiga ko'rsatiladigan xavfsizlik xatosi."""


# --------------------------------------------------------------------------- #
# 1. Havolani tekshirish (SSRF himoyasi)
# --------------------------------------------------------------------------- #
def _is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # IPv6 ichiga o'ralgan IPv4 (::ffff:127.0.0.1) ni ochib tekshiramiz
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def validate_url(url: str) -> str:
    """Havolani tekshiradi. Xavfli bo'lsa SecurityError ko'taradi."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise SecurityError("Havola juda uzun yoki bo'sh.")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SecurityError("Havolani o'qib bo'lmadi.") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise SecurityError("Faqat http:// yoki https:// havolalar qabul qilinadi.")

    if parts.username or parts.password:
        raise SecurityError("Ichida parol bor havolalar qabul qilinmaydi.")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SecurityError("Havolada sayt manzili yo'q.")

    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise SecurityError("Ichki tarmoq manzillariga ruxsat berilmagan.")

    try:
        port = parts.port
    except ValueError as exc:
        raise SecurityError("Havoladagi port noto'g'ri.") from exc
    if port is not None and port not in _ALLOWED_PORTS:
        raise SecurityError("Faqat 80 va 443 portlariga ruxsat berilgan.")

    # IP manzil to'g'ridan-to'g'ri yozilgan bo'lsa
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        pass  # domen nomi - quyida DNS orqali tekshiramiz
    else:
        if not _is_public_ip(literal):
            raise SecurityError("Ichki tarmoq manzillariga ruxsat berilmagan.")
        return url

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM), timeout=10
        )
    except asyncio.TimeoutError as exc:
        raise SecurityError("Sayt manzilini aniqlab bo'lmadi (DNS javob bermadi).") from exc
    except socket.gaierror as exc:
        raise SecurityError("Bunday sayt topilmadi. Havolani tekshiring.") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise SecurityError("Sayt manzilini aniqlab bo'lmadi.")
    for address in addresses:
        if not _is_public_ip(address):
            log.warning("SSRF urinishi bloklandi: %s -> %s", host, address)
            raise SecurityError("Ichki tarmoq manzillariga ruxsat berilmagan.")
    return url


def safe_link(url: str | None) -> str | None:
    """Tugmaga qo'yish uchun havolani tekshiradi (faqat http/https)."""
    if not url or len(url) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.hostname:
        return None
    return url


# --------------------------------------------------------------------------- #
# 2. Fayl yo'llarini tekshirish
# --------------------------------------------------------------------------- #
def is_inside(path: str | Path | None, root: Path) -> bool:
    """`path` haqiqatan ham `root` ichidami? (papkadan chiqib ketishga qarshi)"""
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


# --------------------------------------------------------------------------- #
# 3. Kim foydalana oladi
# --------------------------------------------------------------------------- #
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_USERS


def is_allowed_user(user_id: int) -> bool:
    """ALLOWED_USERS bo'sh bo'lsa - hamma uchun ochiq. Adminlar doim ruxsatli."""
    if is_admin(user_id):
        return True
    return not config.ALLOWED_USERS or user_id in config.ALLOWED_USERS


# --------------------------------------------------------------------------- #
# 4. So'rovlar chastotasi (spamga qarshi)
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Oddiy "siljuvchi oyna" cheklovi: soatiga N ta va so'rovlar orasida pauza."""

    def __init__(self, per_hour: int, cooldown: float) -> None:
        self.per_hour = max(1, per_hour)
        self.cooldown = max(0.0, cooldown)
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> str | None:
        """Ruxsat bo'lsa None, aks holda foydalanuvchiga xabar qaytaradi."""
        now = time.monotonic()
        hits = self._hits[user_id]
        while hits and now - hits[0] > 3600:
            hits.popleft()

        if hits and self.cooldown and now - hits[-1] < self.cooldown:
            wait = int(self.cooldown - (now - hits[-1])) + 1
            return f"⏳ Biroz sekinroq. {wait} soniyadan keyin urinib ko'ring."

        if len(hits) >= self.per_hour:
            wait = int((3600 - (now - hits[0])) / 60) + 1
            return (
                f"🚦 Soatiga {self.per_hour} tadan ko'p so'rov yuborib bo'lmaydi.\n"
                f"Taxminan {wait} daqiqadan so'ng qayta urinib ko'ring."
            )

        hits.append(now)
        return None

    def forget_old(self) -> None:
        """Xotira o'smasligi uchun eski foydalanuvchilarni tozalaydi."""
        now = time.monotonic()
        for user_id in list(self._hits):
            hits = self._hits[user_id]
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if not hits:
                del self._hits[user_id]


limiter = RateLimiter(config.RATE_LIMIT_PER_HOUR, config.RATE_LIMIT_COOLDOWN)


def rate_check(rate_limiter: RateLimiter, user_id: int) -> str | None:
    """Cheklovni tekshiradi; adminlar uchun cheklov qo'llanilmaydi."""
    if is_admin(user_id):
        return None
    return rate_limiter.check(user_id)
