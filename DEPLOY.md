# RuMio botini serverga qo'yish

Server: oddiy VPS (1-2 CPU, 1-2 GB RAM) 1000 foydalanuvchiga yetadi. Ubuntu 22.04/24.04 tavsiya.

## 1-usul: Docker (eng oson)
```
git clone <repo> rumio && cd rumio      # yoki fayllarni yuklang
cp .env.example .env && nano .env       # BOT_TOKEN, WEBAPP_URL, ADMIN_IDS, PRICE, CARD ni yozing
docker compose up -d --build
docker compose logs -f                  # loglar
```
Baza va Groq kalitlari `./data` papkasida saqlanadi (yangilashda yo'qolmaydi).
Yangilash: yangi bot.py ni qo'ying, `docker compose up -d --build`.

## 2-usul: systemd
```
sudo mkdir -p /opt/rumio && cd /opt/rumio
# bot.py, requirements.txt, .env ni shu yerga qo'ying
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo cp rumio.service /etc/systemd/system/
sudo systemctl enable --now rumio
journalctl -u rumio -f
```

## Muhim
- Zaxira kanalini (PRIVATE) ulang: server o'chsa ham baza kanaldan tiklanadi.
- Faqat BITTA nusxa ishlasin (bir token bilan 2 ta bot ishga tushmasin).
- Polling 1000 foydalanuvchi uchun yetarli; webhook shart emas.
- Baza WAL rejimida: `data.db-wal` va `data.db-shm` fayllari normal.
- `.env` va `data/` ni hech qachon GitHub ga yuklamang.
- Groq: bepul limit 1000 kishining ovozli suhbatiga YETMAYDI. Ko'p odam ishlatsa Groq pullik (Developer) tarifiga o'ting.
- Telegram kuniga ko'p xabar limiti: bot xabarlarni sekin (≈20/soniya) yuboradi, shuning uchun hamma darhol emas, bir necha daqiqada oladi.

## Pul ishlash sozlamalari (.env)
PRICE=99 000 so'm          # umrbod
PRICE_1M=39000             # 1 oylik
PRICE_3M=69000             # 3 oylik
CARD=8600 xxxx xxxx xxxx ISM FAMILIYA
LAUNCH_PRICE=79 000 so'm   # ochilish narxi (bo'sh qoldirsangiz o'chadi)
LAUNCH_SLOTS=50            # nechta birinchi xaridorga
FRIEND_DISC=20             # do'st havolasi bilan kirganga chegirma %
REF_PCT=20                 # taklif qilganga komissiya %
MIN_PAYOUT=50000           # yechish uchun minimum
HOLD_DAYS=3                # komissiya necha kundan keyin yechiladi (soxta chekdan himoya)
FREE_LESSONS=1
REF_NEED=0                 # 0 = "5 do'st = bepul VIP" o'chiq

Buyruqlar: foydalanuvchi: tugma "💸 Pul ishlash" yoki /pul (hammasi shu yerda) | admin: /promo KOD CHEGIRMA KOMISSIYA [EGA_ID], /sotuv
