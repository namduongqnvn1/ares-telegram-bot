import base64
import io
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime

import requests

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageEnhance = ImageFilter = ImageOps = None


class ReportError(Exception):
    pass


class SheetConflictError(ReportError):
    def __init__(self, report: "Report", sheet: str, row: int, conflicts: dict):
        self.report = report
        self.sheet = sheet
        self.row = row
        self.conflicts = conflicts
        super().__init__(self._format_message())

    @staticmethod
    def _label(column):
        return {
            "B": "DT máy",
            "C": "DT dịch vụ",
            "E": "Tiền mặt",
            "F": "MoMo",
        }.get(column, column)

    @staticmethod
    def _money(value):
        return f"{value:,}".replace(",", ".")

    def _format_message(self):
        parts = []
        for column, values in self.conflicts.items():
            parts.append(
                f"{self._label(column)} đang có {self._money(values['current'])}, ảnh đọc ra {self._money(values['new'])}"
            )
        return "Dữ liệu đã có và khác ảnh: " + "; ".join(parts)


@dataclass(frozen=True)
class Report:
    report_date: str
    machine_revenue: int
    service_revenue: int
    cash: int
    momo: int
    confidence: float
    warnings: tuple[str, ...] = ()

    @property
    def month(self) -> int:
        return datetime.strptime(self.report_date, "%d/%m/%Y").month

    @property
    def year(self) -> int:
        return datetime.strptime(self.report_date, "%d/%m/%Y").year

    def to_dict(self):
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


PROMPT = """Bạn đọc một phiếu BÁO CÁO NGÀY viết tay của Ares Gaming.
Chỉ đọc bảng DOANH THU ở nửa trên ảnh. Bỏ qua hoàn toàn bảng HÀNG TỒN KHO.

Trả về DUY NHẤT một JSON object theo mẫu:
{
  "report_date": "dd/mm/yyyy",
  "fnet_shifts": [0,0,0], "fnet_total": 0,
  "ffood_shifts": [0,0,0], "ffood_total": 0,
  "transfer_shifts": [0,0,0], "transfer_total": 0,
  "cash_total": 0, "cash_remaining": null,
  "confidence": 0.0, "warnings": []
}

Quy tắc:
- Các số trên phiếu có đơn vị nghìn đồng. JSON phải là số tiền VND đầy đủ; ví dụ 3918 thành 3918000.
- report_date lấy từ dòng BÁO CÁO NGÀY ... THÁNG ...; năm hiện hành là 2026 nếu ảnh không ghi năm.
- Nếu ngày hoặc tháng viết tay không đọc được chắc chắn, đặt report_date là null. TUYỆT ĐỐI không suy đoán ngày.
- fnet_total là ô Tổng của dòng Fnet.
- ffood_total là ô Tổng của dòng Ffood.
- transfer_total là ô Tổng của dòng Tiền chuyển khoản.
- cash_total là ô Tổng của dòng Tiền mặt.
- Thứ tự các dòng là Fnet, Ffood, Tổng tiền máy, Tiền chuyển khoản, Tiền mặt, Chi, Tổng tiền thực tế.
- cash_remaining là số cạnh chữ Còn ở cột phải. Nếu không có số Còn thì để null.
- Cột Google Sheet cần ghi: DT máy = Fnet tổng; DT dịch vụ = Ffood tổng; Tiền mặt = Còn nếu có, nếu không dùng Tiền mặt tổng; MoMo = chuyển khoản tổng.
- Không lấy nhầm Tổng tiền máy, Tổng tiền thực tế, tồn kho hoặc suy diễn số bị che.
- Chỉ cần đọc các ô Tổng/cột phải theo quy tắc trên. Không tự cộng 3 ca để tạo cảnh báo.
- Nếu không chắc trường bắt buộc, đặt confidence dưới 0.8 và mô tả trong warnings.
"""


def build_prompt(date_hint: str | None = None) -> str:
    if not date_hint:
        return PROMPT
    return (
        PROMPT
        + "\nBổ sung từ người dùng: ngày báo cáo chắc chắn là "
        + date_hint
        + ". Hãy dùng đúng giá trị này cho report_date, không cần đọc lại ngày trên ảnh.\n"
    )


def prepare_report_image(image_bytes: bytes, mime_type: str):
    """Cắt phần doanh thu và tăng độ rõ trước khi gửi AI."""
    if Image is None:
        return image_bytes, mime_type
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        width, height = image.size
        image = image.crop((0, 0, width, max(1, int(height * 0.58))))
        if image.width < 1800:
            scale = 1800 / image.width
            image = image.resize((1800, int(image.height * scale)), Image.Resampling.LANCZOS)
        image = ImageEnhance.Contrast(image).enhance(1.25)
        image = image.filter(ImageFilter.UnsharpMask(radius=1.5, percent=140, threshold=3))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95, optimize=True)
        return output.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, mime_type


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ReportError("AI không trả về JSON hợp lệ") from exc
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as inner:
            raise ReportError("AI trả về JSON bị lỗi") from inner


def _money(value, field: str, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{field} không phải số")
    value = int(value)
    if value < 0 or value > 100_000_000:
        raise ReportError(f"{field} nằm ngoài giới hạn hợp lý")
    return value


def parse_report_payload(data: dict) -> Report:
    try:
        parsed_date = datetime.strptime(data["report_date"], "%d/%m/%Y")
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportError("Không đọc được ngày báo cáo") from exc
    if parsed_date.year < 2025 or parsed_date.year > 2035:
        raise ReportError("Năm báo cáo không hợp lệ")

    fnet = _money(data.get("fnet_total"), "Fnet")
    ffood = _money(data.get("ffood_total"), "Ffood")
    transfer = _money(data.get("transfer_total"), "chuyển khoản")
    cash_total = _money(data.get("cash_total"), "tiền mặt")
    cash_remaining = _money(data.get("cash_remaining"), "số Còn", optional=True)
    confidence = float(data.get("confidence", 0))
    warnings = []
    for item in data.get("warnings", []):
        warning = str(item)
        lowered = warning.lower()
        if "shift" in lowered and "sum" in lowered:
            continue
        if "tổng 3 ca" in lowered:
            continue
        warnings.append(warning)

    min_confidence = float(os.environ.get("MIN_CONFIDENCE", "0.8"))
    if confidence < min_confidence:
        raise ReportError("Ảnh cần kiểm tra thủ công: " + "; ".join(warnings or ["độ tin cậy thấp"]))

    return Report(
        report_date=parsed_date.strftime("%d/%m/%Y"),
        machine_revenue=fnet,
        service_revenue=ffood,
        cash=cash_remaining if cash_remaining is not None else cash_total,
        momo=transfer,
        confidence=confidence,
        warnings=tuple(warnings),
    )


class GeminiExtractor:
    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        if not self.api_key:
            raise ReportError("Thiếu GEMINI_API_KEY")

    def extract(self, image_bytes: bytes, mime_type: str, date_hint: str | None = None) -> Report:
        image_bytes, mime_type = prepare_report_image(image_bytes, mime_type)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [
                {"text": build_prompt(date_hint)},
                {"inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        response = None
        for attempt in range(4):
            response = requests.post(url, params={"key": self.api_key}, json=payload, timeout=60)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < 3:
                time.sleep(2 ** attempt)
        if response.status_code >= 400:
            raise ReportError(f"Gemini lỗi HTTP {response.status_code}")
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ReportError("Không lấy được kết quả từ Gemini") from exc
        data = _extract_json(text)
        if date_hint:
            data["report_date"] = date_hint
        return parse_report_payload(data)


class ClaudeExtractor:
    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
        if not self.api_key:
            raise ReportError("Thiếu ANTHROPIC_API_KEY")

    def extract(self, image_bytes: bytes, mime_type: str, date_hint: str | None = None) -> Report:
        image_bytes, mime_type = prepare_report_image(image_bytes, mime_type)
        payload = {
            "model": self.model,
            "max_tokens": 1200,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(date_hint)},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }},
                ],
            }],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        response = None
        for attempt in range(4):
            response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=90)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < 3:
                time.sleep(2 ** attempt)
        if response.status_code >= 400:
            raise ReportError(f"Claude lỗi HTTP {response.status_code}: {response.text[:200]}")
        try:
            parts = response.json()["content"]
            text = "\n".join(part.get("text", "") for part in parts if part.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise ReportError("Không lấy được kết quả từ Claude") from exc
        data = _extract_json(text)
        if date_hint:
            data["report_date"] = date_hint
        return parse_report_payload(data)


class SheetsWriter:
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, spreadsheet_id=None, credentials_json=None):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ReportError("Thiếu thư viện Google API; hãy cài requirements.txt") from exc
        self.spreadsheet_id = spreadsheet_id or os.environ.get("SPREADSHEET_ID")
        raw = credentials_json or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        credentials_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not raw and credentials_file:
            try:
                with open(credentials_file, "r", encoding="utf-8") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise ReportError("Không mở được file JSON Google") from exc
        if not self.spreadsheet_id or not raw:
            raise ReportError("Thiếu cấu hình Google Sheets")
        try:
            info = json.loads(raw)
            credentials = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
        except (ValueError, TypeError, KeyError) as exc:
            raise ReportError("GOOGLE_SERVICE_ACCOUNT_JSON không hợp lệ") from exc
        self.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def _sheet_title(self, report: Report) -> str:
        return f"Tháng {report.month}/{report.year}"

    def _find_row(self, report: Report):
        from googleapiclient.errors import HttpError

        title = self._sheet_title(report)
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A5:A40",
                valueRenderOption="FORMATTED_VALUE",
            ).execute()
        except HttpError as exc:
            raise ReportError(
                f"Không đọc được ngày báo cáo chính xác (sheet {title} không tồn tại, có thể AI đọc nhầm ngày)"
            ) from exc
        for offset, values in enumerate(result.get("values", []), start=5):
            if values and str(values[0]).strip() == report.report_date:
                return title, offset
        raise ReportError(
            f"Không đọc được ngày báo cáo khớp với Sheet (không thấy {report.report_date} trong sheet {title})"
        )

    @staticmethod
    def _number(value):
        if value in (None, ""):
            return None
        return int(float(str(value).replace(".", "").replace(",", "")))

    def write(self, report: Report, dry_run=True, overwrite=False):
        title, row = self._find_row(report)
        current = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{title}'!B{row}:F{row}",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [[]])
        cells = (current[0] + [None] * 5)[:5]
        desired = {
            "B": report.machine_revenue,
            "C": report.service_revenue,
            "E": report.cash,
            "F": report.momo,
        }
        indexes = {"B": 0, "C": 1, "E": 3, "F": 4}
        conflicts = {}
        for column, value in desired.items():
            old = self._number(cells[indexes[column]])
            if old not in (None, 0, value):
                conflicts[column] = {"current": old, "new": value}
        if conflicts and not overwrite:
            raise SheetConflictError(report, title, row, conflicts)
        if dry_run:
            return {"status": "dry_run", "sheet": title, "row": row, "values": desired}

        body = {"valueInputOption": "USER_ENTERED", "data": [
            {"range": f"'{title}'!{column}{row}", "values": [[value]]}
            for column, value in desired.items()
        ]}
        self.service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body=body
        ).execute()
        return {"status": "written", "sheet": title, "row": row, "values": desired}
