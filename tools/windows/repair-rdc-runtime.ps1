param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'rdc-common.ps1')
$packageDir = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander'
if (Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like "*$packageDir*" }) { throw 'RDC is running; repair must not mutate a live package.' }
# Explicit maintenance only; supervision never reinstalls or restores quarantined files.
& npm.cmd install --global --force "@wonderwhy-er/desktop-commander@$RdcVersion" --ignore-scripts --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "npm repair failed: $LASTEXITCODE" }
$problem = Get-RdcPackageProblem $packageDir
if ($problem) { throw "RDC repair integrity failure: $problem" }
Write-Host "RDC $RdcVersion global package verified. Authentication was not changed."
