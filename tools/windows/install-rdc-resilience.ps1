param()
$ErrorActionPreference = 'Stop'
$Root = Join-Path $env:LOCALAPPDATA 'RemoteDesktopCommander'
try {
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$admin = New-Object System.Security.Principal.WindowsPrincipal($identity)
if (-not $admin.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this installer once in an elevated PowerShell window: the S4U background task requires administrator rights.' }
. (Join-Path $PSScriptRoot 'rdc-common.ps1')
Import-Module "$env:SystemRoot\System32\WindowsPowerShell\v1.0\Modules\ScheduledTasks\ScheduledTasks.psd1"
$files = @('rdc-common.ps1','rdc-bootstrap.ps1','rdc-launcher.ps1','rdc-watchdog.ps1','rdc-import-smoke.mjs','repair-rdc-runtime.ps1')
$hashes = ($files | ForEach-Object { Get-RdcFileHash (Join-Path $PSScriptRoot $_) }) -join ''
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $releaseId = ([BitConverter]::ToString($sha.ComputeHash([text.encoding]::UTF8.GetBytes($hashes))).Replace('-','')).Substring(0,16) } finally { $sha.Dispose() }
$Release = Join-Path $Root "releases\$releaseId"
New-Item -ItemType Directory -Force -Path $Release | Out-Null
foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $Release $file) -Force }
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$BackgroundPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$AgentTask = 'Remote Desktop Commander Agent'
$WatchdogTask = 'Remote Desktop Commander Watchdog'
# Preserve pre-install task definitions locally for rollback; never touch the live process.
foreach ($name in @($AgentTask,$WatchdogTask)) {
  $backup = Join-Path $Release "$name.before.xml"
  if (-not (Test-Path $backup) -and (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath $backup -Encoding utf8 }
}
$agentAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $Release 'rdc-launcher.ps1')`""
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$agentSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([timespan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $AgentTask -Action $agentAction -Trigger $agentTrigger -Settings $agentSettings -Principal $Principal -Force | Out-Null
$watchAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$(Join-Path $Release 'rdc-watchdog.ps1')`""
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$watchSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 55) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $WatchdogTask -Action $watchAction -Trigger $watchTrigger -Settings $watchSettings -Principal $BackgroundPrincipal -Force | Out-Null
Enable-ScheduledTask -TaskName $AgentTask | Out-Null
Enable-ScheduledTask -TaskName $WatchdogTask | Out-Null
$history = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration('Microsoft-Windows-TaskScheduler/Operational')
try { $history.IsEnabled = $true; $history.SaveChanges() } finally { $history.Dispose() }
Write-Host "Installed immutable RDC supervision release $releaseId. Agent process was not restarted."
[pscustomobject]@{Status='installed';Release=$Release;InstalledUtc=[datetimeoffset]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'installation-status.json') -Encoding utf8
} catch {
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  [pscustomobject]@{Status='failed';Error=$_.Exception.Message;CheckedUtc=[datetimeoffset]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root 'installation-status.json') -Encoding utf8
  throw
}
