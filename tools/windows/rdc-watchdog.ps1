param()
$ErrorActionPreference = "Stop"
$TaskName = "Remote Desktop Commander Agent"
$Root = Join-Path $env:LOCALAPPDATA "RemoteDesktopCommander"
$LogDir = Join-Path $Root "logs"
$Entry = Join-Path $env:APPDATA "npm\node_modules\@wonderwhy-er\desktop-commander\dist\index.js"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "watchdog.log"
function Write-Log([string]$Message) { "[$(Get-Date -Format o)] $Message" | Out-File $Log -Append -Encoding utf8 }
try {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
  if (-not $task.Settings.Enabled) {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Log "enabled agent task"
    $task = Get-ScheduledTask -TaskName $TaskName
  }
  $agents = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like "*$Entry*" -and $_.CommandLine -match '\sremote(?:\s|$)' })
  if ($agents.Count -gt 0) {
    $failurePath = Join-Path $Root "watchdog-failure.json"
    if (Test-Path $failurePath) {
      Remove-Item -LiteralPath $failurePath -Force
      Write-Log "agent healthy pid=$($agents[0].ProcessId); cleared restart backoff state"
    } else {
      Write-Log "agent healthy pid=$($agents[0].ProcessId)"
    }
    exit 0
  }
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  $now = Get-Date
  $failurePath = Join-Path $Root "watchdog-failure.json"
  $failure = if (Test-Path $failurePath) { Get-Content $failurePath -Raw | ConvertFrom-Json } else { [pscustomobject]@{ Count=0; LastAttemptUtc='2000-01-01T00:00:00Z' } }
  $delayMinutes = [Math]::Min(30, [Math]::Pow(2, [Math]::Min([int]$failure.Count, 4)))
  if (($now.ToUniversalTime() - [datetime]$failure.LastAttemptUtc).TotalMinutes -lt $delayMinutes) {
    Write-Log "agent absent; restart backoff active (${delayMinutes}m)"
    exit 0
  }
  if ($task.State -eq 'Running') {
    Write-Log "task reports Running without matching stable agent; stopping stale task instance"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
  }
  Write-Log "agent missing; starting task; priorResult=0x$('{0:X8}' -f $info.LastTaskResult)"
  [pscustomobject]@{ Count=([int]$failure.Count + 1); LastAttemptUtc=$now.ToUniversalTime().ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $failurePath -Encoding utf8
  Start-ScheduledTask -TaskName $TaskName
} catch { Write-Log "watchdog error: $($_.Exception.Message)"; exit 1 }
