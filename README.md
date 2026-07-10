# Ares Telegram Report Bot

Bot Telegram nhận ảnh báo cáo ngày, đọc số liệu bằng Gemini hoặc Claude và ghi 4 cột vào Google Sheets:

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
- `AI_PROVIDER`: `gemini` hoặc `claude`
- `GEMINI_API_KEY`: API key Gemini
- `ANTHROPIC_API_KEY`: API key Anthropic, cần khi `AI_PROVIDER=claude`
- `SPREADSHEET_ID`: ID Google Sheet
- `GOOGLE_SERVICE_ACCOUNT_JSON`: nội dung JSON của service account Google
- `ALLOWED_CHAT_IDS`: không bắt buộc; điền chat id để giới hạn ai được dùng bot
- `DRY_RUN`: `true` là đọc thử, `false` là ghi thật

## Cách dùng

Gửi hoặc forward ảnh báo cáo vào bot. Bot sẽ phản hồi số đã đọc và dòng đã ghi.

Nếu ảnh mờ, bị che, hoặc số liệu không chắc, bot sẽ không ghi và báo cần kiểm tra thủ công.

Nếu bot không đọc được ngày, nó sẽ giữ ảnh đó ở trạng thái chờ. Trả lời lại bằng dạng `Ngày 8/7` hoặc `8/7/2026`, bot sẽ xử lý lại ảnh vừa rồi với ngày được bổ sung.

Nếu bot đọc sai một số hoặc báo dữ liệu trong Sheet đang khác ảnh, có thể sửa trực tiếp bằng chat:

```text
MoMo 1.768.000
Tiền mặt 3.843.000
DT máy 4.027.000
DT dịch vụ 1.838.000
```

Nếu chỉ có một cột đang bị lệch, có thể nhắn kiểu:

```text
số đúng là 1.768.000, nhập đi
```

## Chạy trên VPS

Bot đang chạy trên VPS bằng `bot.py` dạng long polling, không cần webhook/domain.

Các lệnh quản lý trên VPS:

```bash
systemctl status ares-telegram-bot --no-pager
journalctl -u ares-telegram-bot -f
systemctl restart ares-telegram-bot
```
