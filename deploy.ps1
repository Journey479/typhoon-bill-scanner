# =============================================================================
# deploy.ps1 — Deploy DocumentScanner ไปยัง Google Cloud Run (แบบ source / ไม่เขียน Dockerfile)
# รันใน PowerShell หลังติดตั้ง gcloud CLI และ `gcloud auth login` แล้ว
#
# ค่าทั้งหมด (คีย์/โทเคน/โฟลเดอร์/โมเดล) อยู่ในไฟล์เดียว: env.yaml
# แก้ค่าที่ env.yaml ที่เดียว แล้วรัน .\deploy.ps1 — ไม่ต้องแก้ไฟล์นี้หรือ main.py
# =============================================================================

# ---------- ค่าโครงสร้างพื้นฐาน (ตั้งครั้งเดียวต่อโปรเจกต์ แทบไม่ต้องแตะอีก) ----------
$PROJECT_ID = "YOUR_GCP_PROJECT_ID"                                  # เช่น my-project-123456
$REGION     = "asia-southeast1"                                      # สิงคโปร์ (เสถียร) / asia-southeast3 = Bangkok
$SERVICE    = "docscan"                                              # ชื่อ Cloud Run service
$SA_EMAIL   = "YOUR_SA@YOUR_GCP_PROJECT_ID.iam.gserviceaccount.com"  # Service Account ที่แชร์ Drive/Sheet ไว้แล้ว
$ENV_FILE   = "env.yaml"                                             # ← ค่าตัวแปรของแอปทั้งหมดอยู่ในนี้
# ----------------------------------------------------------------------------------

# กันพลาด: ต้องมี env.yaml ก่อน
if (-not (Test-Path $ENV_FILE)) {
  Write-Host "ไม่พบ $ENV_FILE — copy จากเทมเพลตก่อน:  Copy-Item env.example.yaml env.yaml" -ForegroundColor Red
  exit 1
}

gcloud config set project $PROJECT_ID

# เปิด API ที่จำเป็น (ครั้งแรกครั้งเดียว — รันซ้ำได้)
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Deploy: min=0 (scale-to-zero = ไม่เสียเงินตอนว่าง), max=1 + concurrency=1 (กันรอบซ้อน/ข้อมูลซ้ำ)
# env ทั้งหมดดึงจาก env.yaml ผ่าน --env-vars-file (รองรับ comma ในค่าได้ ต่างจาก --set-env-vars)
gcloud run deploy $SERVICE `
  --source . `
  --region $REGION `
  --service-account $SA_EMAIL `
  --function scan `
  --allow-unauthenticated `
  --min-instances 0 `
  --max-instances 1 `
  --concurrency 1 `
  --cpu 1 `
  --memory 512Mi `
  --timeout 600 `
  --env-vars-file $ENV_FILE

Write-Host ""
Write-Host "เสร็จแล้ว — เอา Service URL ที่ขึ้นด้านบนไปใส่ใน CloudRun/code.gs (CLOUD_RUN_URL)" -ForegroundColor Green
Write-Host "และตั้ง TRIGGER_TOKEN ใน code.gs ให้ตรงกับค่าใน env.yaml" -ForegroundColor Green

# =============================================================================
# (ออปชัน) Cloud Scheduler เป็น safety-net — ยิงทุก 2 นาที เผื่อไฟล์เข้าทางอื่นที่ไม่ผ่าน Telegram
# Cloud Scheduler ฟรี 3 jobs/บัญชี. ถ้าใช้แค่ event-driven จาก code.gs ก็ไม่ต้องรันส่วนนี้
# =============================================================================
# $RUN_URL = "https://PUT-YOUR-CLOUD-RUN-URL-HERE.a.run.app"
# $TOKEN   = "<TRIGGER_TOKEN เดียวกับใน env.yaml>"
# gcloud services enable cloudscheduler.googleapis.com
# gcloud scheduler jobs create http docscan-poll `
#   --location $REGION `
#   --schedule "*/2 * * * *" `
#   --uri "$RUN_URL`?token=$TOKEN" `
#   --http-method GET
