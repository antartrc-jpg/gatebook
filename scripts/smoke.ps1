param([string]$ApiBase="http://localhost:8081",[switch]$Keep)
$ErrorActionPreference="Stop"; $ProgressPreference="SilentlyContinue"

Write-Host "→ Health" -ForegroundColor Cyan
Invoke-RestMethod "$ApiBase/health" | Out-Null

# Testdatei
$tmp = Join-Path $PSScriptRoot ("smoke_"+(Get-Date -Format yyyyMMdd_HHmmss)+".txt")
"smoke $(Get-Date -Format s)" | Set-Content $tmp -Encoding UTF8
$ct="text/plain"; $len=(Get-Item $tmp).Length

Write-Host "→ presign2" -ForegroundColor Cyan
$pres = Invoke-RestMethod -Method POST "$ApiBase/files/presign2" -ContentType 'application/json' `
  -Body (@{ filename=(Split-Path -Leaf $tmp); content_type=$ct; size_bytes=$len } | ConvertTo-Json)

Write-Host "→ PUT" -ForegroundColor Cyan
Invoke-WebRequest -Method Put -InFile $tmp -ContentType $ct -Uri $pres.url | Out-Null

Write-Host "→ confirm2" -ForegroundColor Cyan
Invoke-RestMethod -Method POST "$ApiBase/files/confirm2" -ContentType 'application/json' `
  -Body (@{ file_id=$pres.file_id } | ConvertTo-Json) | Out-Null

if ($Keep) {
  Write-Host "→ retention (Keep, Alias-fähig)" -ForegroundColor Cyan
  try {
    Invoke-RestMethod -Method PATCH "$ApiBase/files/$($pres.file_id)/retention" -ContentType 'application/json' `
      -Body (@{ retention_keep=$true } | ConvertTo-Json) | Out-Null
  } catch {
    Invoke-RestMethod -Method PATCH "$ApiBase/files/$($pres.file_id)/retention" -ContentType 'application/json' `
      -Body (@{ within_24h=$false } | ConvertTo-Json) | Out-Null
  }
}

Write-Host "→ recent2 prüfen" -ForegroundColor Cyan
$recent = Invoke-RestMethod "$ApiBase/files/recent2"
if (-not ($recent | Where-Object { $_.id -eq $pres.file_id })) { throw "ID nicht in recent2" }

Write-Host ("SMOKE OK: {0}" -f $pres.file_id) -ForegroundColor Green
