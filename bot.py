import json
import mimetypes
import os
import time
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

from reporting import GeminiExtractor, ReportError, SheetsWriter


class TelegramBot:
    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"
        self.allowed_chat_ids = {
            int(x.strip())
            for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
            if x.strip()
        }

    def api(self, method, **params):
        response = requests.post(f"{self.base_url}/{method}", json=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data)
        return data["result"]

    def send_message(self, chat_id, text):
        return self.api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

    def get_updates(self, offset=None):
        params = {"timeout": 50, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.api("getUpdates", **params)

    def download_file(self, file_id):
        info = self.api("getFile", file_id=file_id)
        response = requests.get(f"{self.file_url}/{info['file_path']}", timeout=60)
        response.raise_for_status()
        mime_type = mimetypes.guess_type(info["file_path"])[0] or "image/jpeg"
        return response.content, mime_type

    def is_allowed(self, chat_id):
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids


def load_state(path: Path):
    if not path.exists():
        return {"offset": None, "done_file_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"offset": None, "done_file_ids": []}


def save_state(path: Path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def dry_run_enabled():
    return str(os.environ.get("DRY_RUN", "true")).lower() in {"1", "true", "yes", "on"}


def pick_image(message):
    if message.get("photo"):
        photo = message["photo"][-1]
        return photo["file_id"], photo.get("file_unique_id"), "photo"
    document = message.get("document") or {}
    if str(document.get("mime_type", "")).startswith("image/"):
        return document["file_id"], document.get("file_unique_id"), "document"
    return None, None, None


def format_money(value):
    return f"{value:,}".replace(",", ".")


def format_success(report, result, dry_run):
    values = result["values"]
    status = "ĐÃ ĐỌC THỬ, CHƯA GHI" if dry_run else "ĐÃ GHI GOOGLE SHEET"
    warning_text = ""
    if report.warnings:
        warning_text = "\n\n⚠️ Cảnh báo đối chiếu:\n" + "\n".join(f"- {warning}" for warning in report.warnings)
    return (
        f"✅ <b>{status}</b>\n"
        f"Ngày: <b>{report.report_date}</b>\n"
        f"Sheet: <b>{result['sheet']}</b>, dòng <b>{result['row']}</b>\n\n"
        f"DT máy: <b>{format_money(values['B'])}</b>\n"
        f"DT dịch vụ: <b>{format_money(values['C'])}</b>\n"
        f"Tiền mặt: <b>{format_money(values['E'])}</b>\n"
        f"MoMo: <b>{format_money(values['F'])}</b>"
        f"{warning_text}"
    )


def process_message(bot, message, extractor, writer, done_file_ids):
    chat_id = message["chat"]["id"]
    if not bot.is_allowed(chat_id):
        bot.send_message(chat_id, "⛔ Chat này chưa được phép dùng bot.")
        return

    text = (message.get("text") or "").strip().lower()
    if text in {"/start", "/help"}:
        bot.send_message(
            chat_id,
            "Gửi hoặc forward ảnh báo cáo ngày vào đây. Bot sẽ đọc 4 cột: DT máy, DT dịch vụ, Tiền mặt, MoMo rồi ghi Google Sheet.",
        )
        return

    file_id, unique_id, source = pick_image(message)
    if not file_id:
        bot.send_message(chat_id, "Mày gửi/forward ảnh báo cáo vào đây là được.")
        return
    if unique_id and unique_id in done_file_ids:
        bot.send_message(chat_id, "Ảnh này tao xử lý rồi, không ghi lại để tránh trùng.")
        return

    bot.send_message(chat_id, "Đã nhận ảnh, tao đang đọc số liệu...")
    image_bytes, mime_type = bot.download_file(file_id)
    report = extractor.extract(image_bytes, mime_type)
    dry_run = dry_run_enabled()
    result = writer.write(report, dry_run=dry_run)
    if unique_id and not dry_run:
        done_file_ids.append(unique_id)
        del done_file_ids[:-300]
    bot.send_message(chat_id, format_success(report, result, dry_run))


def main():
    load_dotenv()
    state_path = Path(os.environ.get("STATE_FILE", ".ares-telegram-state.json"))
    state = load_state(state_path)
    bot = TelegramBot()
    extractor = GeminiExtractor()
    writer = SheetsWriter()
    done_file_ids = state.setdefault("done_file_ids", [])

    print("Ares Telegram bot đang chạy...", flush=True)
    while True:
        try:
            updates = bot.get_updates(offset=state.get("offset"))
            for update in updates:
                state["offset"] = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    save_state(state_path, state)
                    continue
                try:
                    process_message(bot, message, extractor, writer, done_file_ids)
                except ReportError as exc:
                    bot.send_message(message["chat"]["id"], f"⚠️ Cần kiểm tra thủ công: {exc}")
                except Exception:
                    traceback.print_exc()
                    bot.send_message(message["chat"]["id"], "❌ Bot lỗi khi xử lý ảnh. Xem log trên host giúp tao.")
                save_state(state_path, state)
        except KeyboardInterrupt:
            break
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
