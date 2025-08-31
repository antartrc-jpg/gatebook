$ErrorActionPreference="Stop"
$INFRA=(Get-Location).Path
$F="$INFRA\test-upload2.txt"
Set-Content -Path $F -Value ("hello gatebook " + (Get-Date -Format s)) -Encoding UTF8
$ct="text/plain"; $size=(Get-Item $F).Length
$pres = Invoke-RestMethod -Method POST -Uri http://localhost:8081/files/presign -Body (@{filename=(Split-Path -Leaf $F);content_type=$ct;size_bytes=$size} | ConvertTo-Json) -ContentType "application/json"
$hash = (Get-FileHash $F -Algorithm SHA256).Hash.ToLower()
Invoke-WebRequest -Method Put -InFile $F -ContentType $ct -Uri $pres.url | Out-Null
Invoke-RestMethod -Method POST -Uri http://localhost:8081/files/confirm -Body (@{file_id=$pres.file_id; sha256_hex=$hash} | ConvertTo-Json) -ContentType "application/json"
docker compose -f .\docker-compose.dev.yml exec db `
  psql -U postgres -d gatebook -c "select id, filename, size_bytes, left(sha256_hex,8) as sha8, server_received_at from file_object order by server_received_at desc limit 5;"
