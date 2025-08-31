$ROOT = Split-Path -Parent (Get-Location).Path
$CFG = Join-Path $ROOT "config"
New-Item -ItemType Directory -Force -Path $CFG | Out-Null

@"
version: 1
labels:
  system: { MIS: "Meetings im System", OSR: "Off-System-Rate", PPM: "Problems per Meeting", ODcov: "Outcome-Doc Coverage" }
  roles:  { Sponsor: "Sponsor", Admin: "Admin", Owner: "Owner", Deputy: "Stv. Owner", Leader: "Leader", Viewer: "Viewer" }
  meetings: { weekly: "Weekly", biweekly: "Zweiwöchentlich" }
notes: { engine_constant: "Schwellen/Logik bleiben unverändert; nur Labels variieren" }
"@ | Set-Content -Path (Join-Path $CFG "terminology.yaml") -Encoding UTF8

@"
version: 1
tool_name: "Gatetool"
grade_bands:
  hervorragend: { min: 90 }
  sehr_stark:  { min: 80, max: 89 }
  solide:      { min: 70, max: 79 }
  ausreichend: { min: 60, max: 69 }
  verbesserungsfaehig: { min: 50, max: 59 }
  nicht_bestanden: { max: 49 }
vc_levels: { VC4: "Practitioner", VC5: "Leader/Implementer" }
rules:
  G1: { must: ["opt_in_pct >= 70","NOT (ps_score < 3.0 AND efficacy_score < 3.0)","live_trial_days <= 14","artifact_present == true"] }
  G2: { must: ["MIS >= 80","OSR <= 20","leader_ref_per_week >= 1"] }
  G3: { must: ["cadence_pct >= 85","ODcov >= 90","PPM >= 50","OSR <= 20","delta_OSR <= 0","VC == 5"] }
  G4: { must: ["traceability_majority == true","recognition_every_2nd == true"] }
  G5: { must: ["restart_latency_days <= 14","scope_ok_5_fields == true","voice_pct >= 60","VC == 5"] }
finals:
  gate_fail_overrides_grade: true
  pdf:
    header: { show_tool_version: true, show_qr_signature: true }
    pages: ["certificate_summary","gate_G1","gate_G2","gate_G3","gate_G4","gate_G5"]
"@ | Set-Content -Path (Join-Path $CFG "certificate.yaml") -Encoding UTF8
