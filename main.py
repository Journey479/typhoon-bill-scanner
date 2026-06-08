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
from datetime import datetime
from typing import Optional

import requests
import functions_framework
from PIL import Image
from pydantic import BaseModel, Field
from openai import OpenAI

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


# ==============================================================================
# CONFIG — ความลับอ่านจาก env / ค่าที่ไม่ลับเติมไว้ให้ (override ผ่าน env ได้)
# ==============================================================================
TYPHOON_API_KEYS = [k.strip() for k in os.getenv("TYPHOON_API_KEYS", "").split(",") if k.strip()]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# โทเคนกันคนนอกยิง endpoint มั่ว — ต้องตรงกับ TRIGGER_TOKEN ใน code.gs (ตั้งผ่าน env)
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN", "").strip()

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
DEFAULT_AUTORUN = os.getenv("DEFAULT_AUTORUN", "on")  # ถ้ายังไม่มีไฟล์สถานะ ให้ถือว่าเปิด

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

HEADERS = [
    "วันที่บิล", "เดือนที่บันทึก", "ประเภทบิล", "เลขที่บิล",
    "ชื่อผู้ขาย (รวมสาขา)", "ที่อยู่ผู้ขาย", "ชื่อผู้ซื้อ", "ที่อยู่ผู้ซื้อ",
    "รายการ", "มูลค่าสินค้า", "จำนวนภาษี (VAT)", "จำนวนเงินรวม",
    "ความน่าเชื่อถือ(%)", "หมายเหตุ", "ลิงก์รูปภาพใน Drive",
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


TYPHOON_PROMPT = """
คุณคือผู้เชี่ยวชาญสกัดข้อมูลจากเอกสารบัญชีไทย ข้อความด้านล่างมาจากการ OCR เอกสาร 1 ใบ (อาจมีตัวอักษรเพี้ยน บรรทัดสลับ หรือ noise)
ตีความอย่างรอบคอบแล้วสกัดข้อมูลตามกฎต่อไปนี้

[ขั้นที่ 1: คัดกรอง]
- เป็นเอกสารบัญชีหรือไม่ (ใบเสร็จ, ใบกำกับภาษี, บิลเงินสด, ใบส่งสินค้า)?
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

--- ข้อความ OCR จากเอกสาร ---
"""


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


def check_and_create_headers(sheet_service, spreadsheet_id, tab_name):
    res = sheet_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1:A1").execute()
    values = res.get("values", [])
    if not values or not values[0] or values[0][0] == "":
        sheet_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
            valueInputOption="USER_ENTERED", body={"values": [HEADERS]}).execute()


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
# TELEGRAM
# ==============================================================================
def notify_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "true"},
            timeout=15)
    except Exception as e:
        print(f"⚠️ ส่ง Telegram ไม่สำเร็จ: {e}")


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


def process_cycle(drive_service, sheet_service, manual=False):
    """ประมวลผลไฟล์ทั้งหมดใน Inbox 1 รอบ คืนค่า (success, error, timeout, total)

    manual=True (สั่งจากคำสั่ง run/scan) → แจ้งกลับ Telegram แม้ Inbox ว่าง
    เพื่อให้รู้ว่าคำสั่งทำงานแล้วจริง (auto/scheduler จะเงียบไว้ ไม่สแปม)
    """
    reset_processing_to_inbox(drive_service)

    files = list_files_in_folder(drive_service, FOLDER_INBOX)
    bills = [f for f in files if ("image" in f["mimeType"] or "pdf" in f["mimeType"])]
    if not bills:
        if manual:
            notify_telegram("ℹ️ (Cloud Run) ไม่มีไฟล์ใน Inbox ให้ประมวลผล")
        return 0, 0, 0, 0

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
        select_typhoon_key(doc_counter)
        print(f"⚡ [{idx}/{total}] {file_name} [คีย์ {current_key_index + 1}/{len(TYPHOON_API_KEYS)}]")

        try:
            move_file(drive_service, file_id, FOLDER_PROCESSING)
            raw = download_file_to_bytes(drive_service, file_id)
            image_bytes = _compress_image(raw) if "image" in mime_type else raw

            bill = analyze_receipt(image_bytes, mime_type)
            if not bill.get("is_valid_bill"):
                print("  ⚠️ ไม่ใช่บิล -> Error")
                move_file(drive_service, file_id, FOLDER_ERROR)
                error += 1
                continue

            recorded_month = datetime.now().strftime("%Y-%m")
            drive_link = f"https://drive.google.com/file/d/{file_id}/view"
            row = [
                bill.get("date"), recorded_month, bill.get("bill_type"), bill.get("invoice_no"),
                bill.get("seller_name"), bill.get("seller_address"), bill.get("buyer_name"),
                bill.get("buyer_address"), bill.get("items_summary"), bill.get("subtotal"),
                bill.get("vat"), bill.get("total"), bill.get("confidence"), bill.get("note"), drive_link,
            ]
            append_to_sheet(sheet_service, SPREADSHEET_ID, SHEET_TAB_NAME, row)
            target = get_or_create_monthly_folder(drive_service, FOLDER_SUCCESS, recorded_month)
            move_file(drive_service, file_id, target)
            print(f"  ✅ สำเร็จ -> Success/{recorded_month}/")
            file_elapsed = int((datetime.now() - file_start).total_seconds())
            notify_telegram(f"✅ (Cloud Run)สแกนบิลสำเร็จ[{idx}/{total}] {file_name} ใช้เวลา {file_elapsed} วินาที")
            success += 1

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

            move_file(drive_service, file_id, FOLDER_ERROR)
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


def _services():
    global _DRIVE, _SHEET
    if _DRIVE is None:
        creds = get_credentials()
        _DRIVE = get_drive_service(creds)
        _SHEET = get_sheet_service(creds)
        check_and_create_headers(_SHEET, SPREADSHEET_ID, SHEET_TAB_NAME)
    return _DRIVE, _SHEET


@functions_framework.http
def scan(request):
    """ทำงาน 1 รอบต่อ 1 request แล้วจบ (ถูกยิงจาก code.gs ตอนมีไฟล์ใหม่ หรือ Cloud Scheduler)"""
    # กันคนนอกยิง endpoint มั่ว (ถ้าตั้ง TRIGGER_TOKEN ไว้)
    if TRIGGER_TOKEN:
        token = request.args.get("token") or request.headers.get("X-Trigger-Token", "")
        if token != TRIGGER_TOKEN:
            return ({"error": "forbidden"}, 403)

    if not TYPHOON_API_KEYS:
        return ({"error": "TYPHOON_API_KEYS not set"}, 500)

    try:
        drive_service, sheet_service = _services()
    except Exception as e:
        print(f"❌ init error: {e}")
        return ({"error": f"init failed: {e}"}, 500)

    # คำสั่ง run/scan ส่ง ?force=1 มา → บังคับประมวลผลแม้ autorun=off
    force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")

    # เคารพสวิตช์ autorun:on/off เดียวกับ Telegram (ยกเว้นสั่ง force มา)
    state = read_autorun_state(drive_service)
    if state != "on" and not force:
        return ({"status": "skipped", "reason": "autorun off"}, 200)

    success, error, timeout, total = process_cycle(drive_service, sheet_service, manual=force)
    return ({
        "status": "ok",
        "processed": total,
        "success": success,
        "error": error,
        "timeout": timeout,
    }, 200)
