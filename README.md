# 🎵 Qo'shiq izlovchi — Telegram bot

Instagram Reels, YouTube Shorts, TikTok va boshqa video havolalarini qabul qiladi,
videoni yuklab beradi va uning ostidagi **🎵 Qo'shiqni top** tugmasi bosilganda
videodagi qo'shiqni aniqlab beradi. So'ng qo'shiqni **6 xil variantda**
yuklab olish mumkin: Original, Slowed, Slowed + Reverb, Speed Up, Nightcore,
Bass Boost.

## Qanday ishlaydi

```
Havola  →  yt-dlp (video yuklash)  →  ffmpeg (audio ajratish)
        →  audio 5 ta joyidan kesiladi  →  Shazam bilan solishtiriladi
        →  natijalar ovoz berish (voting) usulida tekshiriladi  →  javob
        →  tanlangan variant (slowed / reverb / nightcore ...) mp3 bo'lib yuboriladi
```

Aniqlik uchun video **bitta emas, bir necha bo'lagidan** qidiriladi
(odatda reels boshida gap/shovqin bo'ladi, shuning uchun avval o'rta qismlar
tekshiriladi). Ikki bo'lak bir xil natija bersa — javob darhol qaytariladi va
"ishonchlilik: yuqori" deb belgilanadi.

## Talablar

- Python 3.10+
- Internet
- ffmpeg — **qo'lda o'rnatish shart emas**, `imageio-ffmpeg` paketi orqali
  avtomatik tayyorlanadi (`data/bin/ffmpeg.exe`).

## O'rnatish

```powershell
cd c:\Users\Админ\Desktop\qushiq_izlovchi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.env` faylida bot tokeni turadi:

```
BOT_TOKEN=123456:AA...
```

## Ishga tushirish

```powershell
.\.venv\Scripts\python.exe run.py
```

yoki shunchaki **`run.bat`** faylini ikki marta bosing.

To'xtatish: `Ctrl + C`.

## Serverda ishlatish (Railway)

Bot Railway'da 24/7 ishlaydi — kompyuteringiz o'chiq bo'lsa ham.

Loyihada tayyor sozlamalar bor:

- `railway.json` — ishga tushirish buyrug'i (`python run.py`) va **bitta nusxa**
  (`numReplicas: 1`);
- `.python-version` — Python 3.12.

**Muhim:** `BOT_TOKEN` va boshqa sozlamalar Railway'ning **Variables** bo'limida
turadi, koddagi `.env` fayli serverga yuborilmaydi.

⚠️ Bir vaqtda **faqat bitta nusxa** ishlashi mumkin. Server ishlab turganda
botni kompyuterda ham ishga tushirsangiz, Telegram "conflict" xatosini beradi.

## Foydalanish

1. Botga `/start` yuboring.
2. Video havolasini tashlang, masalan:
   `https://www.instagram.com/reel/XXXXXXXX/`
3. Bot videoni yuboradi, ostida **🎵 Qo'shiqni top** tugmasi chiqadi.
4. Tugmani bosing — qo'shiq nomi, ijrochisi, albomi va muqovasi keladi.
5. Variant tugmasini bosing — qo'shiq mp3 fayl bo'lib keladi:

| Tugma | Nima qiladi |
|---|---|
| 🎧 Original | O'zgartirilmagan |
| 🐌 Slowed | Sekinlashtirilgan (0.86x) |
| 🌊 Slowed + Reverb | Sekin + aks-sado |
| ⚡ Speed Up | Tezlashtirilgan (1.22x) |
| 🌙 Nightcore | Juda tez, yupqa ovoz |
| 🔊 Bass Boost | Bass kuchaytirilgan |

Birinchi variant ~15 soniya oladi (qo'shiq yuklanadi), keyingilari ~3 soniya.
Bir marta yuborilgan variant Telegram `file_id` bilan keshlanadi — qayta
bosilsa bir zumda keladi.

## Sozlamalar (`.env`)

| Kalit | Ma'nosi | Standart |
|---|---|---|
| `BOT_TOKEN` | Telegram bot tokeni | — |
| `MAX_VIDEO_HEIGHT` | Yuklanadigan video maksimal balandligi | `720` |
| `MAX_UPLOAD_MB` | Telegramga yuborish chegarasi | `48` |
| `CLIP_SECONDS` | Tahlil uchun audio bo'lak uzunligi | `12` |
| `MAX_ATTEMPTS` | Nechta bo'lak tekshirilsin (aniqlik ↑, tezlik ↓) | `5` |
| `FILE_TTL_SECONDS` | Vaqtinchalik fayllar umri | `7200` |
| `MAX_CONCURRENT_DOWNLOADS` | Parallel yuklashlar soni | `3` |
| `DOWNLOAD_TIMEOUT` | Bitta yuklashga ajratilgan vaqt (soniya) | `300` |
| `MAX_DURATION_MIN` | Videoning maksimal uzunligi (daqiqa) | `30` |
| `MAX_CONCURRENT_RECOGNITIONS` | Shazam'ga parallel so'rovlar soni | `2` |
| `MAX_FILESIZE_MB` | Diskka yuklashning eng katta hajmi | `200` |
| `SONG_MAX_MB` | Qo'shiq audiosining eng katta hajmi | `60` |
| `SONG_MAX_MIN` | Qo'shiqning eng katta uzunligi (daqiqa) | `12` |
| `ALLOWED_USERS` | Yopiq rejim: ruxsat etilgan Telegram ID'lar | bo'sh (hamma) |
| `ADMIN_USERS` | Cheklovlarsiz ishlaydigan egalar | bo'sh |
| `RATE_LIMIT_PER_HOUR` | Bir foydalanuvchi uchun soatlik limit | `20` |
| `RATE_LIMIT_COOLDOWN` | So'rovlar orasidagi pauza (soniya) | `5` |
| `MAX_DISK_MB` | Vaqtinchalik fayllar uchun eng katta joy | `2048` |
| `COOKIES_FROM_BROWSER` | `auto` / `chrome` / `edge` / `firefox` — Instagram uchun | bo'sh |
| `ACR_*` | Ixtiyoriy ACRCloud zaxira xizmati | bo'sh |

## Instagram "login talab qilinyapti" desa

Instagram ba'zan mehmon (login qilmagan) so'rovlarni bloklaydi. Yechim:

1. `.env` da `COOKIES_FROM_BROWSER=auto` deb yozing — bot avval cookie'siz
   urinadi, bloklansa Chrome → Edge → Firefox cookie'lari bilan qayta uradi
   (brauzeringizda Instagram'ga kirgan bo'lishingiz va brauzer yopiq turishi
   kerak), **yoki**
2. brauzerdan `cookies.txt` (Netscape formatida) eksport qilib, loyiha
   papkasiga qo'ying.

## Aniqlikni yanada oshirish (ixtiyoriy)

Shazam bazasida yo'q qo'shiqlar uchun ACRCloud zaxirasini yoqish mumkin:
[console.acrcloud.com](https://console.acrcloud.com) da bepul loyiha ochib,
`.env` ga `ACR_HOST`, `ACR_ACCESS_KEY`, `ACR_ACCESS_SECRET` ni yozing.
Shazam topa olmagan holatda avtomatik ishlaydi.

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `run.py`, `run.bat` | Ishga tushirish |
| `app/bot.py` | Telegram handlerlari, tugmalar, javob matnlari |
| `app/downloader.py` | yt-dlp orqali video yuklash |
| `app/audio.py` | ffmpeg: audio ajratish va bo'laklarga kesish |
| `app/recognizer.py` | Shazam + ACRCloud orqali qo'shiqni aniqlash |
| `app/variants.py` | Slowed / Reverb / Nightcore / Bass effektlari |
| `app/storage.py` | Tugmalar uchun ma'lumot saqlash va tozalash |
| `app/ffmpeg_setup.py` | ffmpeg ni avtomatik tayyorlash |
| `app/security.py` | Xavfsizlik: havola tekshiruvi, limitlar, yo'l nazorati |
| `app/texts.py` | Barcha o'zbekcha matnlar |

## Xavfsizlik

Botga o'rnatilgan himoya choralari:

| Xavf | Himoya |
|---|---|
| **SSRF** — ichki tarmoqqa so'rov (`127.0.0.1`, `192.168.x.x`, bulut metadata `169.254.169.254`) | Har bir havola DNS orqali tekshiriladi; ichki/xizmat manzillari, `.local`/`.onion` domenlari, 80/443 dan boshqa portlar va parolli havolalar rad etiladi |
| **Spam / DoS** | Foydalanuvchi uchun soatlik limit + pauza, bir vaqtda bitta yuklash, parallel yuklashlar cheklovi |
| **Diskni to'ldirish** | `MAX_DISK_MB` chegarasi, `MAX_FILESIZE_MB` yuklash chegarasi, video uzunligi chegarasi, avtomatik tozalash |
| **Papkadan chiqib ketish (path traversal)** | Fayl o'chirish va o'qish faqat `data/downloads` ichida; buzilgan yozuvlar rad etiladi |
| **HTML/havola in'yeksiyasi** | Barcha matnlar HTML-ekranlanadi; tugmalarga faqat `http(s)` havolalar qo'yiladi |
| **Buyruq in'yeksiyasi** | ffmpeg shell'siz (`exec`) chaqiriladi, fayl nomlari botning o'zi tomonidan beriladi |
| **Begona bosishlar** | Tugma faqat o'zi paydo bo'lgan suhbatda ishlaydi; token tasodifiy (64 bit) |
| **Xato ma'lumot sizishi** | Foydalanuvchiga faqat tayyor xabarlar; texnik tafsilotlar log'ga yoziladi |

### Tavsiyalar

1. **Tokenni yangilang.** Token yozishmada ko'ringan bo'lsa,
   [@BotFather](https://t.me/BotFather) → `/revoke` → yangi tokenni `.env` ga yozing.
2. **`.env` va `cookies.txt` ni hech kimga bermang** — ular `.gitignore` da,
   lekin fayllarni nusxalashda ehtiyot bo'ling. `cookies.txt` sizning
   Instagram/YouTube sessiyangiz — u sizib ketsa, akkauntingiz xavf ostida.
3. **Yopiq rejim.** Bot faqat o'zingiz uchun bo'lsa, `.env` da
   `ALLOWED_USERS=<sizning ID>` deb yozing (ID ni [@userinfobot](https://t.me/userinfobot)
   dan olasiz). Shunda begonalar botdan foydalana olmaydi.
4. **Serverga qo'yganda** botni alohida (administrator bo'lmagan) foydalanuvchi
   nomidan ishlating va `data/` papkasiga faqat o'sha foydalanuvchi kira olsin.

### Qolgan xavflar (ochiq aytilgan)

Havola tekshiruvi **boshlang'ich manzilni** himoya qiladi. Sayt so'rovni ichki
manzilga qayta yo'naltirsa (redirect) yoki DNS javobi tekshiruvdan keyin
o'zgarsa (DNS rebinding), yt-dlp o'sha manzilga borishi mumkin. Shuning uchun
botni ichki tarmoqda maxfiy xizmatlar bilan bitta serverda ishlatmang yoki
uni alohida konteyner/tarmoqda yuriting.
