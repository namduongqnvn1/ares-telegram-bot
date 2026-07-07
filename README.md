# Ares Telegram Report Bot

Bot Telegram nhận ảnh báo cáo ngày, đọc số liệu bằng Gemini và ghi 4 cột vào Google Sheets:

- DT máy = ô Tổng của Fnet
- DT dịch vụ = ô Tổng của Ffood
- Tiền mặt = mục Còn; nếu không có Còn thì dùng Tổng tiền mặt
- MoMo = ô Tổng của Tiền chuyển khoản

## Chạy thử trên máy

```powershell
cd ares-telegram-bot
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Điền các biến trong `.env`, sau đó chạy:

```powershell
.\.venv\Scripts\python bot.py
```

Để test an toàn, để:

```env
DRY_RUN=true
```

Khi bot đọc đúng rồi mới đổi:

```env
DRY_RUN=false
```

## Biến môi trường cần có

- `TELEGRAM_BOT_TOKEN`: token lấy từ BotFather
- `GEMINI_API_KEY`: API key Gemini
- `SPREADSHEET_ID`: ID Google Sheet
- `GOOGLE_SERVICE_ACCOUNT_JSON`: nội dung JSON của service account Google
- `ALLOWED_CHAT_IDS`: không bắt buộc; điền chat id để giới hạn ai được dùng bot
- `DRY_RUN`: `true` là đọc thử, `false` là ghi thật

## Cách dùng

Gửi hoặc forward ảnh báo cáo vào bot. Bot sẽ phản hồi số đã đọc và dòng đã ghi.

Nếu ảnh mờ, bị che, hoặc số liệu không chắc, bot sẽ không ghi và báo cần kiểm tra thủ công.
