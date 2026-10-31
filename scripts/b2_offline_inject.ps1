# B2 offline inject for order-bot (Windows).
# Primary: mihomo/Clash Meta ext-ctl GLOBAL->REJECT (reversible). System uses mixed-port 7897.
# Restore: powershell -NoProfile -File scripts\b2_offline_inject.ps1 -RestoreOnly
#
# Secondary (often ineffective here): IP firewall vs CDN; hosts blackhole while proxy TCP still green.
# Do NOT use -TriggerManual against resident with recoverable conflicted batches (e.g. batch 17).
param(
  [switch]$RestoreOnly,
  [switch]$BlockOnly,
  [string]$Ctl = "http://127.0.0.1:39797",
  [string]$StateFile = "docs\验证记录\_b2_mihomo_state.json"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Join-Path $PSScriptRoot '..')

function Save-State {
  $cfg = Invoke-RestMethod "$Ctl/configs"
  $globalNow = (Invoke-RestMethod "$Ctl/proxies/GLOBAL").now
  $obj = [pscustomobject]@{ mode = $cfg.mode; global = $globalNow; savedAt = (Get-Date -Format o) }
  $dir = Split-Path $StateFile -Parent
  if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $obj | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
  return $obj
}

function Restore-Mihomo {
  if (-not (Test-Path $StateFile)) {
    # best-effort defaults
    try { Invoke-RestMethod -Method Patch -Uri "$Ctl/configs" -ContentType "application/json" -Body '{"mode":"rule"}' | Out-Null } catch {}
    Write-Host "NO_STATE_FILE; attempted mode=rule"
    return
  }
  $st = Get-Content $StateFile -Encoding UTF8 | ConvertFrom-Json
  try {
    $body = '{"name":"' + $st.global + '"}'
    Invoke-RestMethod -Method Put -Uri "$Ctl/proxies/GLOBAL" -ContentType "application/json" -Body $body | Out-Null
    Write-Host "RESTORED_GLOBAL $($st.global)"
  } catch { Write-Host "RESTORE_GLOBAL_ERR $_" }
  try {
    Invoke-RestMethod -Method Patch -Uri "$Ctl/configs" -ContentType "application/json" -Body ('{"mode":"'+$st.mode+'"}') | Out-Null
    Write-Host "RESTORED_MODE $($st.mode)"
  } catch { Write-Host "RESTORE_MODE_ERR $_" }
  # also clear any leftover IP firewall rules from older helper versions
  Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "OrderBot-B2-Offline*" } | ForEach-Object {
    Remove-NetFirewallRule -Name $_.Name -ErrorAction SilentlyContinue
    Write-Host "REMOVED_FW $($_.DisplayName)"
  }
}

function Block-Mihomo {
  $st = Save-State
  Write-Host "SAVED mode=$($st.mode) global=$($st.global)"
  Invoke-RestMethod -Method Patch -Uri "$Ctl/configs" -ContentType "application/json" -Body '{"mode":"global"}' | Out-Null
  Invoke-RestMethod -Method Put -Uri "$Ctl/proxies/GLOBAL" -ContentType "application/json" -Body '{"name":"REJECT"}' | Out-Null
  Write-Host "BLOCKED mode=global GLOBAL=REJECT"
}

function Test-Resident {
  $a = [bool](Get-Process -Id 14016 -ErrorAction SilentlyContinue)
  $b = [bool](Get-Process -Id 23336 -ErrorAction SilentlyContinue)
  $l = [bool](Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue)
  Write-Host "resident p14016=$a p23336=$b listen18765=$l"
}

function Test-Feishu {
  & .\.venv\Scripts\python.exe -c @"
import httpx, yaml
from pathlib import Path
cfg=yaml.safe_load(Path(r'data/test-runtime-20261023/data/config.yaml').read_text(encoding='utf-8'))
try:
  r=httpx.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
               json={'app_id':cfg['feishu']['app_id'],'app_secret':'x'}, timeout=8)
  print('feishu_http', r.status_code)
except Exception as e:
  print('feishu_err', type(e).__name__)
"@
}

if ($RestoreOnly) { Restore-Mihomo; Write-Host "RESTORE_DONE"; exit 0 }

Write-Host "=== BASELINE ==="
Test-Resident
Test-Feishu
Block-Mihomo
Start-Sleep -Seconds 1
Write-Host "=== DURING BLOCK ==="
Test-Resident
Test-Feishu
try {
  $code = (Invoke-WebRequest http://127.0.0.1:18765/ -UseBasicParsing -TimeoutSec 5).StatusCode
  Write-Host "http_root $code"
} catch { Write-Host "http_root FAIL" }

if ($BlockOnly) {
  Write-Host "BLOCK_ONLY_ACTIVE; restore: powershell -NoProfile -File scripts\b2_offline_inject.ps1 -RestoreOnly"
  exit 0
}

Write-Host "=== RESTORE ==="
Restore-Mihomo
Start-Sleep -Seconds 1
Test-Resident
Test-Feishu
Write-Host "DONE"

