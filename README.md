# ☁️ Document Scanner — Cloud Run (event-driven)

ระบบสแกนบิล/ใบกำกับภาษีอัตโนมัติบน **Google Cloud Run** แบบ "ฟังก์ชัน" (ไม่ต้องเขียน Dockerfile เอง)
ส่งรูปทาง Telegram → OCR → สกัดข้อมูลเป็นตาราง → บันทึกลง Google Sheet → แจ้งผลกลับ

ทำงาน **"1 รอบต่อ 1 request" แล้วดับ** (scale-to-zero ไม่เสียเงินตอนว่าง) และ **code.gs ยิง Cloud Run
ทันทีที่มีไฟล์ใหม่เข้า Inbox** (event-driven) จึงประมวลผลแทบจะทันทีโดยไม่ต้องรันเครื่องค้าง

## 🤖 เครื่องยนต์ OCR — Google ก่อน แล้วค่อย Typhoon

ใช้**โควตาฟรีของ Google ให้หมดก่อน** แล้วค่อย fallback ไป Typhoon (ไม่จำกัดจำนวน) อัตโนมัติ:

| ลำดับ | เครื่องยนต์ | โควตา | นับที่ |
|---|---|---|---|
| 1 | Google Vision OCR + **Gemini 2.5 Flash** | 20 ไฟล์/วัน | แท็บ `_QuotaState` ในชีต |
| 2 | Google Vision OCR + **Gemini 2.5 Flash-Lite** | 20 ไฟล์/วัน (รวม 40/วัน) | แท็บ `_QuotaState` ในชีต |
| 3 | **Typhoon** OCR + LLM | ไม่จำกัด | — |

- ตัวนับ Gemini **รีเซ็ตทุกวันตอน 07:00** (ปรับได้ที่ `GOOGLE_QUOTA_RESET_HOUR` / `GOOGLE_QUOTA_TZ_OFFSET`)
- **Vision จำกัด 1,000 หน้า/เดือน** (`VISION_MONTHLY_LIMIT`) กันทะลุโควตาฟรี — ครบแล้วสลับไป Typhoon ทันที (รีเซ็ตต้นเดือน)
- Vision ใช้สิทธิ์ **Service Account** ของ Cloud Run (ADC) — ไม่ต้องมี API key/ไฟล์ JSON ; **Gemini** ใช้ API key
- ไม่ตั้ง `GEMINI_API_KEY` = ข้าม Google ใช้ **Typhoon ล้วน** ได้เลย
- **Gemini 503 (โหลดหนักชั่วคราว) ไม่เด้งไป Typhoon ทันที** — retry โมเดลเดิม (`GEMINI_MAX_ATTEMPTS`, backoff `GEMINI_RETRY_BACKOFF` วินาที) แล้วลองโมเดลถัดไป ก่อนค่อย fallback (ต่างจาก 429/quota ที่มาร์กโมเดลเต็มวันทันที)
- ตัวนับโควตาเก็บใน **แท็บ `_QuotaState`** ของ Google Sheet (เซลล์ A1 = JSON) ไม่ใช่ไฟล์ Drive เพราะ **Service Account สร้างไฟล์ใหม่ใน My Drive ไม่ได้** (`storageQuotaExceeded`) แต่แก้ชีต/เพิ่มแท็บที่มีอยู่แล้วได้ปกติ — แท็บถูกสร้างอัตโนมัติรอบแรก

## 📁 ไฟล์ในโปรเจกต์

| ไฟล์ | คืออะไร | commit ขึ้น git? |
|---|---|---|
| `main.py` | worker หลัก — entry point `scan(request)` | ✅ (ไม่มีความลับ) |
| `requirements.txt` | ไลบรารีสำหรับ buildpacks | ✅ |
| `colab_fallback.ipynb` | Colab notebook สำรอง — import `main.py` โพลล์ Inbox แทนตอน Cloud Run ล่ม | ✅ |
| `deploy.ps1` | สคริปต์ deploy (อ่านค่าจาก `env.yaml`, ดันความลับเข้า Secret Manager) | ✅ |
| `env.example.yaml` | เทมเพลตค่าตัวแปร → copy เป็น `env.yaml` แล้วเติมค่า | ✅ |
| `code.gs.template` | Apps Script (Telegram webhook) → วางใน Apps Script editor | ✅ |
| `.gitignore` / `.gcloudignore` | กันความลับขึ้น git / กันไฟล์ขยะขึ้น Cloud Build | ✅ |
| `env.yaml` | **ค่าจริง** (ความลับ + ID + เป้าหมาย deploy) | ❌ git-ignored |
| `code.gs` | **ค่าจริง** (อ่านจาก Script Properties — ไม่มีความลับฝัง) | ❌ git-ignored |
| `Google Vision code/` | โค้ดอ้างอิงเวอร์ชัน local (มี SA key — local เท่านั้น) | ❌ ignored ทั้ง git + gcloud |

## 🔄 มันทำงานยังไง

```
ส่งรูป/ไฟล์ทาง Telegram
        │
        ▼
code.gs (webhook) ──[allowlist gate]──► saveFile ──► Google Drive (Inbox)
        │
        └──HTTP GET (?token=...)──► Cloud Run: scan(request)
                                        ├─ อ่าน _autorun_state.txt (ถ้า off → จบทันที)
                                        ├─ กู้ไฟล์ค้าง Processing → Inbox
                                        ├─ process_cycle (ต่อไฟล์):
                                        │     Google (Vision OCR → Gemini) จนเต็มโควตา
                                        │     → fallback Typhoon (OCR → LLM)
                                        │     → เขียน Google Sheet → ย้ายเข้า Success/YYYY-MM/
                                        └─ broadcast ผลทาง Telegram (ทุกแชทใน allowlist) → ดับ
```

- ใช้ **Service Account** (ตัวที่แชร์ Drive/Sheet/Vision ไว้แล้ว) ผ่าน ADC — ไม่ต้องอัปโหลดไฟล์ JSON
- ความลับเก็บใน **Secret Manager** แล้ว mount เป็น env ตอน deploy (ไม่ใช่ env var เปล่าที่ใครเปิด Console ก็เห็น)

## 💰 ตั้งให้ไม่บานปลาย

- `--min-instances 0` → ว่างเมื่อไหร่ดับสนิท ไม่คิดเงิน (**ห้ามตั้ง ≥ 1**)
- Cloud Run อยู่ใน free tier (2M requests + 360k vCPU-วินาที/เดือน) — เหลือเฟือ
- **Cloud Vision ฟรี 1,000 หน้า/เดือน** หลังจากนั้นคิดเงิน → โค้ดมีเพดาน `VISION_MONTHLY_LIMIT` กันไว้ชั้นหนึ่ง
  - แนะนำเสริม 2 ชั้นที่ GCP: ตั้ง **quota override** ของ Vision API + ตั้ง **Budget alert** (Billing) กันพลาด
- `--max-instances 1 --concurrency 1` → กันรอบซ้อนจน double-process (ดูหมายเหตุท้ายไฟล์)

## 🚀 วิธีใช้ (ครั้งแรก)

### 1) เตรียมเครื่องมือ + เปิด Billing
- ติดตั้ง gcloud CLI: https://cloud.google.com/sdk/docs/install → `gcloud auth login`
- **เปิด Billing** ให้โปรเจกต์ (จำเป็นสำหรับ Cloud Vision API)

### 2) เติมค่าตัวแปรทั้งหมด — ที่ `env.yaml` ที่เดียว
```powershell
Copy-Item env.example.yaml env.yaml
```
แก้ `env.yaml` ใส่ค่าจริง:
- **เป้าหมาย deploy:** `PROJECT_ID`, `REGION`, `SERVICE`, `SA_EMAIL`
- **ความลับ:** `GEMINI_API_KEY` (จาก https://aistudio.google.com/apikey), `TYPHOON_API_KEYS`,
  `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TRIGGER_TOKEN`
  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(32))"   # สุ่ม TRIGGER_TOKEN
  ```
- **Drive/Sheet:** `SPREADSHEET_ID`, `FOLDER_INBOX/PROCESSING/SUCCESS/ERROR`
- **โควตา Google** (มีค่า default ให้แล้ว): `GEMINI_DAILY_LIMIT_PER_MODEL`, `VISION_MONTHLY_LIMIT` ฯลฯ

### 3) Deploy
```powershell
.\deploy.ps1
```
สคริปต์จะ: เปิด API ที่จำเป็น → ดันความลับเข้า **Secret Manager** + ให้สิทธิ์ SA → build + deploy
เสร็จแล้วได้ **Service URL** (เช่น `https://docscan-xxxx.run.app`)

> ครั้งแรกถ้าเจอ error `default service account is missing required IAM permissions`
> ให้ grant สิทธิ์ build SA ก่อน (รันครั้งเดียว):
> ```powershell
> gcloud projects add-iam-policy-binding YOUR_PROJECT_ID `
>   --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" `
>   --role="roles/cloudbuild.builds.builder"
> ```

### 4) ติดตั้ง Apps Script (code.gs)
- เปิด `code.gs.template` → คัดลอกทั้งหมดไปวางใน Apps Script editor ของบอท
- เติมค่าจริงในฟังก์ชัน `setupProperties()` (`CLOUD_RUN_URL` = URL จากข้อ 3, `TRIGGER_TOKEN` = ค่าเดียวกับ `env.yaml`,
  folders/spreadsheet/token และ **`ADMIN_CHAT_IDS`** = chat id ของคุณ)
- ▶️ **รัน `setupProperties()` หนึ่งครั้ง** เพื่อบันทึกเข้า Script Properties (จากนั้นล้างค่าออกได้)
  > ⚠️ ต้องตั้ง `ADMIN_CHAT_IDS` ก่อน ไม่งั้นระบบ allowlist จะมองคุณเป็นคนนอกและล็อกตัวเองออก
- ▶️ **รัน `setupBillDeeTrigger()` หนึ่งครั้ง** เพื่อติดตั้ง onEdit trigger ของฟีเจอร์ "ย้ายเข้าบิลดี" (ดูหัวข้อคำสั่ง Telegram ด้านล่าง) — กดอนุญาตสิทธิ์ Drive/Sheet ตอนรันครั้งแรก
- **Deploy → Manage deployments → Edit → New version → Deploy** แล้วตั้ง Telegram webhook ให้ชี้มาที่ `.../exec`

## ⌨️ คำสั่ง Telegram

> 🔒 เฉพาะ **แอดมิน + แชทที่อยู่ใน allowlist** เท่านั้นที่ใช้บอทได้ คนนอกทักมาจะถูกปฏิเสธ (เห็นแค่ chat id ของตัวเอง)

| คำสั่ง | ทำอะไร |
|---|---|
| ส่งรูป/ไฟล์ | อัปโหลดเข้า Inbox **แล้วยิง Cloud Run ประมวลผลทันที** |
| `run` / `scan` | ประมวลผลไฟล์ค้างใน Inbox เดี๋ยวนี้ — บังคับทำแม้ `autorun:off` (`?force=1`) |
| `autorun:on` / `autorun:off` / `autorun:status` | เปิด/ปิด/ดูสถานะการประมวลผลอัตโนมัติ |
| `show` / `sh` / `link` / `mvp` / `mve` / `help` | รายงาน/ลิงก์/ย้ายไฟล์/ช่วยเหลือ |
| `allow <id>` / `revoke <id>` / `chats` | **(แอดมิน)** เพิ่ม/ถอนสิทธิ์แชท / ดูรายชื่อ allowlist |

ผลการสแกนแต่ละไฟล์ถูก **broadcast ไปทุกแชทใน allowlist** พร้อมเวลาที่ใช้ + เครื่องยนต์ที่ใช้
(เช่น `✅ สแกนบิลสำเร็จ[2/5] ... ใช้เวลา 18 วินาที [Google/gemini-2.5-flash]`)

**กรณีล้มเหลวก็แจ้งกลับด้วย** — ไฟล์ที่ถูกย้ายเข้า Error folder จะ broadcast เหตุผลให้ทราบทันที:
- `⚠️ ไม่ใช่บิล[..] ... -> ย้ายเข้า Error (เหตุผล)` — เอกสารอ่านไม่ออก/ไม่ใช่เอกสารบัญชี
- `❌ สแกนล้มเหลว[..] ... -> ย้ายเข้า Error` พร้อมข้อความ error — ข้อผิดพลาดอื่น ๆ

**บันทึกลงแท็บ `Error` ด้วย** — ทุกเอกสารที่ย้ายเข้า Error folder จะเพิ่ม 1 แถวในแท็บ `Error` ของชีต (โครง + หัวคอลัมน์สร้างอัตโนมัติรอบแรกโดย `ensure_error_tab_layout()`) ไฟล์ Error เก็บแยก **รายเดือน `Error/YYYY-MM/`** เหมือน Success
- **หัวคอลัมน์อยู่แถว 2** (แถว 1 เป็นแถวควบคุม) ; ข้อมูลเริ่มแถว 3 ; คอลัมน์: `บิลดี` (checkbox), `หมายเหตุ`, `ลิงก์รูปภาพใน Drive`, `เวลาที่บันทึก`, `โมเดลที่ประมวลผล`, `เวลาที่ใช้ประมวลผล (วินาที)`
- แถว 1: `B1` = label **`ย้ายเข้าบิลดี`**, `C1` = **Dropdown `Yes`/`No`** (ปกติ `No`)

**ฟีเจอร์ "ย้ายเข้าบิลดี" (code.gs / onEdit):** เปิดดูรูปแล้วถ้าจริงๆ เป็นบิลที่ดี → **ติ๊ก checkbox `บิลดี`** ในแถวนั้น (ติ๊กหลายแถวได้) → สลับ `C1` เป็น **`Yes`** ระบบจะย้ายไฟล์จาก `Error/` → `Success/YYYY-MM/`, ยกข้อมูลขึ้นแท็บ `Raw_Data` (map หมายเหตุ/ลิงก์/เวลา/โมเดล/เวลาประมวลผล, ตั้ง `ความน่าเชื่อถือ(%) = 00`), ลบแถวออกจาก `Error`, แล้ว **รีเซ็ต `C1` กลับ `No`** ให้เอง
> ⚠️ ต้องรัน **`setupBillDeeTrigger()` หนึ่งครั้ง** ใน Apps Script editor เพื่อติดตั้ง onEdit trigger (ผูกกับสเปรดชีต — จำเป็นเพราะ simple trigger เข้าถึง DriveApp ไม่ได้)

(หมายเหตุ: กรณี rate-limit/timeout ที่ระบบวนไฟล์กลับ Inbox เพื่อลองใหม่รอบหน้าจะ **ไม่** แจ้ง เพื่อกันสแปม)

## 🛟 Fallback ด้วย Google Colab (ตอน Cloud Run ล่ม)

ถ้า Cloud Run ดับ/ปิด ใช้ **`colab_fallback.ipynb`** รันไปป์ไลน์เดิมบน Colab แทนได้ทันที — notebook นี้ **import `main.py` ตัวเดียวกัน** จึงได้ตรรกะตรงกันเป๊ะ (โฟลเดอร์รายเดือน, แท็บ Error/บิลดี, ตัวนับโควตา, แจ้ง Telegram) ต่างกันแค่ **โพลล์ Inbox เป็นระยะ** แทน event-driven และใช้ **Service Account JSON key** (เก็บใน **Colab Secrets** หรือ **Google Drive** ครั้งเดียว — ไม่ต้องอัปโหลดทุกครั้ง) แทน ADC ของ Cloud Run

**วิธีใช้:** เปิด `colab_fallback.ipynb` ใน Colab → รันเซลล์ 1→7 ตามลำดับ (ติดตั้งไลบรารี → โหลด SA key จาก Colab Secrets/Drive → กรอกค่าตรง `env.yaml` → อัปโหลด `main.py` → ทดสอบเชื่อมต่อ → รันครั้งเดียว หรือเปิดลูปโพลล์)

> 💡 **ไม่ต้องอัป JSON ทุกครั้ง:** เซลล์ที่ 2 ลอง **Colab Secrets** ก่อน (เพิ่ม secret `SA_JSON` ครั้งเดียว วางเนื้อ JSON ทั้งก้อน — ผูกกับบัญชี ใช้ได้ทุก notebook) ถ้าไม่มีจะ fallback ไป mount **Google Drive** (เก็บไฟล์ key ไว้ครั้งเดียว ตั้ง path ที่ `DRIVE_KEY_PATH`)

- เคารพสวิตช์ **`autorun:on/off`** เดียวกับ Telegram (สั่งจากบอทได้ปกติ)
- ไฟล์ใหม่ยังเข้า Inbox ผ่าน Telegram → code.gs ได้ตามปกติ (code.gs เป็น Apps Script แยกจาก Cloud Run) — Colab จะกวาดให้เอง
- ⚠️ **อย่ารัน Colab พร้อม Cloud Run** — ทั้งคู่เรียก `reset_processing_to_inbox()` ต้นรอบ จะแย่งไฟล์กันจนประมวลผล/บันทึกซ้ำ ใช้ Colab เฉพาะตอน Cloud Run ดับเท่านั้น
- ความลับ (key/token/IDs) กรอกในเซลล์ Colab ตอนรัน — **ไม่ถูกบันทึกลงไฟล์ `.ipynb`** ที่ commit

## 🔧 อัปเดตภายหลัง

| แก้อะไร | ต้องทำ |
|---|---|
| ค่าตัวแปร / ความลับ / โควตา | แก้ `env.yaml` → `.\deploy.ps1` (อัปเดต secret + redeploy ให้เอง) |
| โค้ด `main.py` / `requirements.txt` | `.\deploy.ps1` (build ใหม่ ~1-2 นาที) |
| `code.gs` | Apps Script → Deploy → New version (ค่าตั้งใน Script Properties ไม่ต้องแตะโค้ด) |
| เพิ่ม/ถอนแชท | สั่ง `allow <id>` / `revoke <id>` ใน Telegram (ไม่ต้อง deploy) |

## ⚠️ หมายเหตุสำคัญ

- **ทำไม `max-instances 1` + `concurrency 1`:** ต้นรอบทุกครั้ง `process_cycle` เรียก `reset_processing_to_inbox()`
  ซึ่งย้าย "ทุกไฟล์" ใน Processing กลับ Inbox ถ้ารันหลาย instance พร้อมกัน instance หนึ่งจะดึงไฟล์ที่อีก instance
  กำลังทำอยู่กลับไป → ประมวลผล/บันทึกซ้ำ ดังนั้นต้องจำกัดให้รันทีละ 1
- request ที่โดน 429 (instance ไม่ว่าง) **ไม่ทำให้ไฟล์หาย** — ไฟล์ยังอยู่ใน Inbox รอบที่กำลังรันจะกวาดทั้งหมดในรอบเดียว
- **ไฟล์ที่เข้า Drive โดยตรง (ไม่ผ่าน Telegram)** จะไม่ถูกยิงอัตโนมัติ — พิมพ์ `run` หรือเปิด Cloud Scheduler (ดูท้าย `deploy.ps1`) เป็น safety-net
- **PDF:** Vision อ่าน inline ได้สูงสุด 5 หน้า/ไฟล์ ; ไฟล์ใหญ่กว่านั้นให้แยกก่อน
- ไฟล์สถานะบน Drive (เก็บในโฟลเดอร์ Success เพราะ worker ไม่สแกน): `_autorun_state.txt`,
  `_telegram_chats.txt` (allowlist) — เขียนโดย code.gs (รันในนามผู้ใช้) ส่วน worker (SA) อ่าน/แก้
- **ตัวนับโควตา** ย้ายมาเก็บใน **แท็บ `_QuotaState`** ของ Google Sheet (ไม่ใช่ไฟล์ Drive) เพราะ SA สร้างไฟล์ใหม่ใน My Drive ไม่ได้ — สร้างแท็บอัตโนมัติรอบแรก ไม่ต้องแตะ
- **คอลัมน์ในชีต Raw_Data:** นอกจากข้อมูลบิล มี 3 คอลัมน์ท้าย — `เวลาที่บันทึก` (เวลาไทย), `โมเดลที่ประมวลผล`, `เวลาที่ใช้ประมวลผล (วินาที)`
  > ⚠️ ชีตที่ใช้งานอยู่ก่อนเพิ่มฟีเจอร์นี้ ระบบ **ไม่เขียนทับหัวคอลัมน์เดิม** — เติมหัว `P1/Q1/R1` เองครั้งเดียว (แถวข้อมูลใหม่จะกรอกครบทุกคอลัมน์เอง)
