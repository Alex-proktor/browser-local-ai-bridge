param()
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "RemoteDesktopCommander"
$LogDir = Join-Path $Root "logs"
$Node = "C:\Program Files\nodejs\node.exe"
$Entry = Join-Path $env:APPDATA "npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
$Bootstrap = Join-Path $PSScriptRoot "rdc-bootstrap.ps1"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdout = Join-Path $LogDir "agent-$stamp.stdout.log"
$stderr = Join-Path $LogDir "agent-$stamp.stderr.log"
$status = Join-Path $LogDir "agent-$stamp.status.log"
"[$(Get-Date -Format o)] launching node=$Node entry=$Entry args=remote" | Out-File $status -Encoding utf8
if (-not (Test-Path $Node)) { "[$(Get-Date -Format o)] node missing" | Out-File $status -Append -Encoding utf8; exit 20 }
if (-not (Test-Path $Bootstrap)) { "[$(Get-Date -Format o)] bootstrap missing" | Out-File $status -Append -Encoding utf8; exit 21 }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $Bootstrap
if ($LASTEXITCODE -ne 0) { "[$(Get-Date -Format o)] bootstrap failed code=$LASTEXITCODE" | Out-File $status -Append -Encoding utf8; exit $LASTEXITCODE }
$mutex = New-Object System.Threading.Mutex($false, 'Local\RDC-Agent-Launcher')
try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { $mutex.Dispose(); exit 0 }
try {
$process = Start-Process -FilePath $Node -ArgumentList @('"' + $Entry + '"', 'remote') -WorkingDirectory (Split-Path $Entry) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
# Retain the handle before exit; otherwise Windows PowerShell may return a null ExitCode.
$null = $process.Handle
"[$(Get-Date -Format o)] agent started pid=$($process.Id) stdout=$stdout stderr=$stderr" | Out-File $status -Append -Encoding utf8
$process.WaitForExit()
$process.Refresh()
$code = $process.ExitCode
if ($null -eq $code) { $code = 22 }
"[$(Get-Date -Format o)] agent exit code=$code" | Out-File $status -Append -Encoding utf8
exit $code
} finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
