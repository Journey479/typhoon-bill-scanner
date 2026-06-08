# =============================================================================
# deploy.ps1 — Deploy DocumentScanner ไปยัง Google Cloud Run (แบบ source / ไม่เขียน Dockerfile)
# รันใน PowerShell หลังติดตั้ง gcloud CLI และ `gcloud auth login` แล้ว
#
# ค่าทั้งหมด (คีย์/โทเคน/โฟลเดอร์/โมเดล + เป้าหมาย deploy) อยู่ในไฟล์เดียว: env.yaml
# - ค่า "ความลับ" (คีย์/โทเคน) -> ดันเข้า Secret Manager แล้ว mount ผ่าน --set-secrets
#   (ไม่เป็น env var เปล่า ๆ ที่ใครเปิด Console ก็เห็น)
# - ค่าที่ไม่ลับ (ID โฟลเดอร์/โมเดล/โควตา) -> ส่งเป็น env var ตามปกติ
# สคริปต์นี้ไม่มีค่าความลับ/ค่าโปรเจกต์ฝังอยู่ จึง commit ขึ้น git ได้ปลอดภัย
# =============================================================================

$ENV_FILE = "env.yaml"   # ← ค่าตัวแปรทั้งหมดอยู่ในนี้ (git-ignored)

if (-not (Test-Path $ENV_FILE)) {
  Write-Host "ไม่พบ $ENV_FILE — copy จากเทมเพลตก่อน:  Copy-Item env.example.yaml env.yaml" -ForegroundColor Red
  exit 1
}

# ---------- อ่านค่า key เดียวจาก env.yaml (รองรับ `KEY: "value"  # comment` และ `KEY: value`) ----------
function Get-EnvVar([string]$Key) {
  foreach ($line in Get-Content $ENV_FILE) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $m = [regex]::Match($t, '^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$')
    if (-not $m.Success -or $m.Groups[1].Value -ne $Key) { continue }
    $v = $m.Groups[2].Value.Trim()
    if ($v.StartsWith('"')) {
      $end = $v.IndexOf('"', 1)
      if ($end -ge 1) { return $v.Substring(1, $end - 1) }
    }
    $hash = $v.IndexOf('#')
    if ($hash -ge 0) { $v = $v.Substring(0, $hash).Trim() }
    return $v
  }
  return ""
}

# ---------- เป้าหมาย deploy (อ่านจาก env.yaml — ไม่ฝังในสคริปต์) ----------
$PROJECT_ID = Get-EnvVar "PROJECT_ID"
$REGION     = Get-EnvVar "REGION"
$SERVICE    = Get-EnvVar "SERVICE"
$SA_EMAIL   = Get-EnvVar "SA_EMAIL"

foreach ($pair in @{ PROJECT_ID = $PROJECT_ID; REGION = $REGION; SERVICE = $SERVICE; SA_EMAIL = $SA_EMAIL }.GetEnumerator()) {
  if ([string]::IsNullOrWhiteSpace($pair.Value)) {
    Write-Host "ขาดค่า $($pair.Key) ใน $ENV_FILE — เติมให้ครบก่อน deploy" -ForegroundColor Red
    exit 1
  }
}

gcloud config set project $PROJECT_ID

# เปิด API ที่จำเป็น (ครั้งแรกครั้งเดียว — รันซ้ำได้ไม่เป็นไร)
# vision = OCR ฝั่ง Google (ต้องเปิด Billing ด้วย) ; secretmanager = เก็บความลับ
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com vision.googleapis.com secretmanager.googleapis.com

# =============================================================================
# 1) ดันความลับเข้า Secret Manager + ให้สิทธิ์ SA อ่าน + เตรียม --set-secrets
#    (คีย์ที่ค่าว่างใน env.yaml จะถูกข้าม)
# =============================================================================
$SECRET_KEYS = @{
  GEMINI_API_KEY   = "docscan-gemini-api-key"
  TYPHOON_API_KEYS = "docscan-typhoon-api-keys"
  TELEGRAM_TOKEN   = "docscan-telegram-token"
  TRIGGER_TOKEN    = "docscan-trigger-token"
}
$setSecrets = @()
foreach ($key in $SECRET_KEYS.Keys) {
  $val = Get-EnvVar $key
  if ([string]::IsNullOrWhiteSpace($val)) { continue }
  $name = $SECRET_KEYS[$key]

  gcloud secrets describe $name *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  + สร้าง secret ใหม่: $name" -ForegroundColor Yellow
    gcloud secrets create $name --replication-policy="automatic" | Out-Null
  }

  # เขียนค่าเป็นไฟล์ชั่วคราว (UTF-8 ไม่มี BOM, ไม่มี newline ต่อท้าย) แล้ว add version
  $tmpSecret = New-TemporaryFile
  [System.IO.File]::WriteAllText($tmpSecret.FullName, $val, (New-Object System.Text.UTF8Encoding($false)))
  gcloud secrets versions add $name --data-file="$($tmpSecret.FullName)" | Out-Null
  Remove-Item $tmpSecret.FullName -Force

  # ให้ SA ของ Cloud Run อ่าน secret นี้ได้
  gcloud secrets add-iam-policy-binding $name `
    --member="serviceAccount:$SA_EMAIL" `
    --role="roles/secretmanager.secretAccessor" --condition=None | Out-Null

  $setSecrets += "$key=$($name):latest"
}

# =============================================================================
# 2) สร้างไฟล์ env "เฉพาะค่าที่ไม่ลับ" (ตัดความลับ + ค่าเป้าหมาย deploy ออก) ส่งเป็น env var
# =============================================================================
$EXCLUDE = @("GEMINI_API_KEY","TYPHOON_API_KEYS","TELEGRAM_TOKEN","TRIGGER_TOKEN",
             "PROJECT_ID","REGION","SERVICE","SA_EMAIL")
$tmpEnv = Join-Path $env:TEMP ("docscan-env-{0}.yaml" -f ([guid]::NewGuid().ToString('N')))
$envLines = foreach ($line in Get-Content $ENV_FILE) {
  $m = [regex]::Match($line, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:')
  if ($m.Success -and ($EXCLUDE -contains $m.Groups[1].Value)) { continue }
  $line
}
[System.IO.File]::WriteAllLines($tmpEnv, $envLines, (New-Object System.Text.UTF8Encoding($false)))

# =============================================================================
# 3) Deploy: min=0 (scale-to-zero), max=1 + concurrency=1 (กันรอบซ้อน/ข้อมูลซ้ำ)
# =============================================================================
$deployArgs = @(
  "run","deploy",$SERVICE,
  "--source",".",
  "--region",$REGION,
  "--service-account",$SA_EMAIL,
  "--function","scan",
  "--allow-unauthenticated",
  "--min-instances","0","--max-instances","1","--concurrency","1",
  "--cpu","1","--memory","512Mi","--timeout","600",
  "--env-vars-file",$tmpEnv
)
if ($setSecrets.Count -gt 0) {
  $deployArgs += @("--set-secrets", ($setSecrets -join ","))
  Write-Host "🔐 ความลับที่ mount ผ่าน Secret Manager: $($setSecrets -join ', ')" -ForegroundColor Cyan
}

try {
  gcloud @deployArgs
} finally {
  if (Test-Path $tmpEnv) { Remove-Item $tmpEnv -Force }   # ลบไฟล์ env ชั่วคราวเสมอ
}

Write-Host ""
Write-Host "เสร็จแล้ว — เอา Service URL ที่ขึ้นด้านบนไปตั้งใน Apps Script (Script Property: CLOUD_RUN_URL)" -ForegroundColor Green
Write-Host "และตั้ง Script Property: TRIGGER_TOKEN ให้ตรงกับค่าใน env.yaml" -ForegroundColor Green

# =============================================================================
# (ออปชัน) Cloud Scheduler เป็น safety-net — ยิงทุก 2 นาที เผื่อไฟล์เข้าทางอื่นที่ไม่ผ่าน Telegram
# =============================================================================
# $RUN_URL = "https://PUT-YOUR-CLOUD-RUN-URL-HERE.a.run.app"
# $TOKEN   = "<TRIGGER_TOKEN เดียวกับใน env.yaml>"
# gcloud services enable cloudscheduler.googleapis.com
# gcloud scheduler jobs create http docscan-poll `
#   --location $REGION `
#   --schedule "*/2 * * * *" `
#   --uri "$RUN_URL`?token=$TOKEN" `
#   --http-method GET
