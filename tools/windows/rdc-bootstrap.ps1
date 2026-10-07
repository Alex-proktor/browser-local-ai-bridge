param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'rdc-common.ps1')
$packageDir = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander'
$problem = Get-RdcPackageProblem $packageDir
if ($problem) {
  Write-Error "RDC integrity failure: $problem. Run repair-rdc-runtime.ps1 after reviewing antivirus reports." -ErrorAction Continue
  exit 21
}
& 'C:\Program Files\nodejs\node.exe' (Join-Path $PSScriptRoot 'rdc-import-smoke.mjs') $packageDir
if ($LASTEXITCODE -ne 0) { exit 23 }
exit 0
