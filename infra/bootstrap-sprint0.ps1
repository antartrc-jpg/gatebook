$ErrorActionPreference = "Stop"
Write-Host "==> Bootstrap Sprint 0 (Upload-Pipeline) startet ..." -ForegroundColor Cyan

# Pfade
$INFRA = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT  = Split-Path -Parent $INFRA
$API   = Join-Path $ROOT "api"
$APP   = Join-Path $API "app"
$WEB   = Join-Path $ROOT "web"

# Verzeichnisse
New-Item -ItemType Directory -Force -Path $API,$APP,$WEB | Out-Null

# .env für API (nur Dev) – inkl. PUBLIC_S3_ENDPOINT für Browser
$ApiEnv = @"
DATABASE_URL=postgres://postgres:postgres@db:5432/gatebook
REDIS_URL=redis://redis:6379/0
S3_ENDPOINT=http://minio:9000
PUBLIC_S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=admin
S3_SECRET_KEY=admin123456
S3_BUCKET=artifacts
JWT_SECRET=dev-secret
ALLOW_ORIGINS=http://localhost:5173
"@
Set-Content -Path (Join-Path $API ".env") -Encoding UTF8 -Value $ApiEnv

# requirements.txt
$Req = @"
fastapi==0.115.0
uvicorn[standard]==0.30.6
boto3==1.34.162
psycopg2-binary==2.9.9
python-multipart==0.0.9
"@
Set-Content -Path (Join-Path $API "requirements.txt") -Encoding UTF8 -Value $Req

# Dockerfile
$Dockerfile = @"
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8080
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
"@
Set-Content -Path (Join-Path $API "Dockerfile") -Encoding UTF8 -Value $Dockerfile

# app/main.py (mit PUBLIC_S3_ENDPOINT für Presign-URL)
$MainPy = @"
import os, uuid
from typing import Optional
import psycopg2, psycopg2.extras
import boto3
from botocore.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_URL = os.getenv("DATABASE_URL")
S3_ENDPOINT = os.getenv("S3_ENDPOINT")  # intern (Container→MinIO)
PUBLIC_S3_ENDPOINT = os.getenv("PUBLIC_S3_ENDPOINT", S3_ENDPOINT)  # extern (Browser→localhost)
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET","artifacts")
ALLOW_ORIGINS = (os.getenv("ALLOW_ORIGINS") or "*").split(",")

app = FastAPI(title="Gatebook API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def db():
    return psycopg2.connect(DB_URL)

def ensure_schema():
    sql = '''
    create extension if not exists pgcrypto;
    create table if not exists file_object(
      id uuid primary key default gen_random_uuid(),
      filename text not null,
      size_bytes bigint,
      content_type text,
      s3_key text not null,
      sha256_hex text,
      server_received_at timestamptz not null default now(),
      exif_taken_at timestamptz,
      within_24h boolean not null default true
    );
    create table if not exists audit_log(
      id bigserial primary key,
      action text not null,
      entity text not null,
      entity_id uuid,
      meta jsonb,
      at timestamptz default now()
    );
    '''
    conn = db(); cur = conn.cursor()
    cur.execute(sql); conn.commit()
    cur.close(); conn.close()

ensure_schema()

# S3-Clients: intern (API↔MinIO) und öffentlich (Browser↔localhost) für Presign
s3_int = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1"
)
s3_pub = boto3.client(
    "s3",
    endpoint_url=PUBLIC_S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1"
)

class PreSignIn(BaseModel):
    filename: str
    content_type: str
    size_bytes: int = Field(ge=1, le=1024*1024*1024)  # ≤1GB

class PreSignOut(BaseModel):
    file_id: str
    s3_key: str
    url: str

class ConfirmIn(BaseModel):
    file_id: str
    sha256_hex: str

@app.get("/health")
def health():
    # optional: leichte DB/S3 Checks
    return {"status":"ok"}

@app.post("/files/presign", response_model=PreSignOut)
def files_presign(inp: PreSignIn):
    file_id = str(uuid.uuid4())
    s3_key = f"t-default/{file_id}/{inp.filename}"
    url = s3_pub.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key, "ContentType": inp.content_type},
        ExpiresIn=900
    )
    conn = db(); cur = conn.cursor()
    cur.execute("insert into file_object(id, filename, content_type, s3_key, within_24h) values (%s,%s,%s,%s,true)",
                (file_id, inp.filename, inp.content_type, s3_key))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
                ("files.presign","file_object",file_id, psycopg2.extras.Json({"filename":inp.filename})))
    conn.commit(); cur.close(); conn.close()
    return {"file_id": file_id, "s3_key": s3_key, "url": url}

@app.post("/files/confirm")
def files_confirm(inp: ConfirmIn):
    key = _get_key(inp.file_id)
    head = s3_int.head_object(Bucket=S3_BUCKET, Key=key)
    size_bytes = head.get("ContentLength")
    content_type = head.get("ContentType","application/octet-stream")
    conn = db(); cur = conn.cursor()
    cur.execute("""
      update file_object set size_bytes=%s, content_type=%s, sha256_hex=%s, server_received_at=now()
      where id=%s
    """, (size_bytes, content_type, inp.sha256_hex, inp.file_id))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
                ("files.confirm","file_object",inp.file_id, psycopg2.extras.Json({"size":size_bytes})))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "size_bytes": size_bytes, "content_type": content_type}

def _get_key(file_id:str)->str:
    conn=db(); cur=conn.cursor()
    cur.execute("select s3_key from file_object where id=%s",(file_id,))
    row=cur.fetchone(); cur.close(); conn.close()
    if not row: raise ValueError("file_id unbekannt")
    return row[0]
"@
Set-Content -Path (Join-Path $APP "main.py") -Encoding UTF8 -Value $MainPy

# web\index.html – Minimaler Uploader
$IndexHtml = @"
<!doctype html><html lang="de"><meta charset="utf-8"><title>Gatebook Uploader</title>
<h3>Gatebook – Mini Uploader</h3>
<input type="file" id="f"/><button id="btn">Upload</button>
<pre id="out"></pre>
<script>
const api = "http://localhost:8080";
const out = (m)=>document.getElementById('out').textContent += m+"\\n";
document.getElementById('btn').onclick = async ()=>{
  const file = document.getElementById('f').files[0]; if(!file) return;
  out("Datei: "+file.name+" ("+file.type+", "+file.size+" bytes)");
  const r1 = await fetch(api+"/files/presign",{method:"POST",headers: {"Content-Type":"application/json"},body: JSON.stringify({filename:file.name, content_type:file.type||"application/octet-stream", size_bytes:file.size})});
  const p = await r1.json();
  await fetch(p.url,{ method:"PUT", headers: {"Content-Type":file.type||"application/octet-stream"}, body:file});
  const buf = await file.arrayBuffer();
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", buf))).map(b=>b.toString(16).padStart(2,"0")).join("");
  const r2 = await fetch(api+"/files/confirm",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file_id:p.file_id, sha256_hex:hash})});
  out("Fertig: "+await r2.text());
};
</script>
"@
Set-Content -Path (Join-Path $WEB "index.html") -Encoding UTF8 -Value $IndexHtml

# Docker Compose bauen & starten
Set-Location $INFRA
docker compose -f .\docker-compose.dev.yml build api
docker compose -f .\docker-compose.dev.yml up -d api web

# Health prüfen
Start-Sleep -Seconds 3
try {
  $health = Invoke-RestMethod http://localhost:8080/health
  Write-Host "API Health:" ($health | ConvertTo-Json -Compress)
} catch { Write-Warning "Health-Check fehlgeschlagen: $($_.Exception.Message)" }

# Smoke-Upload-Script erzeugen
$SmokeUpload = @"
param([string]\$FilePath = "\$PSScriptRoot\test-upload.txt")
Set-Content -Path \$FilePath -Value ("hello gatebook " + (Get-Date -Format s)) -Encoding UTF8
\$name = Split-Path -Leaf \$FilePath
\$ct = "text/plain"
\$size = (Get-Item \$FilePath).Length
\$pres = Invoke-RestMethod -Method POST -Uri http://localhost:8080/files/presign -Body (@{filename=\$name;content_type=\$ct;size_bytes=\$size} | ConvertTo-Json) -ContentType "application/json"
\$hash = (Get-FileHash \$FilePath -Algorithm SHA256).Hash.ToLower()
Invoke-WebRequest -Method Put -InFile \$FilePath -ContentType \$ct -Uri \$pres.url | Out-Null
Invoke-RestMethod -Method POST -Uri http://localhost:8080/files/confirm -Body (@{file_id=\$pres.file_id; sha256_hex=\$hash} | ConvertTo-Json) -ContentType "application/json"
Write-Host "OK: uploaded \$name (file_id=\$($pres.file_id))" -ForegroundColor Green

docker compose -f .\docker-compose.dev.yml exec db psql -U postgres -d gatebook -c "select id, filename, size_bytes, left(sha256_hex,8) as sha8, server_received_at from file_object order by server_received_at desc limit 3;"
"@
Set-Content -Path (Join-Path $INFRA "smoke-upload.ps1") -Encoding UTF8 -Value $SmokeUpload

# Smoke jetzt ausführen
& "$INFRA\smoke-upload.ps1"

Write-Host "==> Bootstrap Sprint 0 abgeschlossen." -ForegroundColor Green
