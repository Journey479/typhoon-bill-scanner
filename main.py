"""
==============================================================================
Document Scanner — Google Cloud Run (functions-framework) worker
==============================================================================
เวอร์ชันสำหรับรันบน Google Cloud Run แบบ "ฟังก์ชัน" (ไม่ต้องเขียน Dockerfile เอง —
deploy ด้วย `gcloud run deploy --source .` แล้ว buildpacks สร้าง container ให้)

ต่างจากเวอร์ชัน Colab/scanner ตรงที่:
  - ไม่มี while-loop: ทำงาน "1 รอบ ต่อ 1 request" แล้วจบ (stateless) -> scale-to-zero ได้
  - ไม่อัปโหลด JSON: ใช้ Application Default Credentials (ADC) จาก Service Account
    ที่แนบกับ Cloud Run (`--service-account=...`) — SA ตัวเดิมที่แชร์ Drive/Sheet ไว้แล้ว
  - ความลับอ่านจาก Environment Variables (ตั้งตอน deploy หรือผ่าน Secret Manager)

ถูกกระตุ้นได้ 2 ทาง:
  1. code.gs ยิง HTTP มาทันทีที่มีไฟล์ใหม่เข้า Inbox (event-driven — หลักของไฟล์นี้)
  2. (ออปชัน) Cloud Scheduler ยิงเป็นระยะ เป็น safety-net เผื่อไฟล์เข้าทางอื่น

วิธี deploy ดูใน CloudRun/README.md
==============================================================================
"""

import io
import os
import json
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
import functions_framework
from PIL import Image
from pydantic import BaseModel, Field
from openai import OpenAI

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Google Vision + Gemini SDK (ออปชัน — ถ้าไม่ได้ติดตั้ง/ไม่ตั้งคีย์ จะใช้ Typhoon ล้วน)
try:
    from google.cloud import vision
    from google import genai
    from google.genai import types
    _GOOGLE_SDK_OK = True
except ImportError:
    vision = genai = types = None
    _GOOGLE_SDK_OK = False


# ==============================================================================
# CONFIG — ความลับอ่านจาก env / ค่าที่ไม่ลับเติมไว้ให้ (override ผ่าน env ได้)
# ==============================================================================
TYPHOON_API_KEYS = [k.strip() for k in os.getenv("TYPHOON_API_KEYS", "").split(",") if k.strip()]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# โทเคนกันคนนอกยิง endpoint มั่ว — ต้องตรงกับ TRIGGER_TOKEN ใน code.gs (ตั้งผ่าน env)
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()

# --- Google Vision + Gemini (เครื่องยนต์หลัก ใช้โควตาฟรีก่อน แล้วค่อย fallback ไป Typhoon) ---
# Vision OCR ใช้สิทธิ์ Service Account ที่แนบกับ Cloud Run (ADC) — ต้องเปิด Cloud Vision API + Billing
# Gemini ใช้ API key จาก Google AI Studio (โควตาฟรีรายวันต่อ key)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = {
    "flash":      os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    "flash_lite": os.getenv("GEMINI_FLASH_LITE_MODEL", "gemini-2.5-flash-lite"),
}
# โควตาฟรีต่อโมเดลต่อวัน (2 โมเดล = 40 ไฟล์/วัน) แล้วรีเซ็ตตอน RESET_HOUR ทุกวัน
GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_LIMIT_PER_MODEL", "20"))
# 503/โหลดหนักเป็น error ชั่วคราว — retry โมเดลละกี่ครั้งก่อนข้ามไปโมเดลถัดไป/Typhoon (+backoff วินาที)
GEMINI_MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "2"))
GEMINI_RETRY_BACKOFF = int(os.getenv("GEMINI_RETRY_BACKOFF", "2"))
QUOTA_RESET_HOUR = int(os.getenv("GOOGLE_QUOTA_RESET_HOUR", "7"))   # 07:00 น.
QUOTA_TZ_OFFSET = int(os.getenv("GOOGLE_QUOTA_TZ_OFFSET", "7"))     # เขตเวลา (ไทย = UTC+7)
# เพดาน Vision OCR ต่อเดือน — กันทะลุโควตาฟรี (Vision ฟรี 1000 หน้า/เดือน) ครบแล้วสลับไป Typhoon
VISION_MONTHLY_LIMIT = int(os.getenv("VISION_MONTHLY_LIMIT", "1000"))

# client ของ Gemini (สร้างครั้งเดียว) — None = ปิดเครื่องยนต์ Google
client_gemini = genai.Client(api_key=GEMINI_API_KEY) if (GEMINI_API_KEY and _GOOGLE_SDK_OK) else None
GOOGLE_ENABLED = client_gemini is not None

# --- ค่าที่ไม่ลับ (เติมไว้ให้จากโปรเจกต์เดิม override ด้วย env ได้) ---
TYPHOON_BASE_URL = os.getenv("TYPHOON_BASE_URL", "https://api.opentyphoon.ai/v1")
TYPHOON_OCR_MODEL = os.getenv("TYPHOON_OCR_MODEL", "typhoon-ocr")
TYPHOON_OCR_TASK_TYPE = os.getenv("TYPHOON_OCR_TASK_TYPE", "default")
TYPHOON_LLM_MODEL = os.getenv("TYPHOON_LLM_MODEL", "typhoon-v2.5-30b-a3b-instruct")
OCR_TIMEOUT = int(os.getenv("TYPHOON_OCR_TIMEOUT", "120"))
OCR_CONNECT_TIMEOUT = 10
OCR_MAX_TOKENS = int(os.getenv("TYPHOON_OCR_MAX_TOKENS", "4096"))

# --- ค่าเฉพาะโปรเจกต์: ตั้งผ่าน env.yaml ทั้งหมด (ไม่ฝัง ID จริงในโค้ด — เป็น template) ---
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SHEET_TAB_NAME = os.getenv("SHEET_TAB_NAME", "Raw_Data")

FOLDER_INBOX = os.getenv("FOLDER_INBOX", "")
FOLDER_PROCESSING = os.getenv("FOLDER_PROCESSING", "")
FOLDER_SUCCESS = os.getenv("FOLDER_SUCCESS", "")
FOLDER_ERROR = os.getenv("FOLDER_ERROR", "")

# โฟลเดอร์เก็บไฟล์สถานะ autorun (ต้องตรงกับ code.gs) — ใช้ Success เพราะ worker ไม่สแกน
STATE_FOLDER_ID = FOLDER_SUCCESS
AUTORUN_FILE = "_autorun_state.txt"
CHATS_FILE = "_telegram_chats.txt"         # รายชื่อ chat id (code.gs เขียน, worker อ่านไป broadcast)
# ตัวนับโควตา Gemini/Vision เก็บในแท็บของ Google Sheet (ไม่ใช่ไฟล์ Drive) เพราะ Service Account
# สร้างไฟล์ใหม่ใน My Drive ไม่ได้ (storageQuotaExceeded) แต่แก้ชีตที่มีอยู่แล้วได้ปกติ
QUOTA_SHEET_TAB = os.getenv("QUOTA_SHEET_TAB", "_QuotaState")  # แท็บเก็บตัวนับ (เซลล์ A1 = JSON)
ERROR_SHEET_TAB = os.getenv("ERROR_SHEET_TAB", "Error")        # แท็บบันทึกเอกสารที่ Error (กรอกเองต่อได้)
DEFAULT_AUTORUN = os.getenv("DEFAULT_AUTORUN", "on")  # ถ้ายังไม่มีไฟล์สถานะ ให้ถือว่าเปิด

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/cloud-platform",   # สำหรับเรียก Cloud Vision API ด้วย SA
]

HEADERS = [
    "วันที่บิล", "เดือนที่บันทึก", "ประเภทบิล", "เลขที่บิล",
    "ชื่อผู้ขาย (รวมสาขา)", "ที่อยู่ผู้ขาย", "ชื่อผู้ซื้อ", "ที่อยู่ผู้ซื้อ",
    "รายการ", "มูลค่าสินค้า", "จำนวนภาษี (VAT)", "จำนวนเงินรวม",
    "ความน่าเชื่อถือ(%)", "หมายเหตุ", "ลิงก์รูปภาพใน Drive",
    "เวลาที่บันทึก", "โมเดลที่ประมวลผล", "เวลาที่ใช้ประมวลผล (วินาที)",
]

# หัวคอลัมน์แท็บ Error (อยู่ "แถว 2" — แถว 1 เป็นแถวควบคุม "ย้ายเข้าบิลดี" + Dropdown Yes/No)
# A = บิลดี (checkbox) ; ที่เหลือ = ข้อมูลที่ยกขึ้น Raw_Data ได้เมื่อยืนยันว่าเป็น "บิลดี"
ERROR_HEADERS = [
    "บิลดี", "หมายเหตุ", "ลิงก์รูปภาพใน Drive",
    "เวลาที่บันทึก", "โมเดลที่ประมวลผล", "เวลาที่ใช้ประมวลผล (วินาที)",
]

# GLOBAL: คีย์ปัจจุบัน + client (วนใหม่ต่อเอกสาร)
current_key_index = 0


def build_typhoon_client(api_key):
    return OpenAI(base_url=TYPHOON_BASE_URL, api_key=api_key, timeout=60, max_retries=2)


client_typhoon = build_typhoon_client(TYPHOON_API_KEYS[current_key_index]) if TYPHOON_API_KEYS else None


def select_typhoon_key(doc_number):
    """หมุนคีย์ round-robin ต่อเอกสาร (ใบ 1->key1, ใบ 2->key2, ... ; คีย์เดียวไม่หมุน)"""
    global current_key_index, client_typhoon
    if not TYPHOON_API_KEYS:
        return 0
    current_key_index = (doc_number - 1) % len(TYPHOON_API_KEYS)
    client_typhoon = build_typhoon_client(TYPHOON_API_KEYS[current_key_index])
    return current_key_index


# ==============================================================================
# SCHEMA + PROMPT
# ==============================================================================
class BillVerification(BaseModel):
    is_valid_bill: bool = Field(description="true/false เป็นเอกสารบัญชีหรือไม่")
    date: Optional[str] = Field(None)
    bill_type: Optional[str] = Field(None)
    invoice_no: Optional[str] = Field(None)
    seller_name: Optional[str] = Field(None)
    seller_address: Optional[str] = Field(None)
    buyer_name: Optional[str] = Field(None)
    buyer_address: Optional[str] = Field(None)
    items_summary: Optional[str] = Field(None)
    subtotal: Optional[float] = Field(None)
    vat: Optional[float] = Field(None)
    total: Optional[float] = Field(None)
    confidence: Optional[int] = Field(None)
    note: Optional[str] = Field(None)


_BASE_PROMPT = """
คุณคือผู้เชี่ยวชาญสกัดข้อมูลจากเอกสารบัญชีไทย ข้อความด้านล่างมาจากการ OCR เอกสาร 1 ใบ (อาจมีตัวอักษรเพี้ยน บรรทัดสลับ หรือ noise)
ตีความอย่างรอบคอบแล้วสกัดข้อมูลตามกฎต่อไปนี้

[ขั้นที่ 1: คัดกรอง]
- เป็นเอกสารบัญชีหรือไม่ (ใบเสร็จ, ใบกำกับภาษี, บิลเงินสด, ใบส่งสินค้า)?
- บิลเงินสด/ใบเสร็จเงินสด ถือเป็นบิลเสมอ (is_valid_bill = true) อย่าปฏิเสธเพราะไม่มีเลขผู้เสียภาษีหรือ VAT
- เอกสารที่ปนภาษาไทย/อังกฤษ/จีน ถือเป็นเรื่องปกติ ไม่ใช่เหตุให้ปฏิเสธหรือลด confidence
- ถ้าไม่ใช่ หรือเป็นข้อความขยะ/อ่านไม่ออก -> is_valid_bill = false และทุกฟิลด์ที่เหลือเป็น null ทันที
- ถ้าใช่ -> is_valid_bill = true แล้วทำขั้นที่ 2

[ขั้นที่ 2: กฎการสกัด (เข้มงวด)]
1. seller_name: ชื่อบริษัท/ร้านค้า + สาขาต่อท้ายเสมอ เช่น 'บมจ. ซีพี ออลล์ (สาขาที่ 01234)', 'บริษัท บิ๊กซี (สำนักงานใหญ่)' (ยึดภาษาไทย)
2. seller_address / buyer_address: ที่อยู่เต็ม + เลขประจำตัวผู้เสียภาษี 13 หลักของฝั่งนั้นๆ
3. date: แปลงเป็น YYYY-MM-DD เสมอ; ถ้าปีเป็น พ.ศ. (มากกว่า 2400) ให้ลบ 543 เป็น ค.ศ. (เช่น 2567 -> 2024)
4. ตัวเลข (subtotal, vat, total): เอาเฉพาะตัวเลข ตัด comma, 'บาท', สัญลักษณ์เงินทิ้ง ใช้ทศนิยมล้วน (เช่น 1500.00)
5. หลักบัญชี: total = subtotal + vat (โดยทั่วไป vat = 7%); ถ้าตัวเลขไม่สอดคล้อง ให้ยึดตัวเลขที่ปรากฏชัดในเอกสารและลด confidence และถ้าไม่มี vat ห้ามเติมเองเด็ดขาด
6. ห้ามเดา/แต่งตัวเลขเด็ดขาด — ฟิลด์ใดยืนยันจาก OCR ไม่ได้ ให้เป็น null
7. confidence (0-100): 100 = OCR ชัด ข้อมูลครบ ตัวเลขลงตัว; ลดลงเมื่อข้อความเพี้ยน/ฟิลด์สำคัญหาย/ตัวเลขไม่ลงตัว
8. note: ถ้า confidence ไม่เต็ม 100 ระบุเหตุผลสั้นๆ หรือฟิลด์ที่ไม่มั่นใจ; ถ้าชัดทุกอย่างใส่ null

[ขั้นที่ 3: รูปแบบผลลัพธ์]
ตอบกลับเป็น JSON object เดียวเท่านั้น ห้ามมีข้อความอื่น ห้ามครอบด้วย ```json และต้องมีคีย์ครบทุกตัวตามนี้:
{
  "is_valid_bill": boolean, "date": "YYYY-MM-DD หรือ null", "bill_type": "'ใบกำกับภาษี'|'บิลเงินสด'|'อื่นๆ' หรือ null",
  "invoice_no": "string|null", "seller_name": "string|null", "seller_address": "string|null",
  "buyer_name": "string|null", "buyer_address": "string|null", "items_summary": "string|null",
  "subtotal": number|null, "vat": number|null, "total": number|null, "confidence": integer 0-100|null, "note": "string|null"
}
"""

# ใช้ต่อท้ายด้วยข้อความ OCR (ใช้ได้ทั้ง Typhoon OCR และ Google Vision OCR)
TYPHOON_PROMPT = _BASE_PROMPT + "\n--- ข้อความ OCR จากเอกสาร ---\n"


def _parse_json_object(raw_text):
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# ==============================================================================
# GOOGLE SERVICES + DRIVE HELPERS
# ==============================================================================
def get_credentials():
    """ADC — บน Cloud Run คือ Service Account ที่แนบไว้ (ไม่ต้องมีไฟล์ JSON)"""
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def get_drive_service(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_sheet_service(creds):
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def list_files_in_folder(service, folder_id):
    q = f"'{folder_id}' in parents and trashed = false"
    return service.files().list(q=q, fields="files(id, name, mimeType)").execute().get("files", [])


def move_file(service, file_id, dest_folder_id):
    try:
        f = service.files().get(fileId=file_id, fields="parents").execute()
        service.files().update(
            fileId=file_id, addParents=dest_folder_id,
            removeParents=",".join(f.get("parents", [])), fields="id, parents",
        ).execute()
    except Exception as e:
        print(f"⚠️ ย้ายไฟล์ {file_id} ไม่ได้: {e}")


def download_file_to_bytes(service, file_id):
    request = service.files().get_media(fileId=file_id)
    stream = io.BytesIO()
    downloader = MediaIoBaseDownload(stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return stream.getvalue()


def get_or_create_monthly_folder(service, parent_folder_id, folder_name):
    q = (f"name = '{folder_name}' and '{parent_folder_id}' in parents "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    files = service.files().list(q=q, fields="files(id, name)").execute().get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]}
    return service.files().create(body=meta, fields="id").execute().get("id")


def check_and_create_headers(sheet_service, spreadsheet_id, tab_name, headers=HEADERS):
    res = sheet_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1:A1").execute()
    values = res.get("values", [])
    if not values or not values[0] or values[0][0] == "":
        sheet_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
            valueInputOption="USER_ENTERED", body={"values": [headers]}).execute()


def append_to_sheet(sheet_service, spreadsheet_id, tab_name, data_list):
    sheet_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [data_list]}).execute()


def check_folder_counts(drive_service):
    """นับจำนวนไฟล์ในแต่ละโฟลเดอร์ปัจจุบัน"""
    inbox_files = list_files_in_folder(drive_service, FOLDER_INBOX)
    process_files = list_files_in_folder(drive_service, FOLDER_PROCESSING)
    error_files = list_files_in_folder(drive_service, FOLDER_ERROR)

    current_month_str = datetime.now().strftime("%Y-%m")
    query = (f"name = '{current_month_str}' and '{FOLDER_SUCCESS}' in parents "
             f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")

    try:
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        monthly_folders = results.get('files', [])
        if monthly_folders:
            current_month_folder_id = monthly_folders[0]['id']
            success_files = list_files_in_folder(drive_service, current_month_folder_id)
            success_count_str = f"{len(success_files)} ไฟล์"
        else:
            success_count_str = "0 ไฟล์ (ยังไม่ถูกสร้าง)"
    except Exception as e:
        success_count_str = f"ไม่สามารถตรวจสอบได้ ({e})"

    return f"""\n
--------------------------------------------------
📊 รายงานจำนวนไฟล์ในระบบ Google Drive ปัจจุบัน:
--------------------------------------------------
📥 1. โฟลเดอร์ Inbox      มีไฟล์ค้างอยู่: {len(inbox_files)} ไฟล์
⚡ 2. โฟลเดอร์ Processing มีไฟล์ค้างอยู่: {len(process_files)} ไฟล์
❌ 3. โฟลเดอร์ Error      มีไฟล์ค้างอยู่: {len(error_files)} ไฟล์
✅ 4. โฟลเดอร์ Success ({current_month_str}): {success_count_str}
--------------------------------------------------"""


# ==============================================================================
# AUTORUN STATE (อ่านไฟล์สถานะที่ code.gs เขียนไว้บน Drive)
# ==============================================================================
def read_autorun_state(drive_service):
    """อ่าน _autorun_state.txt จาก STATE_FOLDER_ID -> 'on' / 'off' (ไม่มีไฟล์ = DEFAULT_AUTORUN)"""
    q = (f"name = '{AUTORUN_FILE}' and '{STATE_FOLDER_ID}' in parents and trashed = false")
    files = drive_service.files().list(
        q=q, fields="files(id, modifiedTime)", orderBy="modifiedTime desc").execute().get("files", [])
    if not files:
        return DEFAULT_AUTORUN
    try:
        content = download_file_to_bytes(drive_service, files[0]["id"]).decode("utf-8").strip().lower()
        return content if content in ("on", "off") else DEFAULT_AUTORUN
    except Exception:
        return DEFAULT_AUTORUN


# ==============================================================================
# GOOGLE QUOTA STATE (ตัวนับ Gemini ต่อวัน — เก็บเป็น JSON บน Drive, รีเซ็ตตอน 07:00)
# ==============================================================================
def _quota_day():
    """วันโควตาปัจจุบัน (รอบใหม่เริ่มตอน QUOTA_RESET_HOUR ตามเขตเวลา QUOTA_TZ_OFFSET)
    ไทย UTC+7 + reset 07:00  ->  ตรงกับเที่ยงคืน UTC พอดี"""
    shifted = datetime.utcnow() + timedelta(hours=QUOTA_TZ_OFFSET - QUOTA_RESET_HOUR)
    return shifted.strftime("%Y-%m-%d")


def ensure_sheet_tab(sheet_service, tab_name):
    """สร้างแท็บ tab_name ถ้ายังไม่มี (แค่เพิ่มแท็บในสเปรดชีตเดิม ไม่สร้างไฟล์ใหม่ -> SA ทำได้)"""
    try:
        meta = sheet_service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID, fields="sheets.properties.title").execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if tab_name not in titles:
            sheet_service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}).execute()
            print(f"🆕 สร้างแท็บ: {tab_name}")
    except Exception as e:
        print(f"⚠️ ตรวจ/สร้างแท็บ {tab_name} ไม่ได้: {e}")


def ensure_quota_tab(sheet_service):
    """สร้างแท็บ QUOTA_SHEET_TAB ถ้ายังไม่มี (เก็บตัวนับโควตาในเซลล์ A1)"""
    ensure_sheet_tab(sheet_service, QUOTA_SHEET_TAB)


def get_sheet_id(sheet_service, tab_name):
    """คืน sheetId (gid) ของแท็บ — ใช้กับ batchUpdate ที่อ้าง GridRange (เช่น ตั้ง data validation)"""
    meta = sheet_service.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID, fields="sheets.properties(sheetId,title)").execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    return None


def ensure_error_tab_layout(sheet_service):
    """ตั้งโครงแท็บ Error ครั้งเดียว (ทำงานเมื่อ A2 ยังว่าง):
        แถว 1 = ตัวควบคุม  B1 = 'ย้ายเข้าบิลดี', C1 = Dropdown Yes/No (ค่าเริ่ม No)
        แถว 2 = หัวคอลัมน์ (ERROR_HEADERS)
    หมายเหตุ: checkbox คอลัมน์ A ใส่ "ทีละแถว" ตอนมีข้อมูลใหม่ (ดู log_error_to_sheet)
    ไม่ใส่ทั้งคอลัมน์ เพราะจะทำให้ทุกเซลล์มีค่า FALSE -> นับแถวเพี้ยน + checkbox โผล่ทุกแถวว่าง
    การ์ดด้วย try/except — ตั้งไม่ได้ก็ไม่ทำให้รอบประมวลผลพัง"""
    try:
        res = sheet_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{ERROR_SHEET_TAB}!A2").execute()
        if res.get("values"):
            return   # ตั้งหัวคอลัมน์ไว้แล้ว -> ข้าม
        sheet_id = get_sheet_id(sheet_service, ERROR_SHEET_TAB)
        # 1) ข้อความ: B1 label, C1 = No, หัวคอลัมน์ A2:F2
        sheet_service.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": [
                {"range": f"{ERROR_SHEET_TAB}!B1", "values": [["ย้ายเข้าบิลดี"]]},
                {"range": f"{ERROR_SHEET_TAB}!C1", "values": [["No"]]},
                {"range": f"{ERROR_SHEET_TAB}!A2", "values": [ERROR_HEADERS]},
            ]}).execute()
        if sheet_id is not None:
            # 2) data validation: dropdown Yes/No ที่ C1 (checkbox ใส่ทีละแถวใน log_error_to_sheet)
            sheet_service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": [
                    {"setDataValidation": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 2, "endColumnIndex": 3},
                        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                            {"userEnteredValue": "Yes"}, {"userEnteredValue": "No"}]},
                            "strict": True, "showCustomUi": True}}},
                ]}).execute()
        print(f"🆕 ตั้งโครงแท็บ {ERROR_SHEET_TAB} (แถวควบคุม + dropdown) แล้ว")
    except Exception as e:
        print(f"⚠️ ตั้งโครงแท็บ {ERROR_SHEET_TAB} ไม่ได้: {e}")


def log_error_to_sheet(sheet_service, note, drive_link, model, elapsed):
    """บันทึก 1 แถวลงแท็บ Error ตามโครงใหม่: [บิลดี(checkbox), หมายเหตุ, ลิงก์, เวลาบันทึก, โมเดล, เวลาประมวลผล]
    คอลัมน์ A เริ่มไม่ติ๊ก — เผื่อคนเปิดดูรูปแล้วยืนยันเป็น 'บิลดี' ภายหลัง (code.gs จะย้ายขึ้น Raw_Data ให้)
    ห้ามให้ความล้มเหลวของการ log มาทำให้รอบประมวลผลพัง จึง try/except ครอบทั้งหมด"""
    try:
        ensure_sheet_tab(sheet_service, ERROR_SHEET_TAB)
        ensure_error_tab_layout(sheet_service)
        timestamp = (datetime.now() + timedelta(hours=QUOTA_TZ_OFFSET)).strftime("%Y-%m-%d %H:%M:%S")
        row = [False, note or "", drive_link, timestamp, model or "", elapsed]
        # ข้อมูลเริ่มแถว 3 — นับแถวที่มีอยู่จากคอลัมน์ A (แต่ละแถวข้อมูลมีค่า checkbox อยู่แล้ว
        # และไม่มี checkbox ทั้งคอลัมน์ จึงนับเฉพาะแถวข้อมูลจริง ไม่เพี้ยน)
        existing = sheet_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{ERROR_SHEET_TAB}!A3:A").execute().get("values", [])
        next_row = 3 + len(existing)
        sheet_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{ERROR_SHEET_TAB}!A{next_row}",
            valueInputOption="USER_ENTERED", body={"values": [row]}).execute()
        # ใส่ checkbox เฉพาะ "แถวนี้แถวเดียว" (ไม่ทำทั้งคอลัมน์ -> กันนับแถวเพี้ยน + checkbox โผล่แถวว่าง)
        sheet_id = get_sheet_id(sheet_service, ERROR_SHEET_TAB)
        if sheet_id is not None:
            sheet_service.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": [{"setDataValidation": {
                    "range": {"sheetId": sheet_id, "startRowIndex": next_row - 1, "endRowIndex": next_row,
                              "startColumnIndex": 0, "endColumnIndex": 1},
                    "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}}]}).execute()
    except Exception as e:
        print(f"⚠️ บันทึกแท็บ {ERROR_SHEET_TAB} ไม่ได้: {e}")


def read_google_usage(sheet_service):
    """อ่านตัวนับ Gemini รายวัน {flash, flash_lite} + Vision รายเดือน {vision_count} จากเซลล์ A1 ของแท็บ
    คนละวัน -> รีเซ็ต Gemini ; คนละเดือน -> รีเซ็ต Vision"""
    day = _quota_day()
    month = datetime.utcnow().strftime("%Y-%m")   # Vision คิดตามเดือนปฏิทิน (billing)
    base = {"date": day, "flash": 0, "flash_lite": 0, "vision_month": month, "vision_count": 0}
    try:
        res = sheet_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"{QUOTA_SHEET_TAB}!A1").execute()
        values = res.get("values", [])
        if values and values[0] and values[0][0]:
            data = json.loads(values[0][0])
            if data.get("date") == day:          # วันเดียวกัน -> ใช้ค่าเดิม (คนละวัน = รีเซ็ต)
                base["flash"] = int(data.get("flash", 0))
                base["flash_lite"] = int(data.get("flash_lite", 0))
            if data.get("vision_month") == month:  # เดือนเดียวกัน -> ใช้ค่าเดิม (คนละเดือน = รีเซ็ต)
                base["vision_count"] = int(data.get("vision_count", 0))
    except Exception as e:
        print(f"⚠️ อ่านตัวนับโควตา ({QUOTA_SHEET_TAB}) ไม่ได้ (ถือว่าเริ่มศูนย์): {e}")
    return base


def write_google_usage(sheet_service, usage):
    payload = json.dumps({
        "date": usage["date"], "flash": usage["flash"], "flash_lite": usage["flash_lite"],
        "vision_month": usage["vision_month"], "vision_count": usage["vision_count"],
    }, ensure_ascii=False)
    try:
        sheet_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{QUOTA_SHEET_TAB}!A1",
            valueInputOption="RAW", body={"values": [[payload]]}).execute()
    except Exception as e:
        print(f"⚠️ เขียนตัวนับโควตา ({QUOTA_SHEET_TAB}) ไม่ได้: {e}")


# ==============================================================================
# TELEGRAM (รองรับหลายแชท — env TELEGRAM_CHAT_ID + ไฟล์ _telegram_chats.txt บน Drive)
# ==============================================================================
_CHAT_IDS = []   # cache ต่อรอบ (เซ็ตที่ต้น process_cycle)


def _split_ids(text):
    return [c for c in (text or "").replace(",", " ").split() if c]


def load_chat_ids(drive_service):
    """รวม chat id จาก env (seed) + ไฟล์ _telegram_chats.txt ที่ code.gs เขียนไว้ (ไม่ซ้ำ)"""
    ids = list(dict.fromkeys(_split_ids(TELEGRAM_CHAT_ID)))
    try:
        q = f"name = '{CHATS_FILE}' and '{STATE_FOLDER_ID}' in parents and trashed = false"
        files = drive_service.files().list(
            q=q, fields="files(id)", orderBy="modifiedTime desc").execute().get("files", [])
        if files:
            content = download_file_to_bytes(drive_service, files[0]["id"]).decode("utf-8")
            for c in _split_ids(content):
                if c not in ids:
                    ids.append(c)
    except Exception as e:
        print(f"⚠️ อ่าน {CHATS_FILE} ไม่ได้: {e}")
    return ids


def notify_telegram(text):
    """ส่งข้อความไปทุกแชทที่ลงทะเบียนไว้ (broadcast)"""
    if not TELEGRAM_TOKEN:
        return
    chat_ids = _CHAT_IDS if _CHAT_IDS else _split_ids(TELEGRAM_CHAT_ID)
    for cid in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": text, "disable_web_page_preview": "true"},
                timeout=15)
        except Exception as e:
            print(f"⚠️ ส่ง Telegram ({cid}) ไม่สำเร็จ: {e}")


# ==============================================================================
# STAGE 1 + 2
# ==============================================================================
def ocr_with_typhoon(file_bytes, mime_type):
    url = f"{TYPHOON_BASE_URL}/ocr"
    filename = "document.pdf" if "pdf" in mime_type else "image.jpg"
    files = {"file": (filename, file_bytes, mime_type)}
    data = {
        "model": TYPHOON_OCR_MODEL, "task_type": TYPHOON_OCR_TASK_TYPE,
        "max_tokens": str(OCR_MAX_TOKENS), "temperature": "0.1", "top_p": "0.6", "repetition_penalty": "1.2",
    }
    headers = {"Authorization": f"Bearer {TYPHOON_API_KEYS[current_key_index]}"}
    resp = requests.post(url, files=files, data=data, headers=headers,
                         timeout=(OCR_CONNECT_TIMEOUT, OCR_TIMEOUT))
    resp.raise_for_status()
    result = resp.json()
    texts = []
    for page in result.get("results", []):
        if page.get("success") and page.get("message"):
            content = page["message"]["choices"][0]["message"]["content"]
            try:
                content = json.loads(content).get("natural_text", content)
            except (json.JSONDecodeError, TypeError):
                pass
            texts.append(content)
        elif not page.get("success"):
            print(f"⚠️ OCR หน้า {page.get('filename', '?')} ล้มเหลว: {page.get('error', 'unknown')}")
    return "\n".join(texts)


def analyze_text_with_typhoon(full_text):
    response = client_typhoon.chat.completions.create(
        model=TYPHOON_LLM_MODEL,
        messages=[
            {"role": "system", "content": "คุณตอบกลับเป็น JSON object ที่ถูกต้องเท่านั้น"},
            {"role": "user", "content": TYPHOON_PROMPT + full_text},
        ],
        temperature=0.0, max_tokens=2048, response_format={"type": "json_object"},
    )
    data = _parse_json_object(response.choices[0].message.content)
    return BillVerification(**data).model_dump()


def analyze_receipt(image_bytes, mime_type):
    full_text = ocr_with_typhoon(image_bytes, mime_type)
    if not full_text or not full_text.strip():
        raise Exception("OCR ไม่พบข้อความในเอกสาร")
    return analyze_text_with_typhoon(full_text)


# ==============================================================================
# GOOGLE VISION + GEMINI ENGINE (โควตาฟรี — ใช้ก่อน Typhoon)
# อ้างอิงไปป์ไลน์ที่พิสูจน์แล้วใน "Google Vision code/scanner.py":
#   Vision OCR (รูป: document_text_detection / PDF: batch inline ≤5 หน้า) -> Gemini จัดเป็น JSON
# ==============================================================================
class NoEngineAvailable(Exception):
    """โควตา Google หมด และไม่ได้ตั้ง Typhoon ไว้ — ไม่มีเครื่องยนต์ให้ใช้"""


def _is_quota_error(e):
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s


def _is_transient_error(e):
    """error ชั่วคราวฝั่ง Gemini (โมเดลโหลดหนัก/เซิร์ฟเวอร์ล่ม) — ควร retry/ลองโมเดลถัดไป ไม่ใช่โควตาหมด"""
    s = str(e).lower()
    return ("503" in s or "unavailable" in s or "500" in s or "internal" in s
            or "overloaded" in s or "high demand" in s or "deadline" in s)


def ocr_with_vision(vision_client, file_bytes, mime_type):
    """Google Cloud Vision OCR -> ข้อความดิบ (รูปใช้ document_text_detection, PDF ใช้ batch inline ≤5 หน้า)"""
    if "pdf" in mime_type:
        input_config = vision.InputConfig(content=file_bytes, mime_type="application/pdf")
        features = [vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)]
        req = vision.AnnotateFileRequest(
            input_config=input_config, features=features, pages=[1, 2, 3, 4, 5])
        response = vision_client.batch_annotate_files(requests=[req])
        pages = response.responses[0].responses
        return "\n".join(p.full_text_annotation.text for p in pages if p.full_text_annotation.text)
    image = vision.Image(content=file_bytes)
    response = vision_client.document_text_detection(image=image)
    if response.error.message:
        raise Exception(f"Vision API error: {response.error.message}")
    return response.full_text_annotation.text


def analyze_text_with_gemini(full_text, model_name):
    """ส่งข้อความ OCR ให้ Gemini จัดเป็น JSON ตามสคีมา BillVerification (structured output)"""
    response = client_gemini.models.generate_content(
        model=model_name,
        contents=[TYPHOON_PROMPT + full_text],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BillVerification,
            temperature=0.0,
        ),
    )
    return BillVerification(**json.loads(response.text)).model_dump()


class NoGoogleQuota(Exception):
    """โควตา Google หมด (Vision รายเดือน หรือ Gemini รายวันเต็มทุกโมเดล) — ให้ใช้ Typhoon"""


def _google_attempt(vision_client, image_bytes, mime_type, sheet_service, usage):
    """Vision OCR ครั้งเดียว (นับโควตาเดือน) แล้วลอง Gemini ทีละโมเดล -> (bill, engine_label)
    raise NoGoogleQuota ถ้าโควตาหมด ; raise อื่น ๆ ถ้า Google error (ให้ตกไป Typhoon)"""
    if usage.get("vision_count", 0) >= VISION_MONTHLY_LIMIT:
        raise NoGoogleQuota(f"Vision ครบ {VISION_MONTHLY_LIMIT} หน้า/เดือนแล้ว")
    if usage.get("flash", 0) >= GEMINI_DAILY_LIMIT and usage.get("flash_lite", 0) >= GEMINI_DAILY_LIMIT:
        raise NoGoogleQuota("Gemini ครบโควตาวันแล้วทุกโมเดล")

    # --- Vision OCR (นับ 1 หน้าต่อ 1 เอกสาร — นับทันทีหลังเรียกสำเร็จ ก่อนลอง Gemini) ---
    ocr_text = ocr_with_vision(vision_client, image_bytes, mime_type)
    usage["vision_count"] = usage.get("vision_count", 0) + 1
    write_google_usage(sheet_service, usage)
    if not ocr_text or not ocr_text.strip():
        raise Exception("Vision OCR ไม่พบข้อความในเอกสาร")

    # --- Gemini วิเคราะห์ข้อความ (ลอง flash ก่อน หมดแล้วต่อ flash-lite) ---
    last_err = None
    for key in ("flash", "flash_lite"):
        if usage.get(key, 0) >= GEMINI_DAILY_LIMIT:
            continue
        model_name = GEMINI_MODELS[key]
        for attempt in range(GEMINI_MAX_ATTEMPTS):   # ลองโมเดลละหลายครั้ง เผื่อ 503 ชั่วคราว
            try:
                bill = analyze_text_with_gemini(ocr_text, model_name)
                usage[key] = usage.get(key, 0) + 1
                write_google_usage(sheet_service, usage)
                return bill, f"Google/{model_name}"
            except Exception as e:
                last_err = e
                if _is_quota_error(e):
                    print(f"  🔻 {model_name} โควตาหมด (429) -> ปิดโมเดลนี้ของวันนี้")
                    usage[key] = GEMINI_DAILY_LIMIT      # มาร์คว่าเต็ม กันยิงซ้ำ
                    write_google_usage(sheet_service, usage)
                    break                                # โควตาหมด -> ข้ามไปโมเดลถัดไป
                if _is_transient_error(e):
                    if attempt + 1 < GEMINI_MAX_ATTEMPTS:
                        wait = GEMINI_RETRY_BACKOFF * (attempt + 1)
                        print(f"  🔁 {model_name} ชั่วคราว (503/overloaded) -> รอ {wait}s ลองใหม่")
                        time.sleep(wait)
                        continue                         # ลองโมเดลเดิมอีกครั้ง
                    print(f"  🔁 {model_name} ยังล่ม -> ลองโมเดลถัดไป")
                    break                                # ครบจำนวนครั้ง -> โมเดลถัดไป
                raise                                    # Gemini error จริงอื่น -> ตกไป Typhoon
    # ลอง Gemini ครบทุกโมเดลแล้วยังไม่สำเร็จ (โควตาหมด/ล่มชั่วคราว) -> ให้ Typhoon รับช่วง
    raise NoGoogleQuota(str(last_err) if last_err else "Gemini ใช้ไม่ได้")


def analyze_with_best_engine(vision_client, image_bytes, mime_type, sheet_service, usage, doc_counter):
    """ลองฝั่ง Google (Vision+Gemini) จนเต็มโควตา แล้วค่อย fallback ไป Typhoon
    คืน (bill_dict, engine_label). อัปเดต/บันทึกตัวนับโควตาให้เอง"""
    if GOOGLE_ENABLED and vision_client is not None and usage is not None:
        try:
            return _google_attempt(vision_client, image_bytes, mime_type, sheet_service, usage)
        except NoGoogleQuota:
            pass   # โควตา Google หมด -> ใช้ Typhoon เงียบ ๆ
        except Exception as e:
            print(f"  ⚠️ Google ล้มเหลว: {e} -> ลอง Typhoon")

    # --- Typhoon (ไม่จำกัดจำนวน) ---
    if not TYPHOON_API_KEYS:
        raise NoEngineAvailable("โควตา Google หมด และไม่ได้ตั้ง TYPHOON_API_KEYS")
    select_typhoon_key(doc_counter)
    return analyze_receipt(image_bytes, mime_type), f"Typhoon[คีย์{current_key_index + 1}]"


# ==============================================================================
# PROCESSING CYCLE
# ==============================================================================
def reset_processing_to_inbox(drive_service):
    """กู้ไฟล์ที่ค้างใน Processing กลับเข้า Inbox (เผื่อรอบก่อน crash/timeout)"""
    stuck = list_files_in_folder(drive_service, FOLDER_PROCESSING)
    for f in stuck:
        move_file(drive_service, f["id"], FOLDER_INBOX)
        print(f"  ↩️ กู้ไฟล์ค้าง Processing -> Inbox: {f['name']}")
    return len(stuck)


def _compress_image(raw_bytes):
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.width > 1600:
            h = int(img.height * (1600 / img.width))
            img = img.resize((1600, h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return raw_bytes


def process_cycle(drive_service, sheet_service, vision_service, manual=False):
    """ประมวลผลไฟล์ทั้งหมดใน Inbox 1 รอบ คืนค่า (success, error, timeout, total)

    manual=True (สั่งจากคำสั่ง run/scan) → แจ้งกลับ Telegram แม้ Inbox ว่าง
    เพื่อให้รู้ว่าคำสั่งทำงานแล้วจริง (auto/scheduler จะเงียบไว้ ไม่สแปม)
    """
    global _CHAT_IDS
    _CHAT_IDS = load_chat_ids(drive_service)   # โหลดรายชื่อแชทไว้ broadcast ทุกข้อความ
    reset_processing_to_inbox(drive_service)

    files = list_files_in_folder(drive_service, FOLDER_INBOX)
    bills = [f for f in files if ("image" in f["mimeType"] or "pdf" in f["mimeType"])]
    if not bills:
        if manual:
            notify_telegram("ℹ️ (Cloud Run) ไม่มีไฟล์ใน Inbox ให้ประมวลผล")
        return 0, 0, 0, 0

    # โหลดตัวนับโควตา Google ของวันนี้ (None = ปิดเครื่องยนต์ Google -> ใช้ Typhoon ล้วน)
    google_ready = GOOGLE_ENABLED and vision_service is not None
    google_usage = read_google_usage(sheet_service) if google_ready else None
    if google_usage is not None:
        print(f"🔢 โควตา Gemini วันนี้: flash {google_usage['flash']}/{GEMINI_DAILY_LIMIT}, "
              f"flash_lite {google_usage['flash_lite']}/{GEMINI_DAILY_LIMIT} | "
              f"Vision เดือนนี้ {google_usage['vision_count']}/{VISION_MONTHLY_LIMIT} หน้า")

    start_time = datetime.now()
    total = len(bills)
    success = error = timeout = 0
    rate_limited_keys = set()
    doc_counter = 0
    print(f"\n📂 พบไฟล์ {total} ใบ — เริ่มประมวลผล {start_time.strftime('%H:%M:%S')}")

    for idx, f in enumerate(bills, start=1):
        file_id, file_name, mime_type = f["id"], f["name"], f["mimeType"]
        file_start = datetime.now()   # จับเวลาเริ่มของไฟล์นี้ (ใช้แจ้งเวลาต่อไฟล์)
        doc_counter += 1
        print(f"⚡ [{idx}/{total}] {file_name}")

        try:
            move_file(drive_service, file_id, FOLDER_PROCESSING)
            raw = download_file_to_bytes(drive_service, file_id)
            image_bytes = _compress_image(raw) if "image" in mime_type else raw

            bill, engine = analyze_with_best_engine(
                vision_service, image_bytes, mime_type, sheet_service, google_usage, doc_counter)
            print(f"  🤖 ใช้เครื่องยนต์: {engine}")
            if not bill.get("is_valid_bill"):
                print("  ⚠️ ไม่ใช่บิล -> Error")
                error_month = datetime.now().strftime("%Y-%m")
                error_target = get_or_create_monthly_folder(drive_service, FOLDER_ERROR, error_month)
                move_file(drive_service, file_id, error_target)
                note = bill.get("note") or "อ่านไม่ออก/ไม่ใช่เอกสารบัญชี"
                drive_link = f"https://drive.google.com/file/d/{file_id}/view"
                file_elapsed = int((datetime.now() - file_start).total_seconds())
                log_error_to_sheet(sheet_service, note, drive_link, engine, file_elapsed)
                notify_telegram(f"⚠️ (Cloud Run) ไม่ใช่บิล[{idx}/{total}] {file_name} "
                                f"-> ย้ายเข้า Error ({note}) [{engine}]")
                error += 1
                continue

            recorded_month = datetime.now().strftime("%Y-%m")
            drive_link = f"https://drive.google.com/file/d/{file_id}/view"
            file_elapsed = int((datetime.now() - file_start).total_seconds())
            processed_at = (datetime.now() + timedelta(hours=QUOTA_TZ_OFFSET)).strftime("%Y-%m-%d %H:%M:%S")
            row = [
                bill.get("date"), recorded_month, bill.get("bill_type"), bill.get("invoice_no"),
                bill.get("seller_name"), bill.get("seller_address"), bill.get("buyer_name"),
                bill.get("buyer_address"), bill.get("items_summary"), bill.get("subtotal"),
                bill.get("vat"), bill.get("total"), bill.get("confidence"), bill.get("note"), drive_link,
                processed_at, engine, file_elapsed,
            ]
            append_to_sheet(sheet_service, SPREADSHEET_ID, SHEET_TAB_NAME, row)
            target = get_or_create_monthly_folder(drive_service, FOLDER_SUCCESS, recorded_month)
            move_file(drive_service, file_id, target)
            print(f"  ✅ สำเร็จ -> Success/{recorded_month}/")
            notify_telegram(f"✅ (Cloud Run)สแกนบิลสำเร็จ[{idx}/{total}] {file_name} "
                            f"ใช้เวลา {file_elapsed} วินาที [{engine}]")
            success += 1

        except NoEngineAvailable as e:
            move_file(drive_service, file_id, FOLDER_INBOX)
            print(f"  🛑 {e} -> กลับ Inbox, หยุดรอบนี้ (รอโควตารีเซ็ต)")
            notify_telegram("🛑 (Cloud Run) โควตา Google หมด และไม่ได้ตั้ง Typhoon — "
                            "หยุดรอบนี้ รอโควตารีเซ็ตตอน 07:00")
            break

        except Exception as e:
            err = str(e)
            low = err.lower()
            print(f"  ❌ ผิดพลาด: {e}")
            is_rate_limit = "429" in err or "rate limit" in low or "RESOURCE_EXHAUSTED" in err
            is_timeout = "408" in err or "timed out" in low or "timeout" in low

            if is_rate_limit:
                rate_limited_keys.add(current_key_index)
                move_file(drive_service, file_id, FOLDER_INBOX)
                print("  🔑 rate limit -> กลับ Inbox")
                if len(rate_limited_keys) >= len(TYPHOON_API_KEYS):
                    print("  🛑 ทุกคีย์โดน rate limit — หยุดรอบนี้")
                    break
                continue
            if is_timeout:
                move_file(drive_service, file_id, FOLDER_INBOX)
                timeout += 1
                print("  ⏳ timeout -> กลับ Inbox (รอบหน้าลองใหม่)")
                continue

            error_month = datetime.now().strftime("%Y-%m")
            error_target = get_or_create_monthly_folder(drive_service, FOLDER_ERROR, error_month)
            move_file(drive_service, file_id, error_target)
            drive_link = f"https://drive.google.com/file/d/{file_id}/view"
            file_elapsed = int((datetime.now() - file_start).total_seconds())
            log_error_to_sheet(sheet_service, err, drive_link, None, file_elapsed)
            notify_telegram(f"❌ (Cloud Run) สแกนล้มเหลว[{idx}/{total}] {file_name} "
                            f"-> ย้ายเข้า Error\nสาเหตุ: {err}")
            error += 1

    end_time = datetime.now()
    elapsed = int((end_time - start_time).total_seconds())
    # tnote = f" | ⏳ timeout {timeout}" if timeout else ""
    # sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
    
    #print(f"✅ (Cloud Run)สแกนบิลสำเร็จ: {file_name} ใช้เวลา: {elapsed} วินาที")
    return success, error, timeout, total


# ==============================================================================
# CLOUD RUN ENTRY POINT (functions-framework)
# ==============================================================================
# cache services ระดับ instance (warm start ไม่ต้อง build ใหม่ทุก request)
_DRIVE = None
_SHEET = None
_VISION = None


def _services():
    global _DRIVE, _SHEET, _VISION
    if _DRIVE is None:
        creds = get_credentials()
        _DRIVE = get_drive_service(creds)
        _SHEET = get_sheet_service(creds)
        if GOOGLE_ENABLED:   # สร้าง Vision client จาก SA เดียวกัน (ADC) เมื่อเปิดเครื่องยนต์ Google
            try:
                _VISION = vision.ImageAnnotatorClient(credentials=creds)
            except Exception as e:
                print(f"⚠️ สร้าง Vision client ไม่ได้ (จะใช้ Typhoon ล้วน): {e}")
                _VISION = None
            ensure_quota_tab(_SHEET)   # แท็บเก็บตัวนับโควตา (SA สร้างแท็บในชีตเดิมได้ ไม่ติด storage quota)
        check_and_create_headers(_SHEET, SPREADSHEET_ID, SHEET_TAB_NAME)
        ensure_sheet_tab(_SHEET, ERROR_SHEET_TAB)   # แท็บ Error (สร้างล่วงหน้า)
        ensure_error_tab_layout(_SHEET)             # แถวควบคุม + หัวคอลัมน์(แถว2) + checkbox/dropdown
    return _DRIVE, _SHEET, _VISION


@functions_framework.http
def scan(request):
    """ทำงาน 1 รอบต่อ 1 request แล้วจบ (ถูกยิงจาก code.gs ตอนมีไฟล์ใหม่ หรือ Cloud Scheduler)"""
    # กันคนนอกยิง endpoint มั่ว (ถ้าตั้ง TRIGGER_TOKEN ไว้)
    if TRIGGER_TOKEN:
        token = request.args.get("token") or request.headers.get("X-Trigger-Token", "")
        if token != TRIGGER_TOKEN:
            return ({"error": "forbidden"}, 403)

    if not TYPHOON_API_KEYS and not GOOGLE_ENABLED:
        return ({"error": "ไม่ได้ตั้งเครื่องยนต์ OCR เลย — ต้องมี GEMINI_API_KEY หรือ TYPHOON_API_KEYS"}, 500)

    try:
        drive_service, sheet_service, vision_service = _services()
    except Exception as e:
        print(f"❌ init error: {e}")
        return ({"error": f"init failed: {e}"}, 500)

    # คำสั่ง run/scan ส่ง ?force=1 มา → บังคับประมวลผลแม้ autorun=off
    force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")

    # เคารพสวิตช์ autorun:on/off เดียวกับ Telegram (ยกเว้นสั่ง force มา)
    state = read_autorun_state(drive_service)
    if state != "on" and not force:
        return ({"status": "skipped", "reason": "autorun off"}, 200)

    success, error, timeout, total = process_cycle(drive_service, sheet_service, vision_service, manual=force)
    return ({
        "status": "ok",
        "processed": total,
        "success": success,
        "error": error,
        "timeout": timeout,
    }, 200)
