# 🤖 Document Scanner — Bill & Receipt Automation (Vision OCR + Gemini)

An accounting-document pipeline that reads Thai bills, tax invoices, and receipts with a
**two-stage hybrid**: **Google Cloud Vision** does the OCR, then **Gemini** structures the
extracted text into clean JSON. Each bill is logged as a row in **Google Sheets**, and the
source files are organized across **Google Drive** folders by workflow stage and billing
month.

> ระบบบริหารจัดการบิลบัญชี — OCR ด้วย Google Vision แล้วส่งข้อความให้ Gemini วิเคราะห์ บันทึกลง Google Sheet อัตโนมัติ

---

## 🧠 How it works — the hybrid pipeline

Plain OCR can read characters but can't decide *which* number is the VAT or *which* line is
the seller. A pure LLM reading the image is accurate but burns image tokens and hits free-tier
limits fast. This project splits the work:

1. **Stage 1 — OCR (Google Cloud Vision):** `document_text_detection` (images) /
   `batch_annotate_files` (PDF) extracts the full text. Vision is strong at dense Thai text.
2. **Stage 2 — Structuring (Gemini):** the OCR **text** (not the image) is sent to Gemini
   with a Pydantic `response_schema`, which returns validated JSON — seller (with branch),
   buyer, addresses, invoice no., subtotal/VAT/total, and an `is_valid_bill` junk flag.

**Why send text instead of the image to Gemini?** It uses far fewer tokens than an image,
lowers cost, and reduces hallucination — while keeping near-LLM accuracy.

> 💡 **Note on quotas:** sending text still counts as **one Gemini request per bill**, so it
> does **not** lower the free tier's *requests-per-day* (RPD) cap. To process more bills/day,
> the app **rotates across multiple Gemini API keys** (one per Google account = separate
> daily quota). For a permanent fix, enable **billing** on a single key.

---

## ✨ Features

- **Vision OCR** — reads dense Thai/English documents; supports images and small PDFs
  (up to 5 pages inline).
- **Gemini structuring** — strict prompt + `response_schema` returns clean JSON; reconciles
  `total = subtotal + vat`; flags non-bill files (`is_valid_bill = false`) into Error.
- **Quota failover (for RPD limits)** — step-by-step on `429`:
  1. `gemini-2.5-flash` → `gemini-2.5-flash-lite` (same key, separate quota)
  2. rotate to the next API key (reset to `flash`)
  3. if all keys are exhausted, the file is safely returned to Inbox and the run stops.
- **Image compression** — images are resized (max 1600 px) / JPEG 85% before OCR.
- **Drive workflow folders** — Inbox → Processing → Success / Error, with successes filed
  into `Success/YYYY-MM/` monthly subfolders.
- **Auto headers** — the Sheet tab gets its column headers created automatically if missing.
- **Interactive CLI menu** — scan, count files per folder, or reset stuck files to Inbox.
- **One service-account JSON** authenticates Drive + Sheets + Vision; Gemini uses API keys.

---

## 📂 Project Structure

```
DocumentScanner_GoogleVision/
├── scanner.py          # 🟢 Main app: Vision OCR ➜ Gemini ➜ Drive + Sheets CLI
├── requirements.txt    # Python dependencies
├── .env                # Config: Gemini keys, credentials path, Sheet ID, Drive folder IDs
├── .gitignore
├── README.md
├── SETUP.md            # Step-by-step setup guide (Thai)
└── config/
    └── *.json          # Google Cloud service-account credentials (git-ignored)
```

---

## 🔄 Processing Workflow

```
┌─────────┐   ┌────────────┐   Vision OCR     ┌──────────┐   JSON   ┌──────────────┐
│  Inbox  │ → │ Processing │ ──► full text ─► │  Gemini  │ ──────►  │ valid bill?  │
└─────────┘   └────────────┘                  └──────────┘          └──────┬───────┘
                                                                  yes ▼        ▼ no
                                              Google Sheet row + link │        │
                                                                      ▼        ▼
                                                       Success/YYYY-MM/      Error/
```

Each scanned bill produces one row with these 15 columns:

| วันที่บิล | เดือนที่บันทึก | ประเภทบิล | เลขที่บิล | ชื่อผู้ขาย (รวมสาขา) | ที่อยู่ผู้ขาย | ชื่อผู้ซื้อ | ที่อยู่ผู้ซื้อ | รายการ | มูลค่าสินค้า | จำนวนภาษี (VAT) | จำนวนเงินรวม | ความน่าเชื่อถือ | หมายเหตุ | ลิงก์รูปภาพใน Drive |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

> `ความน่าเชื่อถือ` = คะแนนความมั่นใจ 0–100 ที่ Gemini ประเมินให้แต่ละใบ และ `หมายเหตุ` คือเหตุผลกำกับเมื่อคะแนนไม่เต็ม — ช่วยให้ไล่ตรวจใบที่ควรเช็กซ้ำได้เร็ว

---

## 🛠️ Setup (quick)

Full step-by-step guide (in Thai) is in **[`SETUP.md`](SETUP.md)**.

```bash
pip install -r requirements.txt
```

1. Create a **Google Cloud project**, enable **Cloud Vision API**, **Drive API**, and
   **Sheets API**, and make sure **billing is enabled** (Vision requires it).
2. Create a **service account**, download its **JSON key** into `config/`.
3. **Share** the 4 Drive folders and the target Spreadsheet with the service-account email
   (Editor).
4. Get one or more **Gemini API keys** from [Google AI Studio](https://aistudio.google.com/apikey)
   (use keys from different Google accounts to multiply your daily quota).
5. Fill in `.env`:

   | Variable | Description |
   |---|---|
   | `GEMINI_API_KEYS` | One or more Gemini API keys, comma-separated (rotated on `429`) |
   | `CREDENTIALS_FILE` | Path to the service-account JSON (Drive, Sheets **and** Vision) |
   | `SPREADSHEET_ID` | Target Google Sheet ID |
   | `SHEET_TAB_NAME` | Sheet tab to write into (default `Raw_Data`) |
   | `FOLDER_INBOX` / `FOLDER_PROCESSING` / `FOLDER_SUCCESS` / `FOLDER_ERROR` | Drive folder IDs |

---

## ▶️ Usage

```bash
python scanner.py
```

Menu options:

| # | Action |
|---|---|
| 1 | 🚀 Scan bills from Inbox → OCR → Gemini → write to Sheet → file into Success/Error |
| 2 | 📊 Count files in each folder (Inbox, Processing, Error, Success/this-month) |
| 3 | 🔄 Move all files from Error & Processing back to Inbox (re-run) |
| 4 | 🚪 Exit |

**Typical run:** upload bill images/PDFs into the **Inbox** Drive folder → run `scanner.py`
→ press **1** → check the Google Sheet.

---

## ⚠️ Security Note

`.env` and `config/*.json` are git-ignored. **Never** commit or share them. If a Gemini key
or service-account key is leaked, **revoke/recreate** it in Google AI Studio / the Google
Cloud Console immediately.

---

## 📝 Tech Stack

Python · Google Cloud Vision (`google-cloud-vision`) · Gemini (`google-genai`) · Google
Drive API · Google Sheets API · Pillow · Pydantic
