# ☁️ Document Scanner — Cloud Run (event-driven) — TEMPLATE

เทมเพลตสำหรับรันระบบสแกนบิลบน **Google Cloud Run** แบบ "ฟังก์ชัน" (ไม่ต้องเขียน Dockerfile เอง)
ทุกไฟล์ในโฟลเดอร์นี้**ไม่มีความลับ/ไม่มี ID เฉพาะโปรเจกต์** — เติมค่าเองตามขั้นตอนด้านล่าง

ต่างจากเวอร์ชัน poll-loop ตรงที่ทำงาน **"1 รอบต่อ 1 request" แล้วดับ** (scale-to-zero)
และ **code.gs ยิง Cloud Run ทันทีที่มีไฟล์ใหม่เข้า Inbox** (event-driven) จึงประมวลผลแทบจะทันทีโดยไม่ต้องรันเครื่องค้าง

## 📁 ไฟล์ในเทมเพลต

| ไฟล์ | คืออะไร | ต้องเติมค่า? |
|---|---|---|
| `main.py` | worker หลัก — entry point `scan(request)` | ❌ อ่านค่าจาก env ทั้งหมด |
| `requirements.txt` | ไลบรารีสำหรับ buildpacks | ❌ |
| `env.example.yaml` | เทมเพลตค่าตัวแปรทั้งหมด → copy เป็น `env.yaml` แล้วเติมค่า | ✅ |
| `deploy.ps1` | คำสั่ง deploy — เติม project/region/SA ด้านบนสุด | ✅ (ตั้งครั้งเดียว) |
| `code.gs.template` | Apps Script (Telegram webhook) → วางใน Apps Script editor แล้วเติมค่า | ✅ |
| `README.md` | ไฟล์นี้ | — |

> 🔒 `env.yaml` และ `code.gs` (ไฟล์จริงที่เติมค่าแล้ว) ถูก **git-ignore** ไว้ — ความลับไม่หลุดขึ้น repo

## 🔄 มันทำงานยังไง

```
ส่งรูป/ไฟล์ทาง Telegram
        │
        ▼
code.gs (webhook) ──saveFile──► Google Drive (Inbox)
        │
        └──HTTP GET (?token=...)──► Cloud Run: scan(request)
                                        ├─ อ่าน _autorun_state.txt (ถ้า off → จบทันที)
                                        ├─ กู้ไฟล์ค้าง Processing → Inbox
                                        ├─ process_cycle: Typhoon OCR → LLM → Google Sheet
                                        └─ แจ้งผลทาง Telegram → ดับ (ไม่เสียเงินต่อ)
```

- ใช้ **Service Account** (ตัวที่แชร์ Drive/Sheet ไว้แล้ว) ผ่าน ADC — **ไม่ต้องอัปโหลดไฟล์ JSON**
- ความลับใส่เป็น **Environment Variables** ตอน deploy (จาก `env.yaml`)

## 💰 ตั้งให้ไม่เสียเงิน

- `--min-instances 0` → ว่างเมื่อไหร่ดับสนิท ไม่คิดเงิน (**ห้ามตั้ง ≥ 1**)
- อยู่ใน **free tier**: 2M requests + 360k vCPU-วินาที/เดือน — เหลือเฟือ
- `--max-instances 1 --concurrency 1` → **กันรอบซ้อนกันจน double-process** (สำคัญ — ดูหมายเหตุท้ายไฟล์)
- Cloud Run บังคับเปิด Billing account — แนะนำตั้ง **Budget alert** กันลืม

## 🚀 วิธีใช้ (ครั้งแรก)

### 1) เตรียมเครื่องมือ
- ติดตั้ง gcloud CLI: https://cloud.google.com/sdk/docs/install
- `gcloud auth login`

### 2) เติมค่าตัวแปรทั้งหมด — ที่ `env.yaml` ที่เดียว
```powershell
Copy-Item env.example.yaml env.yaml
```
แล้วแก้ `env.yaml` ใส่ค่าจริง:
- `TYPHOON_API_KEYS` (หลายคีย์คั่นด้วย `,` ได้), `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- `TRIGGER_TOKEN` — สุ่มยาว ๆ (กันคนนอกยิง endpoint) เช่น
  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- `SPREADSHEET_ID`, `FOLDER_INBOX/PROCESSING/SUCCESS/ERROR`

### 3) ตั้งค่าโครงสร้างพื้นฐานใน `deploy.ps1` (ตั้งครั้งเดียว)
แก้บล็อกบนสุด: `$PROJECT_ID`, `$REGION`, `$SA_EMAIL`
(อีเมล Service Account ดูได้จากไฟล์ `config/*.json` ฟิลด์ `client_email`)

### 4) Deploy
```powershell
.\deploy.ps1
```
buildpacks จะ build + deploy ให้ เสร็จแล้วได้ **Service URL** (เช่น `https://docscan-xxxx.run.app`)

> ครั้งแรกถ้าเจอ error `default service account is missing required IAM permissions`
> ให้ grant สิทธิ์ build SA ก่อน (รันครั้งเดียว):
> ```powershell
> gcloud projects add-iam-policy-binding YOUR_PROJECT_ID `
>   --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" `
>   --role="roles/cloudbuild.builds.builder"
> ```

### 5) ติดตั้ง Apps Script
- เปิด `code.gs.template` → คัดลอกทั้งหมดไปวางใน Apps Script editor ของบอท
- เติม `CLOUD_RUN_URL` (= URL จากข้อ 4), `TRIGGER_TOKEN` (= ค่าเดียวกับใน `env.yaml`), token/folder/sheet IDs
- **Deploy → Manage deployments → Edit → New version → Deploy**
- ตั้ง Telegram webhook ให้ชี้มาที่ Apps Script `.../exec`

## ⌨️ คำสั่ง Telegram

| คำสั่ง | ทำอะไร |
|---|---|
| ส่งรูป/ไฟล์ | อัปโหลดเข้า Inbox **แล้วยิง Cloud Run ประมวลผลทันที** |
| `run` / `scan` | สั่งประมวลผลไฟล์ค้างใน Inbox เดี๋ยวนี้ — **บังคับทำแม้ `autorun:off`** (ยิงด้วย `?force=1`) และตอบกลับแม้ Inbox ว่าง |
| `autorun:on` / `autorun:off` / `autorun:status` | เปิด/ปิด/ดูสถานะการประมวลผลอัตโนมัติ |
| `show` / `sh` / `link` / `mvp` / `mve` / `help` | รายงาน/ลิงก์/ย้ายไฟล์/ช่วยเหลือ |

## 🔧 อัปเดตภายหลัง

| แก้อะไร | ต้องทำ |
|---|---|
| ค่าตัวแปร (คีย์/โทเคน/โฟลเดอร์/timeout) | แก้ `env.yaml` → `.\deploy.ps1` |
| โค้ด `main.py` / `requirements.txt` | `.\deploy.ps1` (build ใหม่ ~1-2 นาที) |
| เฉพาะ env เดียว ไม่อยาก build ใหม่ | `gcloud run services update <SERVICE> --region <REGION> --update-env-vars KEY=ค่า` |
| `code.gs` | Apps Script → Deploy → New version |

## ⚠️ หมายเหตุสำคัญ

- **ทำไม `max-instances 1` + `concurrency 1`:** ต้นรอบทุกครั้ง `process_cycle` เรียก `reset_processing_to_inbox()`
  ซึ่งย้าย "ทุกไฟล์" ใน Processing กลับ Inbox ถ้ารันหลาย instance พร้อมกัน instance หนึ่งจะดึงไฟล์ที่อีก instance
  กำลังทำอยู่กลับไป → ประมวลผล/บันทึกซ้ำ ดังนั้นต้องจำกัดให้รันทีละ 1
- request ที่โดน 429 (instance ไม่ว่าง) **ไม่ทำให้ไฟล์หาย** — ไฟล์ยังอยู่ใน Inbox รอบที่กำลังรันจะกวาดทั้งหมดในรอบเดียว
- แต่ละไฟล์ที่สแกนเสร็จจะ **แจ้งกลับ Telegram พร้อมเวลาที่ใช้** (เช่น `✅ สแกนบิลสำเร็จ[2/5] ... ใช้เวลา 18 วินาที`) — auto-run จะเงียบถ้า Inbox ว่าง ส่วน `run`/`scan` จะตอบกลับเสมอ
- **ไฟล์ที่เข้า Drive โดยตรง (ไม่ผ่าน Telegram)** จะไม่ถูกยิงอัตโนมัติ — พิมพ์ `run` หรือเปิด Cloud Scheduler (ดูท้าย `deploy.ps1`) เป็น safety-net
