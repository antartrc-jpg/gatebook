$ErrorActionPreference="Stop"
Write-Host "==> MINIO: Bucket & CORS" -ForegroundColor Cyan
$INFRA=(Get-Location).Path
$corsPath = Join-Path $INFRA "cors.json"

@"
{
  "CORSRules": [
    {
      "AllowedOrigins": ["http://localhost:5173","http://localhost"],
      "AllowedMethods": ["GET","PUT","POST","HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag","x-amz-request-id","x-amz-request-id-2"],
      "MaxAgeSeconds": 3000
    }
  ]
}
"@ | Set-Content -Path $corsPath -Encoding UTF8

$MCHOST  = 'http://admin:admin123456@minio:9000'
docker run --rm --network infra_default -e "MC_HOST_minio=$MCHOST" minio/mc mb -p minio/artifacts 2>$null | Out-Null
docker run --rm --network infra_default -v "$INFRA:/work" -e "MC_HOST_minio=$MCHOST" minio/mc cors set minio/artifacts /work/cors.json
docker run --rm --network infra_default -e "MC_HOST_minio=$MCHOST" minio/mc cors info minio/artifacts
Write-Host "==> MINIO: CORS gesetzt" -ForegroundColor Green
