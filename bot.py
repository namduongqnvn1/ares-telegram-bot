import json
import mimetypes
import os
import re
import time
import traceback
import unicodedata
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from reporting import ClaudeExtractor, GeminiExtractor, Report, ReportError, SheetConflictError, SheetsWriter


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


def create_extractor():
    provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
    if provider in {"claude", "anthropic"}:
        return ClaudeExtractor()
    return GeminiExtractor()


def parse_date_hint(text):
    match = re.search(r"(?:ngày\s*)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text, flags=re.I)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    raw_year = match.group(3)
    year = datetime.now().year if not raw_year else int(raw_year)
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).strftime("%d/%m/%Y")
    except ValueError:
        return None


def is_missing_date_error(exc):
    return "ngày báo cáo" in str(exc).lower()


def report_from_dict(data):
    return Report(
        report_date=data["report_date"],
        machine_revenue=int(data["machine_revenue"]),
        service_revenue=int(data["service_revenue"]),
        cash=int(data["cash"]),
        momo=int(data["momo"]),
        confidence=float(data.get("confidence", 1)),
        warnings=tuple(data.get("warnings", [])),
    )


def normalize_money(raw):
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    value = int(digits)
    if value < 100_000:
        value *= 1000
    return value


def strip_accents(text):
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("đ", "d").replace("Đ", "D")


def parse_manual_correction(text, pending=None):
    normalized = strip_accents(text).lower()
    column_patterns = {
        "machine_revenue": r"\b(dt\s*may|fnet)\b",
        "service_revenue": r"\b(dt\s*dich\s*vu|ffood|dich\s*vu)\b",
        "cash": r"\b(tien\s*mat|cash|con|tm)\b",
        "momo": r"\b(momo|mo\s*mo|chuyen\s*khoan|bank|ck)\b",
    }
    field = None
    for candidate, pattern in column_patterns.items():
        if re.search(pattern, normalized, flags=re.I):
            field = candidate
            break

    money_matches = re.findall(r"\d[\d.,]*", text)
    value = None
    for match in reversed(money_matches):
        parsed = normalize_money(match)
        if parsed and parsed >= 100_000:
            value = parsed
            break
    if value is None:
        return None

    if field is None and pending and pending.get("type") == "conflict":
        conflicts = pending.get("conflicts", {})
        if len(conflicts) == 1:
            only_column = next(iter(conflicts))
            field = {
                "B": "machine_revenue",
                "C": "service_revenue",
                "E": "cash",
                "F": "momo",
            }.get(only_column)
    if field is None:
        return None
    return field, value


def parse_manual_corrections(text, pending=None):
    """Cho phép sửa nhiều cột trong một tin nhắn, ví dụ: momo 2.094.000, tm 1.944.000"""
    corrections = {}
    for segment in re.split(r"[,;\n]+", text or ""):
        parsed = parse_manual_correction(segment, pending)
        if parsed:
            field, value = parsed
            corrections[field] = value
    return corrections


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


def format_conflict_prompt(exc):
    return (
        f"⚠️ <b>Dữ liệu trong Sheet đã có và khác ảnh</b>\n"
        f"Ngày: <b>{exc.report.report_date}</b>\n"
        f"Sheet: <b>{exc.sheet}</b>, dòng <b>{exc.row}</b>\n\n"
        f"{exc}\n\n"
        f"Nếu ảnh mới đúng và muốn thay số cũ, trả lời: <b>ghi đè</b>\n"
        f"Nếu AI đọc sai, trả lời kiểu: <b>MoMo 1.768.000</b> hoặc <b>Tiền mặt 3.843.000</b>"
    )


def write_report_from_image(bot, chat_id, extractor, writer, done_file_ids, file_id, unique_id, date_hint=None, overwrite=False):
    image_bytes, mime_type = bot.download_file(file_id)
    report = extractor.extract(image_bytes, mime_type, date_hint=date_hint)
    dry_run = dry_run_enabled()
    result = writer.write(report, dry_run=dry_run, overwrite=overwrite)
    if unique_id and not dry_run:
        done_file_ids.append(unique_id)
        del done_file_ids[:-300]
    bot.send_message(chat_id, format_success(report, result, dry_run))
    return report


def process_message(bot, message, extractor, writer, done_file_ids, pending_by_chat):
    chat_id = message["chat"]["id"]
    chat_key = str(chat_id)
    if not bot.is_allowed(chat_id):
        bot.send_message(chat_id, "⛔ Chat này chưa được phép dùng bot.")
        return

    raw_text = (message.get("text") or "").strip()
    text = raw_text.lower()
    manual_corrections = parse_manual_corrections(raw_text, pending_by_chat.get(chat_key))
    if manual_corrections and chat_key in pending_by_chat:
        pending = pending_by_chat[chat_key]
        if pending.get("type") not in {"conflict", "manual"}:
            bot.send_message(chat_id, "Tao nhận được số sửa, nhưng ảnh đang chờ không phải lỗi số liệu.")
            return
        report = report_from_dict(pending["report"])
        report = replace(report, **manual_corrections)
        dry_run = dry_run_enabled()
        result = writer.write(report, dry_run=dry_run, overwrite=True)
        unique_id = pending.get("unique_id")
        if unique_id and not dry_run:
            done_file_ids.append(unique_id)
            del done_file_ids[:-300]
        pending_by_chat[chat_key] = {
            "type": "manual",
            "report": report.to_dict(),
            "unique_id": unique_id,
            "created_at": int(time.time()),
        }
        bot.send_message(chat_id, format_success(report, result, dry_run))
        return

    if text in {"ghi đè", "ghi de", "overwrite", "cap nhat", "cập nhật"} and chat_key in pending_by_chat:
        pending = pending_by_chat[chat_key]
        if pending.get("type") != "conflict":
            bot.send_message(chat_id, "Ảnh đang chờ không phải lỗi dữ liệu khác nhau, tao không ghi đè.")
            return
        report = report_from_dict(pending["report"])
        dry_run = dry_run_enabled()
        result = writer.write(report, dry_run=dry_run, overwrite=True)
        unique_id = pending.get("unique_id")
        if unique_id and not dry_run:
            done_file_ids.append(unique_id)
            del done_file_ids[:-300]
        pending_by_chat[chat_key] = {
            "type": "manual",
            "report": report.to_dict(),
            "unique_id": unique_id,
            "created_at": int(time.time()),
        }
        bot.send_message(chat_id, format_success(report, result, dry_run))
        return

    date_hint = parse_date_hint(raw_text)
    if (
        date_hint
        and chat_key in pending_by_chat
        and pending_by_chat[chat_key].get("type") == "missing_date"
    ):
        pending = pending_by_chat[chat_key]
        if pending.get("unique_id") and pending["unique_id"] in done_file_ids:
            del pending_by_chat[chat_key]
            bot.send_message(chat_id, "Ảnh đang chờ này đã được xử lý rồi, tao bỏ qua để tránh trùng.")
            return
        bot.send_message(chat_id, f"Đã nhận ngày {date_hint}, tao xử lý lại ảnh vừa rồi...")
        report = write_report_from_image(
            bot,
            chat_id,
            extractor,
            writer,
            done_file_ids,
            pending["file_id"],
            pending.get("unique_id"),
            date_hint=date_hint,
        )
        pending_by_chat[chat_key] = {
            "type": "manual",
            "report": report.to_dict(),
            "unique_id": pending.get("unique_id"),
            "created_at": int(time.time()),
        }
        return

    if text in {"/start", "/help"}:
        bot.send_message(
            chat_id,
            "Gửi hoặc forward ảnh báo cáo ngày vào đây. Bot sẽ đọc 4 cột: DT máy, DT dịch vụ, Tiền mặt, MoMo rồi ghi Google Sheet.",
        )
        return

    file_id, unique_id, source = pick_image(message)
    if not file_id:
        if date_hint:
            bot.send_message(chat_id, "Tao nhận được ngày, nhưng hiện không có ảnh nào đang chờ bổ sung ngày.")
        else:
            bot.send_message(chat_id, "Mày gửi/forward ảnh báo cáo vào đây là được.")
        return
    if unique_id and unique_id in done_file_ids:
        bot.send_message(chat_id, "Ảnh này tao xử lý rồi, không ghi lại để tránh trùng.")
        return

    bot.send_message(chat_id, "Đã nhận ảnh, tao đang đọc số liệu...")
    try:
        report = write_report_from_image(bot, chat_id, extractor, writer, done_file_ids, file_id, unique_id)
        pending_by_chat[chat_key] = {
            "type": "manual",
            "report": report.to_dict(),
            "unique_id": unique_id,
            "created_at": int(time.time()),
        }
    except ReportError as exc:
        if is_missing_date_error(exc):
            pending_by_chat[chat_key] = {
                "type": "missing_date",
                "file_id": file_id,
                "unique_id": unique_id,
                "source": source,
                "created_at": int(time.time()),
            }
            bot.send_message(
                chat_id,
                "⚠️ Tao chưa đọc được ngày báo cáo. Mày trả lời ngày theo dạng <b>Ngày 8/7</b> hoặc <b>8/7/2026</b>, tao sẽ xử lý lại ảnh này.",
            )
            return
        if isinstance(exc, SheetConflictError):
            pending_by_chat[chat_key] = {
                "type": "conflict",
                "report": exc.report.to_dict(),
                "conflicts": exc.conflicts,
                "unique_id": unique_id,
                "created_at": int(time.time()),
            }
            bot.send_message(chat_id, format_conflict_prompt(exc))
            return
        raise


def main():
    load_dotenv()
    state_path = Path(os.environ.get("STATE_FILE", ".ares-telegram-state.json"))
    state = load_state(state_path)
    bot = TelegramBot()
    extractor = create_extractor()
    writer = SheetsWriter()
    done_file_ids = state.setdefault("done_file_ids", [])
    pending_by_chat = state.setdefault("pending_by_chat", {})

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
                    process_message(bot, message, extractor, writer, done_file_ids, pending_by_chat)
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
