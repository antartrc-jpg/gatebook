import os, time, argparse, psycopg2, psycopg2.extras, boto3
from botocore.config import Config

DB_URL = os.getenv("DATABASE_URL")
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET","artifacts")

s3 = boto3.client("s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(s3={"addressing_style":"path"}, signature_version="s3v4"),
    region_name="us-east-1")

def db(): return psycopg2.connect(DB_URL)

def cleanup(threshold_minutes: int, only_file_id: str|None=None) -> int:
    conn = db(); cur = conn.cursor()
    if only_file_id:
        cur.execute("select id::text, s3_key from file_object where id=%s",(only_file_id,))
    else:
        cur.execute("""
          select id::text, s3_key
          from file_object
          where within_24h = true and server_received_at < now() - (%s)::interval
        """,(f"{threshold_minutes} minutes",))
    rows = cur.fetchall()
    total=0
    for fid, key in rows:
        try: s3.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception as e: print(f"[warn] S3 delete failed for {key}: {e}")
        cur2 = conn.cursor()
        cur2.execute("delete from file_object where id=%s",(fid,))
        cur2.execute("insert into audit_log(action, entity, entity_id, meta) values (%s,%s,%s,%s)",
            ("files.cleanup","file_object",fid, psycopg2.extras.Json({"s3_key":key,"by":"janitor"})))
        cur2.close()
        total += 1
    conn.commit(); cur.close(); conn.close()
    return total

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold-minutes", type=int, default=int(os.getenv("CLEANUP_THRESHOLD_MINUTES","1440")))
    ap.add_argument("--interval-seconds", type=int, default=int(os.getenv("CLEANUP_INTERVAL_SECONDS","300")))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--only-file-id", type=str, default=None)
    args = ap.parse_args()

    if args.once:
        n=cleanup(args.threshold_minutes, args.only_file_id)
        print(f"[janitor] cleaned {n} file(s)")
        return

    while True:
        try:
            n=cleanup(args.threshold_minutes)
            print(f"[janitor] cleaned {n} file(s)")
        except Exception as e:
            print(f"[janitor] ERROR: {e}")
        time.sleep(args.interval_seconds)

if __name__ == "__main__":
    main()
