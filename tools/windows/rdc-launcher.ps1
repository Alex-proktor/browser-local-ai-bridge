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
if (-not (Test-Path $Node)) { "[$(Get-Date -Format o)] node missing" | Out-File $status -Append; exit 20 }
if (-not (Test-Path $Bootstrap)) { "[$(Get-Date -Format o)] bootstrap missing" | Out-File $status -Append; exit 21 }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $Bootstrap
if ($LASTEXITCODE -ne 0) { "[$(Get-Date -Format o)] bootstrap failed code=$LASTEXITCODE" | Out-File $status -Append; exit $LASTEXITCODE }
$process = Start-Process -FilePath $Node -ArgumentList @('"' + $Entry + '"', 'remote') -WorkingDirectory (Split-Path $Entry) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
"[$(Get-Date -Format o)] agent started pid=$($process.Id) stdout=$stdout stderr=$stderr" | Out-File $status -Append
$process.WaitForExit()
$process.Refresh()
$code = $process.ExitCode
"[$(Get-Date -Format o)] agent exit code=$code" | Out-File $status -Append
exit $code
