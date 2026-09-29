param()
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "RemoteDesktopCommander"
$LogDir = Join-Path $Root "logs"
$Node = "C:\Program Files\nodejs\node.exe"
$Package = "@wonderwhy-er/desktop-commander"
$Version = "0.2.51"
$Entry = Join-Path $env:APPDATA "npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
$ExpectedSha256 = "A4145198DC75CD34E7C7452C2054B4CC0D29E199AE1459961A17E4DA25A13500"
$RecoveryDir = Join-Path $Root "recovery"
$RecoveryEntry = Join-Path $RecoveryDir "desktop-commander-0.2.51-index.js"
$RepairState = Join-Path $Root "repair-state.json"
New-Item -ItemType Directory -Force -Path $LogDir,$RecoveryDir | Out-Null
$Log = Join-Path $LogDir "bootstrap.log"
function Write-Log([string]$Message) { "[$(Get-Date -Format o)] $Message" | Out-File $Log -Append -Encoding utf8 }
function Test-Entry {
  if (-not (Test-Path -LiteralPath $Entry)) { return $false }
  return (Get-FileHash -LiteralPath $Entry -Algorithm SHA256).Hash -eq $ExpectedSha256
}
if (Test-Entry) {
  if (-not (Test-Path $RecoveryEntry) -or (Get-FileHash $RecoveryEntry -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    Copy-Item -LiteralPath $Entry -Destination $RecoveryEntry -Force
    Write-Log "refreshed verified recovery entry"
  }
  Remove-Item -LiteralPath $RepairState -Force -ErrorAction SilentlyContinue
  Write-Log "entry healthy"; exit 0
}
if ((Test-Path $RecoveryEntry) -and (Get-FileHash $RecoveryEntry -Algorithm SHA256).Hash -eq $ExpectedSha256) {
  New-Item -ItemType Directory -Force -Path (Split-Path $Entry) | Out-Null
  Copy-Item -LiteralPath $RecoveryEntry -Destination $Entry -Force
  if (Test-Entry) { Write-Log "restored entry from verified local recovery copy"; exit 0 }
}
$now = (Get-Date).ToUniversalTime()
$state = if (Test-Path $RepairState) { Get-Content $RepairState -Raw | ConvertFrom-Json } else { [pscustomobject]@{ Count=0; WindowStartUtc=$now.ToString('o') } }
$windowStart = [datetime]$state.WindowStartUtc
if (($now - $windowStart).TotalHours -ge 24) { $state = [pscustomobject]@{ Count=0; WindowStartUtc=$now.ToString('o') } }
if ([int]$state.Count -ge 2) { Write-Log "entry unhealthy; repair limit reached; refusing reinstall loop"; exit 31 }
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { Write-Log "entry unhealthy; npm.cmd missing"; exit 32 }
$state.Count = [int]$state.Count + 1
$state | ConvertTo-Json | Set-Content -LiteralPath $RepairState -Encoding utf8
Write-Log "entry unhealthy; repairing exact package $Package@$Version attempt=$($state.Count)"
& $npm install -g "$Package@$Version" --ignore-scripts --no-audit --no-fund 2>&1 | Out-File $Log -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) { Write-Log "npm repair failed exit=$LASTEXITCODE"; exit 33 }
if (-not (Test-Entry)) { Write-Log "repair completed but entry hash mismatch/missing"; exit 34 }
Remove-Item -LiteralPath $RepairState -Force -ErrorAction SilentlyContinue
Write-Log "repair verified sha256=$ExpectedSha256"
exit 0
