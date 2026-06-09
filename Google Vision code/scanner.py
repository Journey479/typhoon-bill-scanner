import io
import json
import os
import time
import platform
from PIL import Image
from datetime import datetime
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.cloud import vision
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import Optional

# ==============================================================================
# CONFIGURATION (โหลดความลับและค่าตั้งค่าจากไฟล์ .env)
# ==============================================================================
load_dotenv()

# Gemini API keys (คั่นด้วยจุลภาค , — หนึ่ง key ต่อหนึ่งบัญชี Google เพื่อหมุนเวียนโควตารายวัน RPD)
GEMINI_API_KEYS = [
    key.strip()
    for key in os.getenv("GEMINI_API_KEYS", "").split(",")
    if key.strip()
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# ID ของแต่ละโฟลเดอร์ Google Drive (ตั้งค่าในไฟล์ .env)
FOLDER_INBOX = os.getenv("FOLDER_INBOX")
FOLDER_PROCESSING = os.getenv("FOLDER_PROCESSING")
FOLDER_SUCCESS = os.getenv("FOLDER_SUCCESS")
FOLDER_ERROR = os.getenv("FOLDER_ERROR")

# ชื่อแท็บใน Google Sheet
SHEET_TAB_NAME = os.getenv("SHEET_TAB_NAME", "Raw_Data")

CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "config/credentials.json")  # ไฟล์สิทธิ์จาก Google Cloud

# ขอบเขตสิทธิ์: Drive + Sheets สำหรับจัดไฟล์/บันทึกตาราง และ cloud-platform สำหรับเรียก Vision API
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/cloud-platform",
]

# 🛠️ GLOBAL VARIABLES: ระบบติดตามสถานะโมเดลและคีย์ Gemini ปัจจุบัน
SELECTED_MODEL = "gemini-2.5-flash"
current_key_index = 0
client_gemini = genai.Client(api_key=GEMINI_API_KEYS[current_key_index]) if GEMINI_API_KEYS else None

# หัวคอลัมน์ของ Google Sheet
HEADERS = [
    "วันที่บิล", "เดือนที่บันทึก", "ประเภทบิล", "เลขที่บิล",
    "ชื่อผู้ขาย (รวมสาขา)", "ที่อยู่ผู้ขาย", "ชื่อผู้ซื้อ", "ที่อยู่ผู้ซื้อ",
    "รายการ", "มูลค่าสินค้า", "จำนวนภาษี (VAT)", "จำนวนเงินรวม",
    "ความน่าเชื่อถือ(%)", "หมายเหตุ", "ลิงก์รูปภาพใน Drive"
]


class BillVerification(BaseModel):
    is_valid_bill: bool = Field(description="true หรือ false ตามผลการคัดกรองว่าเป็นเอกสารบัญชีหรือไม่")
    date: Optional[str] = Field(None, description="วันที่บนบิล (YYYY-MM-DD) ถ้าไม่มีค่าให้ใส่ null")
    bill_type: Optional[str] = Field(None, description="ประเภทบิล เลือกเฉพาะ: 'ใบกำกับภาษี', 'บิลเงินสด' หรือ 'อื่นๆ'")
    invoice_no: Optional[str] = Field(None, description="เลขที่บิล ถ้าไม่มีค่าให้ใส่ null")
    seller_name: Optional[str] = Field(None, description="ชื่อผู้ขายรวมสาขา (เน้นไทยก่อน เช่น บริษัท... (สำนักงานใหญ่))")
    seller_address: Optional[str] = Field(None, description="ที่อยู่เต็มผู้ขายและเลขประจำตัวผู้เสียภาษีผู้ขาย ถ้าไม่มีค่าให้ใส่ null")
    buyer_name: Optional[str] = Field(None, description="ชื่อบริษัท/ชื่อบุคคลฝั่งผู้ซื้อ ถ้าไม่มีค่าให้ใส่ null")
    buyer_address: Optional[str] = Field(None, description="ที่อยู่เต็มและเลขผู้เสียภาษีฝั่งผู้ซื้อ ถ้าไม่มีค่าให้ใส่ null")
    items_summary: Optional[str] = Field(None, description="สรุปรายการสั้นๆ เช่น ซื้อวัสดุก่อสร้าง, ค่าอาหาร, ค่าน้ำมัน")
    subtotal: Optional[float] = Field(None, description="มูลค่าก่อนภาษี (ตัวเลขทศนิยม)")
    vat: Optional[float] = Field(None, description="จำนวนภาษี VAT (ตัวเลขทศนิยม)")
    total: Optional[float] = Field(None, description="จำนวนเงินรวมสุทธิ (ตัวเลขทศนิยม)")
    confidence: Optional[int] = Field(None, description="ระดับความน่าเชื่อถือของข้อมูลที่สกัดได้ เป็นเปอร์เซ็นต์เต็มจำนวน 0-100 (ประเมินจากความชัดเจนของข้อความ OCR และความครบถ้วน/สอดคล้องของตัวเลข)")
    note: Optional[str] = Field(None, description="หมายเหตุสั้นๆ เช่น เหตุผลที่ความน่าเชื่อถือต่ำ หรือฟิลด์ที่ไม่แน่ใจ/อ่านไม่ชัด ถ้าทุกอย่างชัดเจนให้ใส่ null")


# ==============================================================================
# GOOGLE SERVICE CONNECTORS
# ==============================================================================
def get_google_creds():
    return service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )

def get_drive_service(creds):
    return build("drive", "v3", credentials=creds)

def get_sheet_service(creds):
    return build("sheets", "v4", credentials=creds)

def get_vision_client(creds):
    return vision.ImageAnnotatorClient(credentials=creds)


# ==============================================================================
# GOOGLE DRIVE ADVANCED FUNCTIONS
# ==============================================================================
def list_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    return results.get("files", [])

def move_file(service, file_id, source_folder_id, dest_folder_id):
    try:
        file = service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(file.get("parents", []))
        service.files().update(
            fileId=file_id,
            addParents=dest_folder_id,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()
    except Exception as e:
        print(f"⚠️ ไม่สามารถย้ายไฟล์ {file_id} ได้: {e}")

def download_file_to_bytes(service, file_id):
    request = service.files().get_media(fileId=file_id)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return file_stream.getvalue()

def get_or_create_monthly_folder(service, parent_folder_id, folder_name):
    query = f"name = '{folder_name}' and '{parent_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])

    if files:
        return files[0]['id']
    else:
        print(f"📁 [Drive] สร้างโฟลเดอร์งวดเดือนย่อยใหม่ -> '{folder_name}'")
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_folder_id]
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')


# ==============================================================================
# STAGE 1 — GOOGLE CLOUD VISION OCR (อ่านตัวหนังสือจากภาพ/PDF)
# ==============================================================================
def ocr_with_vision(vision_client, file_bytes, mime_type):
    """ส่งภาพหรือ PDF เข้า Google Cloud Vision แล้วคืนค่าข้อความดิบทั้งหมด (full text)."""
    if "pdf" in mime_type:
        # PDF ใช้ batch_annotate_files แบบ inline (รองรับ PDF ขนาดเล็ก โดยอ่านหน้าแรกๆ)
        input_config = vision.InputConfig(content=file_bytes, mime_type="application/pdf")
        features = [vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)]
        file_request = vision.AnnotateFileRequest(
            input_config=input_config,
            features=features,
            pages=[1, 2, 3, 4, 5],  # Vision inline รองรับสูงสุด 5 หน้า
        )
        response = vision_client.batch_annotate_files(requests=[file_request])
        pages = response.responses[0].responses
        return "\n".join(
            p.full_text_annotation.text for p in pages if p.full_text_annotation.text
        )
    else:
        # ภาพทั่วไป ใช้ document_text_detection (เหมาะกับเอกสารหนาแน่นกว่า text_detection)
        image = vision.Image(content=file_bytes)
        response = vision_client.document_text_detection(image=image)
        if response.error.message:
            raise Exception(f"Vision API error: {response.error.message}")
        return response.full_text_annotation.text


# ==============================================================================
# STAGE 2 — GEMINI วิเคราะห์ "ข้อความ" จาก OCR ให้เป็น JSON โครงสร้าง
# ==============================================================================
GEMINI_PROMPT = """
คุณคือระบบ AI ผู้เชี่ยวชาญด้านการตรวจสอบและสกัดข้อมูลจากเอกสารทางบัญชี (ภาษาไทยเป็นหลัก)
ข้อความด้านล่างคือผลลัพธ์ที่ได้จากการ OCR เอกสารหนึ่งใบ (อาจมีตัวอักษรเพี้ยน เรียงบรรทัดสลับ หรือมี noise ปนมาบ้าง)
โปรดใช้วิจารณญาณตีความข้อความนี้ แล้วสกัดข้อมูลออกมาตามกฎต่อไปนี้

[ขั้นตอนที่ 1: คัดกรองเอกสาร]
- ตรวจสอบว่าข้อความนี้มาจากเอกสารทางบัญชีหรือไม่ (เช่น ใบเสร็จรับเงิน, ใบกำกับภาษี, บิลเงินสด, ใบส่งสินค้า)
- หากไม่ใช่เอกสารทางบัญชี หรือเป็นข้อความขยะ/อ่านไม่ออก ให้กำหนด "is_valid_bill" เป็น false และกำหนดฟิลด์อื่นๆ ทั้งหมดเป็น null ทันที
- หากเป็นเอกสารทางบัญชีจริง ให้กำหนด "is_valid_bill" เป็น true แล้วทำตามขั้นตอนที่ 2

[ขั้นตอนที่ 2: กฎการสกัดข้อมูลอย่างเข้มงวด (Strict Rules)]
1. seller_name: สกัดชื่อบริษัท/ร้านค้า และค้นหาสาขามาต่อท้ายเสมอ เช่น 'บมจ. ซีพี ออลล์ (สาขาที่ 01234)' หรือ 'บริษัท บิ๊กซี (สำนักงานใหญ่)' ยึดภาษาไทยเป็นหลัก
2. buyer_name: ชื่อบริษัทหรือบุคคลในนามผู้ซื้อ (หากไม่มีให้ใส่ null)
3. buyer_address: ที่อยู่เต็มและเลขประจำตัวผู้เสียภาษีฝั่งผู้ซื้อ (หากไม่มีให้ใส่ null)
4. Numbers: ห้ามใส่เครื่องหมายคอมมา (,) ในฟิลด์ตัวเลขเด็ดขาด ให้ใช้ทศนิยมเท่านั้น (เช่น 1500.00)
5. ข้อมูลตัวเลขต้องสัมพันธ์กันตามหลักบัญชี: total = subtotal + vat
6. ความถูกต้องของข้อมูล: ห้ามเดาหรือสร้างตัวเลขขึ้นมาเองเด็ดขาด หากข้อความ OCR ไม่ชัดเจนจนไม่สามารถยืนยันตัวเลขได้ ให้ใส่ค่าฟิลด์ตัวเลขนั้นเป็น null
7. confidence: ประเมินความน่าเชื่อถือของข้อมูลที่สกัดได้เป็นเปอร์เซ็นต์ 0-100 (100 = OCR ชัดเจน ข้อมูลครบ ตัวเลขสอดคล้องกัน, ต่ำลง = ข้อความเพี้ยน/ฟิลด์สำคัญหาย/ตัวเลขไม่ลงตัว)
8. note: หากความน่าเชื่อถือไม่เต็ม 100 ให้ระบุเหตุผลสั้นๆ หรือฟิลด์ที่ไม่แน่ใจ (ถ้าทุกอย่างชัดเจนให้ใส่ null)

--- ข้อความ OCR จากเอกสาร ---
"""


def analyze_text_with_gemini(full_text):
    """ส่งข้อความ OCR ให้ Gemini จัดเป็น JSON ตามสคีมา BillVerification (text-only เพื่อประหยัด token)"""
    global SELECTED_MODEL, client_gemini
    response = client_gemini.models.generate_content(
        model=SELECTED_MODEL,
        contents=[GEMINI_PROMPT + full_text],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BillVerification,
            temperature=0.0,
        ),
    )
    return json.loads(response.text)


def analyze_receipt(vision_client, image_bytes, mime_type):
    """Pipeline 2 ชั้น: Vision OCR -> Gemini วิเคราะห์ข้อความ"""
    full_text = ocr_with_vision(vision_client, image_bytes, mime_type)
    if not full_text or not full_text.strip():
        raise Exception("OCR ไม่พบข้อความในเอกสาร")
    return analyze_text_with_gemini(full_text)


# ==============================================================================
# GOOGLE SHEETS AUTOMATION FUNCTIONS
# ==============================================================================
def check_and_create_headers(sheet_service, spreadsheet_id, tab_name):
    range_name = f"{tab_name}!A1:A1"
    result = sheet_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=range_name
    ).execute()
    values = result.get('values', [])

    if not values or not values[0] or values[0][0] == "":
        print(f"📝 [Setup] สร้างหัวข้อคอลัมน์ใหม่ในแท็บ '{tab_name}' อัตโนมัติ...")
        body = {'values': [HEADERS]}
        sheet_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
            valueInputOption="USER_ENTERED", body=body
        ).execute()

def append_to_sheet(sheet_service, spreadsheet_id, tab_name, data_list):
    body = {'values': [data_list]}
    sheet_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", body=body
    ).execute()


# ==============================================================================
# SUB-MENU FUNCTIONS
# ==============================================================================
def process_bills_workflow(drive_service, sheet_service, vision_client):
    """ฟังก์ชันหลักข้อที่ 1: Vision OCR -> Gemini วิเคราะห์ + ระบบหมุนคีย์/โมเดลเมื่อโดน 429 (RPD)"""
    global SELECTED_MODEL, current_key_index, client_gemini

    if not GEMINI_API_KEYS:
        print("🛑 ไม่พบ GEMINI_API_KEYS ใน .env — กรุณาใส่อย่างน้อย 1 key ก่อนเริ่มประมวลผล")
        return

    print("\n--------------------------------------------------")
    print("🚀 เริ่มต้นระบบสแกนบิล (Vision OCR ➜ Gemini วิเคราะห์ข้อความ)...")
    print("--------------------------------------------------")

    files = list_files_in_folder(drive_service, FOLDER_INBOX)
    if not files:
        print("📭 ไม่พบไฟล์บิลใหม่ในโฟลเดอร์ Inbox")
        return

    total_files = len(files)
    print(f"📂 พบไฟล์รอการประมวลผลทั้งหมด: {total_files} ใบ")
    check_and_create_headers(sheet_service, SPREADSHEET_ID, SHEET_TAB_NAME)

    success_count = 0
    error_count = 0

    for f in files:
        file_id = f["id"]
        file_name = f["name"]
        mime_type = f["mimeType"]

        if "image" not in mime_type and "pdf" not in mime_type:
            print(f"⏩ ข้ามไฟล์ที่ไม่ใช่รูปภาพหรือ PDF: {file_name}")
            continue

        print(f"\n⚡ กำลังสแกน: {file_name} [คีย์ชุดที่: {current_key_index + 1} | โมเดล: {SELECTED_MODEL}]")

        try:
            move_file(drive_service, file_id, FOLDER_INBOX, FOLDER_PROCESSING)
            raw_image_bytes = download_file_to_bytes(drive_service, file_id)

            # --- ลดขนาด/บีบอัดภาพก่อนส่งเข้า Vision เพื่อให้ OCR เร็วขึ้น ---
            if "image" in mime_type:
                try:
                    img = Image.open(io.BytesIO(raw_image_bytes))
                    max_width = 1600  # เผื่อความกว้างไว้สูงเพื่อให้ตัวอักษรไทยคมชัดพอให้ OCR อ่านออก
                    if img.width > max_width:
                        w_percent = (max_width / float(img.width))
                        h_size = int((float(img.height) * float(w_percent)))
                        img = img.resize((max_width, h_size), Image.Resampling.LANCZOS)

                    compressed_io = io.BytesIO()
                    img.convert('RGB').save(compressed_io, format='JPEG', quality=85)
                    image_bytes = compressed_io.getvalue()
                    print("📉 บีบอัดภาพสำเร็จ! เตรียมส่งเข้า OCR")
                except Exception as img_err:
                    print(f"⚠️ ไม่สามารถบีบอัดภาพได้ (ใช้ไฟล์ต้นฉบับแทน): {img_err}")
                    image_bytes = raw_image_bytes
            else:
                image_bytes = raw_image_bytes  # PDF ส่งเข้า Vision ตรงๆ

            bill_data = analyze_receipt(vision_client, image_bytes, mime_type)

            if bill_data:
                if not bill_data.get("is_valid_bill"):
                    print("⚠️ AI แจ้งเตือน: ไฟล์นี้ไม่ใช่บิลซื้อขาย! กำลังย้ายไปโฟลเดอร์ Error...")
                    move_file(drive_service, file_id, FOLDER_PROCESSING, FOLDER_ERROR)
                    error_count += 1
                    continue

                print("✅ อ่านข้อมูลและคำนวณตัวเลขสำเร็จ!")
                drive_link = f"https://drive.google.com/file/d/{file_id}/view"
                recorded_month = datetime.now().strftime("%Y-%m")

                # เรียงข้อมูลให้ตรงกับ HEADERS ทั้ง 15 คอลัมน์
                row_data = [
                    bill_data.get("date"),               # วันที่บิล
                    recorded_month,                      # เดือนที่บันทึก
                    bill_data.get("bill_type"),          # ประเภทบิล
                    bill_data.get("invoice_no"),         # เลขที่บิล
                    bill_data.get("seller_name"),        # ชื่อผู้ขาย (รวมสาขา)
                    bill_data.get("seller_address"),     # ที่อยู่ผู้ขาย
                    bill_data.get("buyer_name"),         # ชื่อผู้ซื้อ
                    bill_data.get("buyer_address"),      # ที่อยู่ผู้ซื้อ
                    bill_data.get("items_summary"),      # รายการ
                    bill_data.get("subtotal"),           # มูลค่าสินค้า
                    bill_data.get("vat"),                # จำนวนภาษี (VAT)
                    bill_data.get("total"),              # จำนวนเงินรวม
                    bill_data.get("confidence"),         # ความน่าเชื่อถือ(%)
                    bill_data.get("note"),               # หมายเหตุ
                    drive_link,                          # ลิงก์รูปภาพใน Drive
                ]

                print("📝 บันทึกข้อมูลลง Google Sheet...")
                append_to_sheet(sheet_service, SPREADSHEET_ID, SHEET_TAB_NAME, row_data)

                target_folder_id = get_or_create_monthly_folder(drive_service, FOLDER_SUCCESS, recorded_month)
                move_file(drive_service, file_id, FOLDER_PROCESSING, target_folder_id)
                print(f"📦 สำเร็จ! ย้ายเข้าแฟ้ม Success/{recorded_month}/")
                success_count += 1
            else:
                raise Exception("JSON ข้อมูลว่างเปล่า")

        except Exception as e:
            print(f"❌ เกิดข้อผิดพลาดกับไฟล์ {file_name}: {e}")

            # 🛠️ จัดการ 429 (RPD/โควตา Gemini เต็ม) แบบ Step-by-Step
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                # แผน A: ถ้าตัวหลัก (flash) เต็ม -> ลองโมเดลสำรอง (lite) ของคีย์เดิม (มีโควตา RPD แยกกัน)
                if SELECTED_MODEL == "gemini-2.5-flash":
                    SELECTED_MODEL = "gemini-2.5-flash-lite"
                    print(f"\n🔄 [Flash เต็ม] สลับมาใช้โมเดลสำรอง: {SELECTED_MODEL} (ภายใต้คีย์เดิม)")
                    move_file(drive_service, file_id, FOLDER_PROCESSING, FOLDER_INBOX)
                    print(f"↩️ ย้าย '{file_name}' กลับเข้า Inbox เพื่อรอรันใหม่ด้วยโมเดล lite")
                    continue

                # แผน B: ถ้า lite ของคีย์ปัจจุบันเต็มด้วย -> ขยับไปคีย์บัญชีถัดไป
                else:
                    if current_key_index < len(GEMINI_API_KEYS) - 1:
                        current_key_index += 1
                        SELECTED_MODEL = "gemini-2.5-flash"
                        client_gemini = genai.Client(api_key=GEMINI_API_KEYS[current_key_index])

                        print(f"\n🔑 [Lite เต็มเช่นกัน] เปลี่ยนไปใช้ API Key บัญชีถัดไป (ลำดับที่ {current_key_index + 1})")
                        print(f"🔄 รีเซ็ตโมเดลกลับเป็นตัวหลัก: {SELECTED_MODEL}")
                        move_file(drive_service, file_id, FOLDER_PROCESSING, FOLDER_INBOX)
                        print(f"↩️ ย้าย '{file_name}' กลับเข้า Inbox เพื่อเตรียมรันด้วย Key ใหม่")
                        continue

                    # แผน C: คีย์ทุกบัญชีเต็มหมดแล้ว -> หยุดอย่างปลอดภัย
                    else:
                        print("\n🛑 [All Quotas Exhausted] API Key ทุกบัญชีในระบบเต็มหมดเกลี้ยงแล้วครับ!")
                        move_file(drive_service, file_id, FOLDER_PROCESSING, FOLDER_INBOX)
                        print("↩️ ย้ายไฟล์กลับเข้า Inbox เรียบร้อย (ปลอดภัย ไม่ตกหล่น)")
                        break

            # error อื่นๆ (ไม่ใช่ 429) -> เตะเข้า Error ตามปกติ
            move_file(drive_service, file_id, FOLDER_PROCESSING, FOLDER_ERROR)
            error_count += 1

        time.sleep(2)

    print("\n==================================================")
    print(f"📊 สรุปผลการสแกนรอบนี้: สำเร็จ {success_count} ใบ | ตกหล่น/ไม่ใช่บิล {error_count} ใบ")
    print("==================================================")


def check_folder_counts(drive_service):
    """ฟังก์ชันหลักข้อที่ 2: นับจำนวนไฟล์ในแต่ละโฟลเดอร์ปัจจุบัน (เพิ่มการเช็ค Success ของเดือนนี้)"""
    print("\n--------------------------------------------------")
    print("📊 รายงานจำนวนไฟล์ในระบบ Google Drive ปัจจุบัน:")
    print("--------------------------------------------------")

    inbox_files = list_files_in_folder(drive_service, FOLDER_INBOX)
    process_files = list_files_in_folder(drive_service, FOLDER_PROCESSING)
    error_files = list_files_in_folder(drive_service, FOLDER_ERROR)

    current_month_str = datetime.now().strftime("%Y-%m")
    query = f"name = '{current_month_str}' and '{FOLDER_SUCCESS}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"

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

    print(f"📥 1. โฟลเดอร์ Inbox      มีไฟล์ค้างอยู่: {len(inbox_files)} ไฟล์")
    print(f"⚡ 2. โฟลเดอร์ Processing มีไฟล์ค้างอยู่: {len(process_files)} ไฟล์")
    print(f"❌ 3. โฟลเดอร์ Error      มีไฟล์ค้างอยู่: {len(error_files)} ไฟล์")
    print(f"✅ 4. โฟลเดอร์ Success ({current_month_str}): {success_count_str}")
    print("--------------------------------------------------")

def reset_files_to_inbox(drive_service):
    """ฟังก์ชันหลักข้อที่ 3: ดึงไฟล์จาก Error และ Processing กลับเข้า Inbox ทั้งหมด"""
    print("\n--------------------------------------------------")
    print("🔄 กำลังเริ่มระบบ Reset ย้ายไฟล์กลับเข้าถัง Inbox...")
    print("--------------------------------------------------")

    process_files = list_files_in_folder(drive_service, FOLDER_PROCESSING)
    if process_files:
        print(f"🧹 พบไฟล์ค้างใน Processing {len(process_files)} ไฟล์ กำลังดึงกลับ...")
        for f in process_files:
            move_file(drive_service, f["id"], FOLDER_PROCESSING, FOLDER_INBOX)
            print(f"  ↩️ ดึงกลับแล้ว: {f['name']}")

    error_files = list_files_in_folder(drive_service, FOLDER_ERROR)
    if error_files:
        print(f"🧹 พบไฟล์ในถัง Error {len(error_files)} ไฟล์ กำลังดึงกลับ...")
        for f in error_files:
            move_file(drive_service, f["id"], FOLDER_ERROR, FOLDER_INBOX)
            print(f"  ↩️ ดึงกลับแล้ว: {f['name']}")

    if not process_files and not error_files:
        print("✨ ไม่มีไฟล์ตกค้างใน Processing และ Error")
    else:
        print("✅ ดึงไฟล์กลับเข้า Inbox เรียบร้อยพร้อมรันใหม่แล้ว")
    print("--------------------------------------------------")


def clear_screen():
    """ฟังก์ชันสำหรับล้างหน้าจอ Terminal รองรับทุกระบบปฏิบัติการ"""
    if platform.system().lower() == "windows":
        os.system("cls")
    else:
        os.system("clear")

# ==============================================================================
# MAIN SYSTEM MENU
# ==============================================================================
def main():
    creds = get_google_creds()
    drive_service = get_drive_service(creds)
    sheet_service = get_sheet_service(creds)
    vision_client = get_vision_client(creds)

    while True:
        print("\n==================================================")
        print("    🤖 ระบบบริหารจัดการบิลบัญชีอัจฉริยะ (Vision OCR + Gemini)")
        print("==================================================")
        print("  [1] 🚀 เริ่มต้นประมวลผลบิลสแกนเข้าตาราง")
        print("  [2] 📊 เช็คจำนวนไฟล์ค้าง (Inbox, Processing, Error, Success)")
        print("  [3] 🔄 ดึงไฟล์จาก Error และ Processing กลับเข้า Inbox")
        print("  [4] 🚪 จบการทำงานและปิดโปรแกรม")
        print("==================================================")

        choice = input("👉 กรุณาเลือกตัวเลขเมนูที่ต้องการทำ (1-4): ").strip()
        clear_screen()
        if choice == "1":
            process_bills_workflow(drive_service, sheet_service, vision_client)
        elif choice == "2":
            check_folder_counts(drive_service)
        elif choice == "3":
            reset_files_to_inbox(drive_service)
        elif choice == "4":
            print("\n🎉 ปิดระบบเรียบร้อย ขอบคุณที่ใช้งาน บาย! 🎉\n")
            break
        else:
            print("⚠️ เลือกเมนูไม่ถูกต้อง! กรุณากดเฉพาะตัวเลข 1, 2, 3 หรือ 4 เท่านั้น")


if __name__ == "__main__":
    main()
