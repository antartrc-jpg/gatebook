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

app = FastAPI(title="Gatebook API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def db():
    return psycopg2.connect(DB_URL)

def wait_db(max_tries=30, sleep_s=1.0):
    last=None
    for _ in range(max_tries):
        try:
            c=db(); c.close(); return
        except Exception as e:
            last=e; time.sleep(sleep_s)
    raise last

def ensure_schema():
    wait_db()
    conn=db(); cur=conn.cursor()
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

s3_int=boto3.client(
    "s3", endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1"
)
s3_pub=boto3.client(
    "s3", endpoint_url=PUBLIC_S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1"
)

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
def health():
    return {"status":"ok"}

@app.get("/health/full")
def health_full():
    status="ok"; checks={}
    try:
        conn=db(); cur=conn.cursor(); cur.execute("select 1"); cur.fetchone(); cur.close(); conn.close()
        checks["db"]="ok"
    except Exception as e:
        checks["db"]=f"err:{e}"; status="degraded"
    try:
        s3_int.head_bucket(Bucket=S3_BUCKET); checks["s3"]="ok"
    except Exception as e:
        checks["s3"]=f"err:{e}"; status="degraded"
    try:
        s=socket.create_connection(("redis",6379),1); s.close(); checks["redis"]="ok"
    except Exception as e:
        checks["redis"]=f"err:{e}"; status="degraded"
    return {"status":status,"checks":checks}

@app.post("/files/presign", response_model=PreSignOut)
def files_presign(inp: PreSignIn):
    file_id=str(uuid.uuid4())
    s3_key=f"t-default/{file_id}/{inp.filename}"
    url=s3_pub.generate_presigned_url(
        "put_object",
        Params={"Bucket":S3_BUCKET,"Key":s3_key,"ContentType":inp.content_type},
        ExpiresIn=900
    )
    conn=db(); cur=conn.cursor()
    cur.execute("insert into file_object(id, filename, content_type, s3_key, within_24h) values (%s,%s,%s,%s,true)",
                (file_id, inp.filename, inp.content_type, s3_key))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
                ("files.presign","file_object",file_id, psycopg2.extras.Json({"filename":inp.filename})))
    conn.commit(); cur.close(); conn.close()
    return {"file_id":file_id,"s3_key":s3_key,"url":url}

@app.post("/files/confirm")
def files_confirm(inp: ConfirmIn):
    conn=db(); cur=conn.cursor()
    cur.execute("select s3_key from file_object where id=%s",(inp.file_id,))
    row=cur.fetchone(); cur.close(); conn.close()
    if not row:
        return {"ok":False,"error":"file_id unbekannt"}
    key=row[0]
    head=s3_int.head_object(Bucket=S3_BUCKET, Key=key)
    size_bytes=head.get("ContentLength")
    content_type=head.get("ContentType","application/octet-stream")
    conn=db(); cur=conn.cursor()
    cur.execute("update file_object set size_bytes=%s, content_type=%s, sha256_hex=%s, server_received_at=now() where id=%s",
                (size_bytes,content_type,inp.sha256_hex,inp.file_id))
    cur.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
                ("files.confirm","file_object",inp.file_id, psycopg2.extras.Json({"size":size_bytes})))
    conn.commit(); cur.close(); conn.close()
    return {"ok":True,"size_bytes":size_bytes,"content_type":content_type}
