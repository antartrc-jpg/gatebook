$ErrorActionPreference="Stop"
$INFRA=(Get-Location).Path
$ROOT = Split-Path -Parent $INFRA

@"
services:
  api2:
    build:
      context: ../api2
      dockerfile: Dockerfile
    command: ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
    env_file:
      - ../api2/.env
    ports:
      - "8081:8080"
    volumes: []
    depends_on: [db, minio, redis]
"@ | Set-Content -Path (Join-Path $INFRA "docker-compose.api2.yml") -Encoding UTF8

docker compose -f .\docker-compose.dev.yml -f .\docker-compose.api2.yml build api2
docker compose -f .\docker-compose.dev.yml -f .\docker-compose.api2.yml up -d api2

1..40 | % { try{ if((Invoke-RestMethod http://localhost:8081/health -TimeoutSec 2).status -eq "ok"){ "API2 bereit"; break } } catch { Start-Sleep -Milliseconds 500 } }
