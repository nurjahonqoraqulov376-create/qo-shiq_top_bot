"""Botning barcha matnlari (o'zbekcha)."""
from __future__ import annotations

START = (
    "🎬 <b>Qo'shiq izlovchi botga xush kelibsiz!</b>\n\n"
    "Menga <b>Instagram Reels</b>, <b>YouTube Shorts</b>, <b>TikTok</b> yoki "
    "boshqa video havolasini yuboring.\n\n"
    "Men:\n"
    "1️⃣ videoni yuklab beraman;\n"
    "2️⃣ uning ostida <b>🎵 Qo'shiqni top</b> tugmasi chiqadi;\n"
    "3️⃣ tugmani bosing — videodagi qo'shiq nomi, ijrochisi va tinglash "
    "havolalarini aniq topib beraman.\n\n"
    "👇 Hoziroq havola yuboring."
)

HELP = (
    "ℹ️ <b>Yordam</b>\n\n"
    "<b>Qanday ishlataman?</b>\n"
    "Shunchaki video havolasini yuboring. Masalan:\n"
    "<code>https://www.instagram.com/reel/XXXXXXXXX/</code>\n"
    "<code>https://youtube.com/shorts/XXXXXXXXX</code>\n\n"
    "<b>Qo'llab-quvvatlanadi:</b> Instagram, YouTube, TikTok, Facebook, "
    "Pinterest, Twitter (X) va boshqa ko'plab saytlar.\n\n"
    "<b>Qo'shiq topilmasa nima qilay?</b>\n"
    "• Videoda musiqa juda past yoki faqat gap bo'lsa, topilmasligi mumkin.\n"
    "• Qayta urinish tugmasini bosib ko'ring.\n\n"
    "<b>Buyruqlar:</b>\n"
    "/start — boshlash\n"
    "/help — yordam"
)

NO_URL = (
    "🔗 Iltimos, video <b>havolasini</b> yuboring.\n"
    "Masalan: <code>https://www.instagram.com/reel/...</code>"
)

DOWNLOADING = "⏳ Video yuklanmoqda, biroz kuting..."
UPLOADING = "📤 Video yuborilmoqda..."
BUSY = "⌛ Oldingi videongiz hali tayyor bo'lmadi. Biroz kuting."

CAPTION_HINT = "👇 Videodagi qo'shiqni bilish uchun tugmani bosing"

SEARCHING = "🔎 Qo'shiq qidirilmoqda..."
SEARCHING_STEP = "🔎 Qo'shiq qidirilmoqda... ({step}/{total})"
SEARCHING_DEEP = "🔬 Chuqurroq qidirilmoqda, biroz kuting..."
ALREADY_SEARCHING = "⏳ Qidiruv allaqachon davom etyapti..."

EXPIRED = (
    "⌛ Bu videoning ma'lumotlari eskirgan.\n"
    "Iltimos, havolani qaytadan yuboring."
)

NOT_FOUND = (
    "😔 <b>Afsuski, qo'shiq topilmadi.</b>\n\n"
    "Sabablari:\n"
    "• videoda musiqa yo'q yoki juda past;\n"
    "• ovoz ustidan gapirilgan;\n"
    "• qo'shiq bazada mavjud emas.\n\n"
    "🔄 Qayta urinib ko'rishingiz mumkin."
)

NO_AUDIO = "🔇 Bu videoda ovoz yo'q, shuning uchun qo'shiqni aniqlab bo'lmaydi."

TOO_BIG = (
    "⚠️ Video hajmi Telegram chegarasidan ({limit} MB) katta, "
    "shuning uchun faqat qo'shiq qidirish imkoni bor."
)

ERROR = "❌ Xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."

NOT_ALLOWED = (
    "🔒 Bu bot yopiq rejimda ishlayapti.\n"
    "Foydalanish uchun bot egasiga murojaat qiling."
)

DISK_BUSY = (
    "💾 Server hozir band (vaqtinchalik xotira to'lgan).\n"
    "Bir necha daqiqadan so'ng qayta urinib ko'ring."
)

WRONG_CHAT = "⚠️ Bu tugma boshqa suhbatga tegishli."
