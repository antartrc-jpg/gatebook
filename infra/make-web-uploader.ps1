$ErrorActionPreference="Stop"
$INFRA=(Get-Location).Path
$WEB = Join-Path (Split-Path -Parent $INFRA) "web"
New-Item -ItemType Directory -Force -Path $WEB | Out-Null
$HTML = @"
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Gatebook – Mini Uploader</title>
  <style>body{font-family:system-ui;margin:2rem} input,button{font-size:1rem}</style>
</head>
<body>
  <h1>Mini Uploader (API2 @8081)</h1>
  <input id="file" type="file"/>
  <button id="btn">Upload</button>
  <pre id="out"></pre>
<script>
const out = (m)=>document.getElementById('out').textContent += m+"\\n";
document.getElementById('btn').onclick = async ()=>{
  const f = document.getElementById('file').files[0];
  if(!f){ out("Bitte Datei wählen"); return; }
  out("Presign...");
  const pres = await fetch('http://localhost:8081/files/presign', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ filename: f.name, content_type: f.type || 'application/octet-stream', size_bytes: f.size })
  }).then(r=>r.json());
  out("PUT -> S3...");
  await fetch(pres.url, { method:'PUT', headers:{'Content-Type': f.type || 'application/octet-stream'}, body: f });
  const buf = await f.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', buf);
  const sha = Array.from(new Uint8Array(digest)).map(b=>b.toString(16).padStart(2,'0')).join('');
  out("Confirm...");
  const conf = await fetch('http://localhost:8081/files/confirm', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ file_id: pres.file_id, sha256_hex: sha })
  }).then(r=>r.json());
  out("Fertig: "+JSON.stringify(conf));
};
</script>
</body>
</html>
"@
Set-Content -Path (Join-Path $WEB "upload.html") -Encoding UTF8 -Value $HTML
Write-Host "==> Web Uploader erstellt: $WEB\upload.html" -ForegroundColor Green
