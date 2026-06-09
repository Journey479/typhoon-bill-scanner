# 📘 คู่มือการติดตั้งและตั้งค่า (Setup Guide)

คู่มือนี้จะพาคุณเซ็ตอัพระบบ **Document Scanner (Vision OCR + Gemini)** ตั้งแต่ศูนย์จนพร้อมใช้งานจริง
ครอบคลุมการตั้งค่า Google Cloud, การเปิด Vision/Drive/Sheets API, Service Account,
การขอ Gemini API Key, โฟลเดอร์ Drive, Google Sheet และไฟล์ `.env`

> ⏱️ ใช้เวลาประมาณ 20–30 นาที (รอบแรกเท่านั้น)

> 🧠 **ระบบทำงาน 2 ขั้น:** (1) **Google Vision** OCR อ่านตัวหนังสือจากบิล → (2) ส่ง *ข้อความ*
> ให้ **Gemini** วิเคราะห์เป็นข้อมูลโครงสร้าง (ชื่อผู้ขาย/เลขที่บิล/ยอดเงิน ฯลฯ)
> Drive/Sheets/Vision ใช้ **Service Account JSON** ส่วน Gemini ใช้ **API Key**

---

## 📋 ภาพรวมสิ่งที่ต้องเตรียม

| รายการ | ได้มาจาก | ใช้ทำอะไร |
|---|---|---|
| Python 3.10+ | python.org | รันโปรแกรม |
| Google Cloud Project (เปิด Billing) | Google Cloud Console | ใช้บริการ Vision API |
| Cloud Vision API | Google Cloud Console | OCR อ่านตัวหนังสือจากบิล (ขั้นที่ 1) |
| Gemini API Key (1 หรือหลาย key) | Google AI Studio | วิเคราะห์ข้อความเป็น JSON (ขั้นที่ 2) |
| Service Account JSON | Google Cloud Console | สิทธิ์เข้าถึง Drive/Sheets/Vision |
| Google Drive Folders (4 อัน) | Google Drive | เก็บไฟล์บิลตามสถานะ |
| Google Sheet | Google Sheets | บันทึกข้อมูลบิล |

---

## ขั้นที่ 1️⃣ — ติดตั้ง Python และโหลดโปรเจกต์

1. ติดตั้ง **Python 3.10 ขึ้นไป** จาก <https://www.python.org/downloads/>
   - ตอนติดตั้งบน Windows อย่าลืมติ๊ก ✅ **"Add Python to PATH"**
2. เปิด Terminal / PowerShell แล้วเข้าไปที่โฟลเดอร์โปรเจกต์:
   ```powershell
   cd C:\Project\DocumentScanner_GoogleVision
   ```
3. (แนะนำ) สร้าง Virtual Environment เพื่อแยกแพ็กเกจ:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
4. ติดตั้งไลบรารีทั้งหมด:
   ```powershell
   pip install -r requirements.txt
   ```

---

## ขั้นที่ 2️⃣ — สร้าง Project บน Google Cloud + เปิด API + เปิด Billing

1. เข้า <https://console.cloud.google.com/>
2. กดมุมบนซ้าย เลือก **"Select a project" → "NEW PROJECT"** ตั้งชื่อโปรเจกต์แล้วกด **Create**
3. **เปิด Billing** ให้โปรเจกต์ (เมนู **Billing**) — ⚠️ **สำคัญ:** Cloud Vision API จะเรียกใช้ไม่ได้ถ้าไม่ผูกบัญชีเรียกเก็บเงิน
   - มีโควตาฟรีระดับหนึ่งต่อเดือน (เช่น 1,000 หน้า/เดือนแรกของ DOCUMENT_TEXT_DETECTION) หลังจากนั้นคิดเงินตามการใช้งาน
4. ไปที่เมนู **APIs & Services → Library** แล้วค้นหาและกด **Enable** ทีละตัว:
   - ✅ **Cloud Vision API**
   - ✅ **Google Drive API**
   - ✅ **Google Sheets API**

---

## ขั้นที่ 3️⃣ — สร้าง Service Account (กุญแจให้โปรแกรมเข้าถึง Google)

1. ไปที่ **APIs & Services → Credentials**
2. กด **+ CREATE CREDENTIALS → Service account**
3. ตั้งชื่อ (เช่น `bill-scanner`) → กด **Create and Continue** → ข้าม role ได้ → **Done**
4. คลิกที่ Service Account ที่เพิ่งสร้าง → แท็บ **KEYS**
5. กด **ADD KEY → Create new key → เลือก JSON → Create**
6. ไฟล์ `.json` จะถูกดาวน์โหลดมา → **ย้ายไฟล์นี้ไปไว้ในโฟลเดอร์ `config/`** ของโปรเจกต์
7. **เปิดไฟล์ JSON นั้น** แล้วคัดลอกค่าในช่อง `"client_email"`
   (หน้าตาประมาณ `bill-scanner@your-project.iam.gserviceaccount.com`)
   👉 อีเมลนี้สำคัญมาก ต้องเอาไป **แชร์สิทธิ์** ให้ Drive และ Sheet ในขั้นถัดไป

> ⚠️ ไฟล์ JSON นี้คือความลับ — ห้าม commit ขึ้น Git (มี `.gitignore` กันไว้ให้แล้ว)
> Service Account ในโปรเจกต์เดียวกันเรียกใช้ Vision API ได้เลยเมื่อเปิด API + Billing แล้ว
> (ไม่ต้องเพิ่ม IAM role พิเศษสำหรับ Vision)

---

## ขั้นที่ 4️⃣ — ขอ Gemini API Key (Google AI Studio)

1. เข้า <https://aistudio.google.com/apikey>
2. กด **Create API Key** → เลือกโปรเจกต์ → คัดลอก Key เก็บไว้
3. **(แนะนำ) ขอหลาย Key จากหลายบัญชี Google** แล้วเอามาใส่เรียงต่อกัน คั่นด้วยจุลภาค `,`
   - แต่ละบัญชีมีโควตารายวัน (RPD) ของตัวเอง → ยิ่งหลาย key ยิ่งสแกนได้มากต่อวัน
   - เมื่อ key ปัจจุบันโดน `429` (โควตาเต็ม) ระบบจะ **สลับไป key ถัดไปอัตโนมัติ**

> 💡 **เข้าใจเรื่องโควตา:** การส่ง *ข้อความ* (ไม่ใช่รูป) ให้ Gemini ช่วยประหยัด token ก็จริง
> แต่ยังนับเป็น **1 request ต่อบิล** จึงไม่ช่วยเรื่องลิมิต "จำนวนครั้งต่อวัน (RPD)" โดยตรง —
> ทางแก้ RPD คือ **ใส่หลาย key** หรือ **เปิด billing** ให้ Gemini (ลิมิตสูงขึ้นมาก)

---

## ขั้นที่ 5️⃣ — สร้างโฟลเดอร์ใน Google Drive (4 โฟลเดอร์)

1. เข้า <https://drive.google.com/> สร้างโฟลเดอร์ทั้งหมด **4 อัน** เช่น:
   - `01_Inbox` (ถังรับบิลใหม่)
   - `02_Processing` (กำลังประมวลผล)
   - `03_Success` (สแกนสำเร็จ — ระบบจะแยกย่อยเป็นรายเดือนให้เอง)
   - `04_Error` (บิลพัง/ไม่ใช่บิล)
2. **เอา ID ของแต่ละโฟลเดอร์** — เปิดโฟลเดอร์แล้วดูที่ URL บนเบราว์เซอร์:
   ```
   https://drive.google.com/drive/folders/1CoWcf-MvlSEh6vJAqplVwbjyiqcnsJvm
                                          └──────────── ID ที่ต้องคัดลอก ────────┘
   ```
3. **แชร์สิทธิ์ทุกโฟลเดอร์** ให้ `client_email` จากขั้นที่ 3:
   - คลิกขวาที่โฟลเดอร์ → **Share** → วางอีเมล Service Account → ตั้งสิทธิ์เป็น **Editor** → Send

---

## ขั้นที่ 6️⃣ — สร้าง Google Sheet

1. เข้า <https://sheets.google.com/> สร้างไฟล์ใหม่
2. **เอา Spreadsheet ID** จาก URL:
   ```
   https://docs.google.com/spreadsheets/d/1eiKlMa7YNoN3ogUtpE.../edit
                                          └──────── ID ─────────┘
   ```
3. แก้ชื่อแท็บ (sheet ด้านล่าง) ให้เป็น **`Raw_Data`** (หรือชื่ออื่นแล้วไปแก้ใน `.env`)
   - ไม่ต้องสร้างหัวคอลัมน์เอง — โปรแกรมจะสร้างหัวตารางให้อัตโนมัติรอบแรกที่รัน
4. **แชร์สิทธิ์ Sheet** ให้ `client_email` เป็น **Editor** เช่นเดียวกับ Drive

---

## ขั้นที่ 7️⃣ — กรอกไฟล์ `.env`

เปิดไฟล์ `.env` ในโฟลเดอร์โปรเจกต์ แล้วกรอกค่าทั้งหมดที่ได้จากขั้นตอนข้างบน:

```ini
# --- Gemini API keys (หลาย key คั่นด้วย , ห้ามเว้นวรรค) ---
GEMINI_API_KEYS=KEY_ตัวที่1,KEY_ตัวที่2

# --- ไฟล์ Service Account (ชื่อไฟล์ JSON ที่วางใน config/) ---
CREDENTIALS_FILE=config/ชื่อไฟล์ของคุณ.json

# --- Google Sheet ---
SPREADSHEET_ID=ใส่ Spreadsheet ID
SHEET_TAB_NAME=Raw_Data

# --- Google Drive folder IDs ---
FOLDER_INBOX=ใส่ ID โฟลเดอร์ Inbox
FOLDER_PROCESSING=ใส่ ID โฟลเดอร์ Processing
FOLDER_SUCCESS=ใส่ ID โฟลเดอร์ Success
FOLDER_ERROR=ใส่ ID โฟลเดอร์ Error
```

> ✅ Checklist ก่อนรัน: เปิด Vision/Drive/Sheets API + Billing แล้ว, มี Gemini API Key อย่างน้อย 1 ตัว,
> ไฟล์ JSON อยู่ใน `config/`, แชร์สิทธิ์ Editor ให้ Service Account ครบทั้ง 4 โฟลเดอร์ + 1 Sheet แล้ว,
> กรอก `.env` ครบทุกค่า

---

## ขั้นที่ 8️⃣ — รันโปรแกรม

```powershell
python scanner.py
```

จะเจอเมนูหลัก:

```
[1] 🚀 เริ่มต้นประมวลผลบิลสแกนเข้าตาราง
[2] 📊 เช็คจำนวนไฟล์ค้าง (Inbox, Processing, Error, Success)
[3] 🔄 ดึงไฟล์จาก Error และ Processing กลับเข้า Inbox
[4] 🚪 จบการทำงาน
```

**วิธีใช้งานจริง:**
1. อัปโหลดรูปบิล/ใบกำกับภาษี (หรือ PDF) เข้าโฟลเดอร์ **Inbox** บน Google Drive
2. รันโปรแกรมแล้วกด **`1`**
3. ระบบจะ OCR ด้วย Google Vision → ส่งข้อความให้ Gemini จัดเป็น JSON → บันทึกลง Google Sheet →
   ย้ายไฟล์ไป `Success/ปีเดือน/` หรือ `Error/` ให้อัตโนมัติ
4. เปิด Google Sheet ดูผลได้เลย 🎉

---

## 🎯 ปรับจูนความแม่นยำ

ความแม่นยำมาจาก 2 จุด ปรับได้ทั้งคู่ใน `scanner.py`:

| ต้องการปรับ | แก้ที่ |
|---|---|
| **กฎการสกัดข้อมูล/คัดกรองขยะ** (ชื่อผู้ขาย, ยอดเงิน, ประเภทบิล ฯลฯ) | แก้ข้อความใน `GEMINI_PROMPT` |
| **โครงสร้าง/คำอธิบายฟิลด์** ที่ Gemini ต้องคืนกลับ | แก้คลาส `BillVerification` (Pydantic) |
| **โมเดลที่ใช้** | ตัวแปร `SELECTED_MODEL` (ค่าเริ่มต้น `gemini-2.5-flash`) |
| **คุณภาพ OCR** | ตัวแปร `max_width` / `quality` ในขั้นบีบอัดภาพ (ภาพคมขึ้น = OCR แม่นขึ้น) |

> 💡 ถ้าอยากแม่นสูงสุดและยอมจ่ายเงิน: เปิด **billing** ของ Gemini แล้วเปลี่ยน `SELECTED_MODEL`
> เป็นรุ่น Pro ได้ หรือพิจารณา **Google Document AI** (Invoice/Expense parser) ที่ออกแบบมา
> เพื่อใบกำกับ/ใบเสร็จโดยเฉพาะ

---

## 🔧 แก้ปัญหาที่พบบ่อย (Troubleshooting)

| อาการ | สาเหตุ / วิธีแก้ |
|---|---|
| `IndexError` / แจ้งว่าไม่พบ `GEMINI_API_KEYS` ตอนเริ่ม | ยังไม่ได้ใส่ Gemini API Key ใน `.env` |
| `FileNotFoundError: config/...json` | วางไฟล์ JSON ผิดที่ หรือชื่อใน `.env` ไม่ตรง |
| `403 PERMISSION_DENIED` (Vision) | ยังไม่ได้เปิด **Cloud Vision API** หรือยังไม่เปิด **Billing** ในโปรเจกต์ |
| `403 / The caller does not have permission` (Drive/Sheet) | ยังไม่ได้แชร์ Drive/Sheet ให้ `client_email` เป็น Editor |
| `429 RESOURCE_EXHAUSTED` (Gemini) | โควตา Gemini เต็ม — ระบบจะสลับ key/model ให้เอง ถ้าหมดทุก key ให้รอโควตารีเซ็ต (รายวัน) หรือใส่ key เพิ่ม/เปิด billing |
| OCR อ่านตัวเลข/ตัวอักษรผิด | ภาพเบลอ/เอียง/แสงไม่ดี → ถ่ายใหม่ให้ชัดและตรง แล้วใช้เมนู `3` ดึงกลับ Inbox ลองใหม่ |
| ฟิลด์ในตารางว่าง/ไม่ตรง | OCR อ่านไม่ชัด หรือ prompt ยังไม่ครอบคลุม → ปรับ `GEMINI_PROMPT` (ดูหัวข้อปรับจูนด้านบน) |
| บิลถูกย้ายไป Error ทั้งที่เป็นบิลจริง | Gemini ตัดสินว่า `is_valid_bill=false` (ภาพ/ข้อความไม่ชัด) → ใช้เมนู `3` ดึงกลับแล้วลองใหม่ |
| PDF หลายหน้าไม่อ่านครบ | Vision แบบ inline อ่านได้สูงสุด 5 หน้า/ไฟล์ — แยกไฟล์ให้เล็กลง |
| `ModuleNotFoundError` | ยังไม่ได้ `pip install -r requirements.txt` หรือลืม activate venv |

---

## 🔒 ข้อควรระวังด้านความปลอดภัย

- ไฟล์ `.env` และ `config/*.json` ถูกตั้งให้ Git ไม่ track ไว้แล้ว (ผ่าน `.gitignore`)
- **ห้าม** แชร์/อัปโหลดไฟล์ทั้งสองนี้ขึ้นที่สาธารณะเด็ดขาด
- ถ้า Gemini Key หลุด ให้ revoke/สร้างใหม่ที่ Google AI Studio ทันที
- ถ้าไฟล์ Service Account หลุด ให้เข้าไป **ปิดใช้งาน/สร้างคีย์ใหม่** ที่ Google Cloud Console ทันที
