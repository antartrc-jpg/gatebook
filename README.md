# Gatebook – Upload/Recent Module

**Aktuell:** API2 v0.3.1 (EXIF `taken_at`, Retention-Alias, idempotentes DELETE)  
**Web:** Merge-Seite `uploader.html` (Uploader + Recents)

## Quick Start (Dev)
```powershell
# Stack starten (db + minio + api2)
docker compose -f .\infra\docker-compose.dev.yml -f .\infra\docker-compose.api2.yml up -d db minio api2

# Health
Invoke-RestMethod http://localhost:8081/health

# Web-Server lokal (Port 5173)
.\web\run.ps1
Start-Process "http://localhost:5173/uploader.html"
```

