# smoke.ps1 â€“ Gatebook Dev Stack Smoke Test (einfach)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
chcp 65001 > $null

# In den Script-Ordner, Compose-Datei setzen
Set-Location -Path $PSScriptRoot
$compose = Join-Path $PSScriptRoot 'docker-compose.dev.yml'

Write-Host "`n--- 0) Stack up ---"
docker compose -f $compose up -d | Out-Host

Write-Host "`n--- 1) API Health ---"
$resp = Invoke-WebRequest 'http://localhost:8080/health' -UseBasicParsing
if ($resp.StatusCode -ne 200 -or $resp.Content -notmatch '"ok"') { throw "API Health unexpected: $($resp.StatusCode) $($resp.Content)" }
$resp | Select StatusCode,Content | Format-Table | Out-Host

Write-Host "`n--- 2) DB ---"
docker compose -f $compose exec -T db psql -U postgres -d gatebook -c "select current_date, current_user;" | Out-Host

Write-Host "`n--- 3) Redis ---"
$pong = docker compose -f $compose exec -T redis redis-cli ping
if ($pong.Trim() -ne 'PONG') { throw "redis ping failed: $pong" }
$pong | Out-Host

Write-Host "`n--- 4) Mailpit (SMTP) ---"
$tcp = Test-NetConnection localhost -Port 1025
if (-not $tcp.TcpTestSucceeded) { throw "SMTP 1025 nicht erreichbar" }
$tcp | Select ComputerName,RemotePort,TcpTestSucceeded | Out-Host
$smtp = [System.Net.Mail.SmtpClient]::new('127.0.0.1',1025)
$msg  = [System.Net.Mail.MailMessage]::new('smoke@gatebook.local','you@example.com','Gatebook Smoke Test','It works via Mailpit.')
$smtp.Send($msg)

Write-Host "`n--- 5) MinIO ---"
$workdir = (Get-Location).Path
$MCHOST  = 'http://admin:admin123456@minio:9000'
Set-Content -Path .\test.txt -Value "hello from smoke test $(Get-Date -Format s)" -Encoding UTF8
docker run --rm --network infra_default -e "MC_HOST_minio=$MCHOST" minio/mc mb -p minio/artifacts | Out-Host
docker run --rm --network infra_default -v "${workdir}:/work" -e "MC_HOST_minio=$MCHOST" minio/mc cp /work/test.txt minio/artifacts/ | Out-Host
docker run --rm --network infra_default -e "MC_HOST_minio=$MCHOST" minio/mc ls minio/artifacts | Out-Host

Write-Host "`n--- 6) API-Env ---"
docker compose -f $compose exec -T api sh -lc "env | egrep '^(S3_|DATABASE_URL|REDIS_URL|JWT_SECRET)'" | Out-Host

Write-Host "`nâœ… Smoke-Test komplett erfolgreich."
