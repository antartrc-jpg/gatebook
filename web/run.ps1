param([int]$Port=5173)

Write-Host "Health-Check API2..." -ForegroundColor Cyan
try { Invoke-RestMethod http://localhost:8081/health | Out-Null; Write-Host "API2 OK" -ForegroundColor Green }
catch { Write-Host "API2 nicht erreichbar. Bitte docker compose up -d db minio api2 starten." -ForegroundColor Yellow }

# 1) Versuch mit Python (wenn vorhanden), Fehler NICHT abbrechen
try {
  if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "Starte python -m http.server auf Port $Port ..." -ForegroundColor Cyan
    python -m http.server $Port -d "$PSScriptRoot"
    exit
  }
} catch { }

# 2) Fallback: npx.cmd via cmd.exe (umgeht npx.ps1)
Write-Host "Starte http-server (npx.cmd via cmd) auf Port $Port ..." -ForegroundColor Cyan
cmd /c "npx.cmd --yes http-server ""$PSScriptRoot"" -p $Port -c-1"
