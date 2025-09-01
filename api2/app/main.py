import os, uuid, socket, time
import psycopg2, psycopg2.extras
import boto3
from botocore.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_URL = os.getenv("DATABASE_URL")
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
PUBLIC_S3_ENDPOINT = os.getenv("PUBLIC_S3_ENDPOINT", S3_ENDPOINT)
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET","artifacts")
ALLOW_ORIGINS = (os.getenv("ALLOW_ORIGINS") or "*").split(",")

app = FastAPI(title="Gatebook API2", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def db(): return psycopg2.connect(DB_URL)
def wait_db(max_tries=30, sleep_s=1.0):
    last=None
    for _ in range(max_tries):
        try: c=db(); c.close(); return
        except Exception as e: last=e; time.sleep(sleep_s)
    raise last

def ensure_schema():
    wait_db()
    conn=db(); cur=conn.cursor()
    try: cur.execute("create extension if not exists pgcrypto;")
    except Exception: pass
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
    );""")
    cur.execute("""
    create table if not exists audit_log(
      id bigserial primary key,
      action text not null,
      entity text not null,
      entity_id uuid,
      meta jsonb,
      at timestamptz default now()
    );""")
    conn.commit(); cur.close(); conn.close()
ensure_schema()

s3_int=boto3.client("s3", endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1")
s3_pub=boto3.client("s3", endpoint_url=PUBLIC_S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1")

class PreSignIn(BaseModel):
    filename: str
    content_type: str
    size_bytes: int = Field(ge=1, le=1024*1024*1024)
class PreSignOut(BaseModel):
    file_id: str
    s3_key: str
    url: str
class ConfirmIn(BaseModel):
    file_id: str
    sha256_hex: str

@app.get("/health")
def health(): return {"status":"ok","service":"api2"}

@app.post("/files/presign", response_model=PreSignOut)
def files_presign(inp:PreSignIn):
    file_id=str(uuid.uuid4()); s3_key=f"t-default/{file_id}/{inp.filename}"
    url=s3_pub.generate_presigned_url("put_object",
        Params={"Bucket":S3_BUCKET,"Key":s3_key,"ContentType":inp.content_type}, ExpiresIn=900)
    conn=db(); cur=conn.cursor()
    cur.execute("insert into file_object(id, filename, content_type, s3_key, within_24h) values (%s,%s,%s,%s,true)",
        (file_id, inp.filename, inp.content_type, s3_key))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
        ("files.presign","file_object",file_id, psycopg2.extras.Json({"filename":inp.filename})))
    conn.commit(); cur.close(); conn.close()
    return {"file_id":file_id,"s3_key":s3_key,"url":url}

@app.post("/files/confirm")
def files_confirm(inp:ConfirmIn):
    conn=db(); cur=conn.cursor(); cur.execute("select s3_key from file_object where id=%s",(inp.file_id,)); row=cur.fetchone(); cur.close(); conn.close()
    if not row: return {"ok":False,"error":"file_id unbekannt"}
    key=row[0]
    head=s3_int.head_object(Bucket=S3_BUCKET, Key=key)
    size_bytes=head.get("ContentLength"); content_type=head.get("ContentType","application/octet-stream")
    conn=db(); cur=conn.cursor()
    cur.execute("update file_object set size_bytes=%s, content_type=%s, sha256_hex=%s, server_received_at=now() where id=%s",
        (size_bytes,content_type,inp.sha256_hex,inp.file_id))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
        ("files.confirm","file_object",inp.file_id, psycopg2.extras.Json({"size":size_bytes})))
    conn.commit(); cur.close(); conn.close()
    return {"ok":True,"size_bytes":size_bytes,"content_type":content_type}
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

class FileRowOut(BaseModel):
    id: str
    filename: str
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    s3_key: str
    server_received_at: Optional[datetime] = None
    get_url: Optional[str] = None

def make_get_url(key: str, expires=900) -> str:
    return s3_pub.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires
    )

@app.get("/files/recent", response_model=List[FileRowOut])
def files_recent(limit: int = 20):
    conn=db(); cur=conn.cursor()
    cur.execute(
        "select id::text, filename, size_bytes, content_type, s3_key, server_received_at "
        "from file_object order by server_received_at desc nulls last limit %s",
        (limit,)
    )
    rows=[]
    for rid,fn,sz,ct,key,dt in cur.fetchall():
        url = make_get_url(key) if key else None
        rows.append({
            "id":rid,"filename":fn,"size_bytes":sz,"content_type":ct,
            "s3_key":key,"server_received_at":dt,"get_url":url
        })
    cur.close(); conn.close()
    return rows

@app.get("/files/{file_id}/download", response_model=FileRowOut)
def files_download(file_id: str):
    conn=db(); cur=conn.cursor()
    cur.execute(
        "select id::text, filename, size_bytes, content_type, s3_key, server_received_at "
        "from file_object where id=%s",
        (file_id,)
    )
    r = cur.fetchone(); cur.close(); conn.close()
    if not r:
        return {"id":file_id,"filename":"(not found)","s3_key":"","get_url":None}
    rid,fn,sz,ct,key,dt = r
    return {"id":rid,"filename":fn,"size_bytes":sz,"content_type":ct,"s3_key":key,"server_received_at":dt,"get_url":make_get_url(key)}
from typing import Dict

@app.delete("/files/{file_id}")
def files_delete(file_id: str) -> Dict[str, bool]:
    conn=db(); cur=conn.cursor()
    cur.execute("select s3_key from file_object where id=%s",(file_id,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        return {"ok": False}
    key = r[0]
    # S3: best effort entfernen
    try:
        s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
    except Exception:
        pass
    # DB-Row löschen + Audit
    cur.execute("delete from file_object where id=%s",(file_id,))
    cur.execute(
      "insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
      ("files.delete","file_object",file_id, psycopg2.extras.Json({"s3_key":key}))
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}
from typing import Dict, List, Optional
from datetime import datetime
# Retention-DTO
class RetentionIn(BaseModel):
    within_24h: bool

@app.get("/files/recent2")
def files_recent2(limit: int = 20):
    conn=db(); cur=conn.cursor()
    cur.execute("""
      select id::text, filename, size_bytes, content_type, s3_key, server_received_at, within_24h
      from file_object order by server_received_at desc nulls last limit %s
    """,(limit,))
    out=[]
    for rid,fn,sz,ct,key,dt,w24 in cur.fetchall():
        url = make_get_url(key) if key else None
        out.append({
            "id":rid,"filename":fn,"size_bytes":sz,"content_type":ct,
            "s3_key":key,"server_received_at":dt,"get_url":url,"within_24h":w24
        })
    cur.close(); conn.close()
    return out

@app.patch("/files/{file_id}/retention")
def files_set_retention(file_id: str, inp: RetentionIn):
    conn=db(); cur=conn.cursor()
    cur.execute(
      "update file_object set within_24h=%s where id=%s "
      "returning id::text, filename, size_bytes, content_type, s3_key, server_received_at, within_24h",
      (inp.within_24h, file_id)
    )
    r = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not r: return {"ok": False}
    rid,fn,sz,ct,key,dt,w24 = r
    return {
      "ok": True,
      "id": rid, "filename": fn, "size_bytes": sz, "content_type": ct,
      "s3_key": key, "server_received_at": dt, "get_url": make_get_url(key),
      "within_24h": w24
    }
# --- Upload-Validierung & neue Endpoints (presign2/confirm2) ---
import os, re, uuid, hashlib
from typing import Optional
from fastapi import HTTPException

ALLOWED_MIME = set(x.strip() for x in os.getenv("UPLOAD_ALLOWED_MIME","image/png,image/jpeg,image/webp,application/pdf,text/plain,application/zip").split(",") if x.strip())
MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES","10485760"))
VERIFY_SHA256 = os.getenv("UPLOAD_VERIFY_SHA256","false").lower() in ("1","true","yes")

def sanitize_filename(fn: str) -> str:
    fn = os.path.basename(fn or "")
    fn = re.sub(r"[^A-Za-z0-9._ -]", "_", fn)[:128]
    return fn or "file"

def validate_upload_meta(filename: str, content_type: str, size_bytes: int):
    if not filename or "/" in filename or "\\" in filename or filename.strip()=="":
        raise HTTPException(status_code=400, detail="invalid filename")
    if size_bytes <= 0 or size_bytes > MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"file too large (max {MAX_BYTES} bytes)")
    if content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"content_type not allowed: {content_type}")

class Presign2In(BaseModel):
    filename: str
    content_type: str
    size_bytes: int

class Presign2Out(BaseModel):
    file_id: str
    s3_key: str
    url: str

@app.post("/files/presign2", response_model=Presign2Out)
def presign2(inp: Presign2In):
    validate_upload_meta(inp.filename, inp.content_type, inp.size_bytes)
    file_id = str(uuid.uuid4())
    safe = sanitize_filename(inp.filename)
    key = f"t-default/{file_id}/{safe}"
    # ContentType wird mit-signiert -> muss der Browser beim PUT mitsenden
    url = s3_pub.generate_presigned_url("put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": inp.content_type},
        ExpiresIn=900)
    return {"file_id": file_id, "s3_key": key, "url": url}

class Confirm2In(BaseModel):
    file_id: str
    sha256_hex: Optional[str] = None

@app.post("/files/confirm2")
def confirm2(inp: Confirm2In):
    fid = inp.file_id
    prefix = f"t-default/{fid}/"
    # key herausfinden (wir kennen nur die ID)
    res = s3_int.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1)
    if "Contents" not in res or not res["Contents"]:
        raise HTTPException(status_code=404, detail="object not found in S3")
    key = res["Contents"][0]["Key"]

    head = s3_int.head_object(Bucket=S3_BUCKET, Key=key)
    size = int(head.get("ContentLength", 0) or 0)
    ctype = head.get("ContentType") or "application/octet-stream"

    if size <= 0 or size > MAX_BYTES:
        try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception: pass
        raise HTTPException(status_code=400, detail="uploaded object invalid size")

    if VERIFY_SHA256 and inp.sha256_hex:
        body = s3_int.get_object(Bucket=S3_BUCKET, Key=key)["Body"]
        h = hashlib.sha256()
        while True:
            chunk = body.read(1024*1024)
            if not chunk: break
            h.update(chunk)
        if h.hexdigest() != inp.sha256_hex.lower():
            try: s3_int.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception: pass
            raise HTTPException(status_code=400, detail="sha256 mismatch")

    filename = key.split("/", 2)[-1]
    conn=db(); cur=conn.cursor()
    cur.execute("""
      insert into file_object(id, filename, size_bytes, content_type, s3_key, server_received_at, within_24h)
      values (%s,%s,%s,%s,%s, now(), true)
      on conflict (id) do update
        set filename=excluded.filename,
            size_bytes=excluded.size_bytes,
            content_type=excluded.content_type,
            s3_key=excluded.s3_key,
            server_received_at=excluded.server_received_at
    """, (fid, filename, size, ctype, key))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "size_bytes": size, "content_type": ctype}
# --- /Upload-Validierung ---
