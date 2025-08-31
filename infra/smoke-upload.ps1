param([string]$FilePath = "$PSScriptRoot\test-upload.txt")
$ErrorActionPreference="Stop"

# Testdatei
Set-Content -Path $FilePath -Value ("hello gatebook " + (Get-Date -Format s)) -Encoding UTF8
$name = Split-Path -Leaf $FilePath
$ct   = "text/plain"
$size = (Get-Item $FilePath).Length

# Presign
$pres = Invoke-RestMethod -Method POST -Uri http://localhost:8080/files/presign `
  -Body (@{filename=$name;content_type=$ct;size_bytes=$size} | ConvertTo-Json) `
  -ContentType "application/json"

# Upload PUT zu MinIO
$hash = (Get-FileHash $FilePath -Algorithm SHA256).Hash.ToLower()
Invoke-WebRequest -Method Put -InFile $FilePath -ContentType $ct -Uri $pres.url | Out-Null

# Confirm
Invoke-RestMethod -Method POST -Uri http://localhost:8080/files/confirm `
  -Body (@{file_id=$pres.file_id; sha256_hex=$hash} | ConvertTo-Json) `
  -ContentType "application/json"

Write-Host "OK: uploaded $name (file_id=$($pres.file_id))" -ForegroundColor Green

# Sichtprüfung DB
docker compose -f .\docker-compose.dev.yml exec db `
  psql -U postgres -d gatebook -c "select id, filename, size_bytes, left(sha256_hex,8) as sha8, server_received_at from file_object order by server_received_at desc limit 3;"
