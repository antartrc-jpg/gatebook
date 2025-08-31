# api2/app/main.py  — v0.3.0 hardened + retention alias + EXIF taken_at
import os, re, uuid, time, hashlib
from io import BytesIO
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import psycopg2, psycopg2.extras
import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Response, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from PIL import Image, ExifTags
    PIL_OK = True
except Exception:
    PIL_OK = False

# -------------------------
# Konfiguration (ENV)
# -------------------------
DB_URL = os.getenv("DATABASE_URL")
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
PUBLIC_S3_ENDPOINT = os.getenv("PUBLIC_S3_ENDPOINT", S3_ENDPOINT)
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET", "artifacts")

ALLOW_ORIGINS = [o.strip() for o in (os.getenv("ALLOW_ORIGINS") or "*").split(",") if o.strip()]

# Upload-Policy
DEFAULT_ALLOWED = (
    "image/png,image/jpeg,image/webp,"
    "application/pdf,text/plain,application/zip,"
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
ALLOWED_MIME = set(x.strip() for x in os.getenv("UPLOAD_ALLOWED_MIME", DEFAULT_ALLOWED).split(",") if x.strip())
MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))  # 20 MB
VERIFY_SHA256 = os.getenv("UPLOAD_VERIFY_SHA256", "false").lower() in ("1", "true", "yes")

# -------------------------
# App + CORS
# -------------------------
app = FastAPI(title="Gatebook API2", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# DB Helpers & Schema
# -------------------------
def db():
    return psycopg2.connect(DB_URL)

def wait_db(max_tries: int = 30, sleep_s: float = 1.0):
    last = None
    for _ in range(max_tries):
        try:
            c = db()
            c.close()
            return
        except Exception as e:
            last = e
            time.sleep(sleep_s)
    raise last

def ensure_schema():
    wait_db()
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("create extension if not exists pgcrypto;")
    except Exception:
        pass
    cur.execute("""
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
    """)
    cur.execute("""
    create table if not exists audit_log(
      id bigserial primary key,
      action text not null,
      entity text not null,
      entity_id uuid,
      meta jsonb,
      at timestamptz default now()
    );
    """)
    cur.execute("create index if not exists ix_file_object_received_at on file_object(server_received_at);")
    cur.execute("create index if not exists ix_file_object_within_24h on file_object(within_24h);")
    conn.commit(); cur.close(); conn.close()

ensure_schema()

# -------------------------
# S3 Clients
# -------------------------
s3_int = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    region_name="us-east-1",
)
s3_pub = boto3.client(
    "s3",
    endpoint_url=PUBLIC_S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    region_name="us-east-1",
)

# -------------------------
# Modelle
# -------------------------
class PreSignIn(BaseModel):
    filename: str
    content_type: str
    size_bytes: int = Field(ge=1, le=1024 * 1024 * 1024)  # hard cap 1GB

class PreSignOut(BaseModel):
    file_id: str
    s3_key: str
    url: str

class ConfirmIn(BaseModel):
    file_id: str
    sha256_hex: Optional[str] = None

class FileRowOut(BaseModel):
    id: str
    filename: str
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    s3_key: str
    server_received_at: Optional[datetime] = None
    get_url: Optional[str] = None
    within_24h: Optional[bool] = None
    retention_keep: Optional[bool] = None
    exif_taken_at: Optional[datetime] = None

class RetentionPatch(BaseModel):
    retention_keep: Optional[bool] = None
    within_24h:    Optional[bool] = None

# -------------------------
# Utils
# -------------------------
HEX_CHARS = set("0123456789abcdef")

def sanitize_filename(fn: str) -> str:
    fn = os.path.basename(fn or "")
    fn = re.sub(r"[^A-Za-z0-9._ -]", "_", fn)[:128]
    return fn or "file"

def validate_upload_meta(filename: str, content_type: str, size_bytes: int) -> Tuple[str, str, int]:
    safe = sanitize_filename(filename)
    if not safe:
        raise HTTPException(status_code=400, detail="invalid filename")
    if size_bytes <= 0:
        raise HTTPException(status_code=400, detail="size_bytes must be > 0")
    if size_bytes > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"file too large (max {MAX_BYTES} bytes)")
    if content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"unsupported content_type: {content_type}")
    return safe, content_type, size_bytes

def validate_sha256_hex(s: Optional[str]):
    if not s:
        return
    x = s.lower()
    if len(x) != 64 or any(c not in HEX_CHARS for c in x):
        raise HTTPException(status_code=400, detail="sha256_hex must be 64 hex chars")

def make_get_url(key: str, expires: int = 900) -> str:
    return s3_pub.generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=expires
    )

def parse_exif_datetime(value: str) -> Optional[datetime]:
    """
    EXIF-Format: 'YYYY:MM:DD HH:MM:SS' – ohne TZ. Wir speichern als UTC.
    """
    try:
        dt = datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def detect_exif_taken_at(key: str, content_type: str) -> Optional[datetime]:
    """
    Nur JPEG sicher. WEBP/PNG werden übersprungen (optional später).
    """
    if not PIL_OK:
        return None
    if content_type.lower() not in ("image/jpeg", "image/jpg"):
        return None
    try:
        obj = s3_int.get_object(Bucket=S3_BUCKET, Key=key)
        data = obj["Body"].read()
        with Image.open(BytesIO(data)) as im:
            exif = im.getexif()
            if not exif:
                return None
            # Primär 36867 (DateTimeOriginal), Fallback 36868/306
            tag_map = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items()}
            for k in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                if k in tag_map and isinstance(tag_map[k], str):
                    dt = parse_exif_datetime(tag_map[k])
                    if dt:
                        return dt
    except Exception:
        return None
    return None

# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "api2"}

# -------------------------
# Core Presign/Confirm
# -------------------------
def presign_core(inp: PreSignIn) -> PreSignOut:
    safe, ctype, _ = validate_upload_meta(inp.filename, inp.content_type, inp.size_bytes)
    file_id = str(uuid.uuid4())
    key = f"t-default/{file_id}/{safe}"

    url = s3_pub.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": ctype},
        ExpiresIn=900,
    )

    conn = db(); cur = conn.cursor()
    cur.execute(
        "insert into file_object(id, filename, content_type, s3_key, within_24h) values (%s,%s,%s,%s,true)",
        (file_id, safe, ctype, key),
    )
    cur.execute(
        "insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
        ("files.presign", "file_object", file_id, psycopg2.extras.Json({"filename": safe,"content_type":ctype})),
    )
    conn.commit(); cur.close(); conn.close()
    return PreSignOut(file_id=file_id, s3_key=key, url=url)

def confirm_core(inp: ConfirmIn):
    validate_sha256_hex(inp.sha256_hex)

    conn = db(); cur = conn.cursor()
    cur.execute("select s3_key from file_object where id=%s", (inp.file_id,))
    row = cur.fetchone()
    key = row[0] if row else None
    cur.close(); conn.close()

    if not key:
        prefix = f"t-default/{inp.file_id}/"
        res = s3_int.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1)
        if "Contents" in res and res["Contents"]:
            key = res["Contents"][0]["Key"]
    if not key:
        raise HTTPException(status_code=404, detail="file_id unknown / object not found")

    head = s3_int.head_object(Bucket=S3_BUCKET, Key=key)
    size_bytes = int(head.get("ContentLength", 0) or 0)
    content_type = head.get("ContentType") or "application/octet-stream"

    if size_bytes <= 0 or size_bytes > MAX_BYTES:
        try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception: pass
        raise HTTPException(status_code=400, detail="uploaded object invalid size")

    if content_type not in ALLOWED_MIME:
        try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception: pass
        raise HTTPException(status_code=415, detail=f"unsupported object content_type: {content_type}")

    # Optional SHA prüfen
    if VERIFY_SHA256 and inp.sha256_hex:
        body = s3_int.get_object(Bucket=S3_BUCKET, Key=key)["Body"]
        h = hashlib.sha256()
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk: break
            h.update(chunk)
        if h.hexdigest() != inp.sha256_hex.lower():
            try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception: pass
            raise HTTPException(status_code=400, detail="sha256 mismatch")

    # EXIF taken_at (nur JPEG)
    exif_dt = detect_exif_taken_at(key, content_type)

    filename = key.split("/", 2)[-1]
    conn = db(); cur = conn.cursor()
    cur.execute("""
        insert into file_object(id, filename, size_bytes, content_type, s3_key, server_received_at, within_24h, exif_taken_at)
        values (%s,%s,%s,%s,%s, now(), true, %s)
        on conflict (id) do update
          set filename=excluded.filename,
              size_bytes=excluded.size_bytes,
              content_type=excluded.content_type,
              s3_key=excluded.s3_key,
              server_received_at=excluded.server_received_at,
              exif_taken_at=coalesce(excluded.exif_taken_at, file_object.exif_taken_at)
    """, (inp.file_id, filename, size_bytes, content_type, key, exif_dt))
    cur.execute(
        "insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
        ("files.confirm", "file_object", inp.file_id, psycopg2.extras.Json({"size": size_bytes, "content_type": content_type, "exif_taken_at": (exif_dt.isoformat() if exif_dt else None)})),
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "size_bytes": size_bytes, "content_type": content_type}

# -------------------------
# Endpoints
# -------------------------
@app.post("/files/presign",  response_model=PreSignOut)
def files_presign(inp: PreSignIn):  return presign_core(inp)

@app.post("/files/presign2", response_model=PreSignOut)
def files_presign2(inp: PreSignIn): return presign_core(inp)

@app.post("/files/confirm")
def files_confirm(inp: ConfirmIn):  return confirm_core(inp)

@app.post("/files/confirm2")
def files_confirm2(inp: ConfirmIn): return confirm_core(inp)

SELECT_BASE = """
select
  id::text, filename, size_bytes, content_type, s3_key, server_received_at, within_24h,
  ((not within_24h) and server_received_at > now() - interval '24 hours') as retention_keep,
  exif_taken_at
from file_object
"""

@app.get("/files/recent", response_model=List[FileRowOut])
def files_recent(limit: int = 20):
    conn = db(); cur = conn.cursor()
    cur.execute(SELECT_BASE + " order by server_received_at desc nulls last limit %s", (limit,))
    rows: List[FileRowOut] = []
    for rid, fn, sz, ct, key, dt, w24, keep, exif_dt in cur.fetchall():
        url = make_get_url(key) if key else None
        rows.append(FileRowOut(
            id=rid, filename=fn, size_bytes=sz, content_type=ct, s3_key=key,
            server_received_at=dt, get_url=url, within_24h=w24, retention_keep=keep, exif_taken_at=exif_dt
        ))
    cur.close(); conn.close()
    return rows

@app.get("/files/recent2", response_model=List[FileRowOut])
def files_recent2(limit: int = 20): return files_recent(limit)

@app.get("/files/{file_id}/download", response_model=FileRowOut)
def files_download(file_id: str):
    conn = db(); cur = conn.cursor()
    cur.execute(SELECT_BASE + " where id=%s", (file_id,))
    r = cur.fetchone(); cur.close(); conn.close()
    if not r:
        return FileRowOut(id=file_id, filename="(not found)", s3_key="", get_url=None)
    rid, fn, sz, ct, key, dt, w24, keep, exif_dt = r
    return FileRowOut(
        id=rid, filename=fn, size_bytes=sz, content_type=ct, s3_key=key,
        server_received_at=dt, get_url=make_get_url(key), within_24h=w24, retention_keep=keep, exif_taken_at=exif_dt
    )

@app.delete("/files/{file_id}", status_code=204)
def files_delete(file_id: str):
    key = None
    conn = db(); cur = conn.cursor()
    cur.execute("select s3_key from file_object where id=%s", (file_id,))
    row = cur.fetchone()
    if row: key = row[0]
    if key:
        try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception: pass
        cur.execute("delete from file_object where id=%s", (file_id,))
        cur.execute(
            "insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
            ("files.delete", "file_object", file_id, psycopg2.extras.Json({"s3_key": key})),
        )
        conn.commit()
    cur.close(); conn.close()
    return Response(status_code=204)

@app.patch("/files/{file_id}/retention", status_code=204)
def files_set_retention(file_id: str, inp: RetentionPatch):
    if inp.retention_keep is not None:
        desired_within_24h = (not inp.retention_keep)
    elif inp.within_24h is not None:
        desired_within_24h = inp.within_24h
    else:
        raise HTTPException(422, "retention_keep ODER within_24h muss gesetzt sein")

    conn = db(); cur = conn.cursor()
    cur.execute("update file_object set within_24h=%s where id=%s", (desired_within_24h, file_id))
    n = cur.rowcount
    cur.execute(
        "insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
        ("files.retention", "file_object", file_id, psycopg2.extras.Json({"within_24h": desired_within_24h})),
    )
    conn.commit(); cur.close(); conn.close()
    if n == 0:
        raise HTTPException(404, "file_id nicht gefunden")
    return Response(status_code=204)
