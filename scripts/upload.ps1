# scripts/upload.ps1 — WinPS 5.x & pwsh kompatibel
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true, Position=0)][string]$Path,
  [switch]$Keep,
  [switch]$Sha256,
  [string]$ApiBase = "http://localhost:8081",
  [switch]$Open
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Path)) { throw "Datei nicht gefunden: $Path" }

# --- MIME-Mapping ---
$ext = [IO.Path]::GetExtension($Path)
if ($null -eq $ext) { $ext = "" }
$ext = $ext.ToLowerInvariant()

$mimeMap = @{
  ".txt"="text/plain"; ".pdf"="application/pdf"; ".png"="image/png"; ".jpg"="image/jpeg"; ".jpeg"="image/jpeg";
  ".webp"="image/webp"; ".zip"="application/zip";
  ".docx"="application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  ".xlsx"="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}
$ct = $mimeMap[$ext]; if (-not $ct) { $ct = "application/octet-stream" }

# --- Größe ---
$fi  = Get-Item -LiteralPath $Path
$len = [int64]$fi.Length

function Get-Sha256Hex([string]$p){
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $fs = [System.IO.File]::OpenRead($p)
    try { ($sha.ComputeHash($fs) | ForEach-Object { $_.ToString('x2') }) -join '' }
    finally { $fs.Dispose() }
  } finally { $sha.Dispose() }
}

# --- presign2 ---
Write-Host "→ presign2 ($ct, ${len}B)" -ForegroundColor Cyan
$body = @{ filename=[IO.Path]::GetFileName($Path); content_type=$ct; size_bytes=$len } | ConvertTo-Json
$pres = Invoke-RestMethod -Method POST "$ApiBase/files/presign2" -ContentType 'application/json' -Body $body

# --- PUT ---
Write-Host "→ PUT" -ForegroundColor Cyan
Invoke-WebRequest -Method Put -InFile $Path -ContentType $ct -Uri $pres.url | Out-Null

# --- confirm2 (+ optional SHA) ---
$confirm = @{ file_id=$pres.file_id }
if ($Sha256) {
  Write-Host "→ Hashing…" -ForegroundColor Cyan
  $confirm.sha256_hex = Get-Sha256Hex $Path
}
Write-Host "→ confirm2" -ForegroundColor Cyan
Invoke-RestMethod -Method POST "$ApiBase/files/confirm2" -ContentType 'application/json' -Body ($confirm | ConvertTo-Json) | Out-Null

# --- optional Keep ---
if ($Keep) {
  Write-Host "→ retention: KEEP" -ForegroundColor Cyan
  try {
    Invoke-RestMethod -Method PATCH "$ApiBase/files/$($pres.file_id)/retention" -ContentType 'application/json' -Body (@{ retention_keep=$true } | ConvertTo-Json) | Out-Null
  } catch {
    Invoke-RestMethod -Method PATCH "$ApiBase/files/$($pres.file_id)/retention" -ContentType 'application/json' -Body (@{ within_24h=$false } | ConvertTo-Json) | Out-Null
  }
}

# --- Ergebnis + Clipboard ---
$info = Invoke-RestMethod "$ApiBase/files/$($pres.file_id)/download"
try { Set-Clipboard -Value $info.get_url } catch { $info.get_url | clip }
Write-Host ("OK: {0}  → URL in Zwischenablage (≈15 min gültig)" -f $info.filename) -ForegroundColor Green
if ($Open) { Start-Process $info.get_url | Out-Null }
$info
