param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'rdc-common.ps1')
Import-Module "$env:SystemRoot\System32\WindowsPowerShell\v1.0\Modules\ScheduledTasks\ScheduledTasks.psd1"
$TaskName = 'Remote Desktop Commander Agent'
$Root = Join-Path $env:LOCALAPPDATA 'RemoteDesktopCommander'
$LogDir = Join-Path $Root 'logs'
$PackageDir = Join-Path $env:APPDATA 'npm\node_modules\@wonderwhy-er\desktop-commander'
$Entry = Join-Path $PackageDir 'dist\index.js'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'watchdog.log'
$FailurePath = Join-Path $Root 'watchdog-failure.json'
$HealthPath = Join-Path $Root 'supervision-status.json'
function Write-Log([string]$Message) { "[$([datetimeoffset]::UtcNow.ToString('o'))] $Message" | Out-File $Log -Append -Encoding utf8 }
function Write-Health([string]$Status, [string]$Detail) {
  [pscustomobject]@{Status=$Status;Detail=$Detail;CheckedUtc=[datetimeoffset]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $HealthPath -Encoding utf8
}
try {
  $task = Get-ScheduledTask -TaskName $TaskName
  if (-not $task.Settings.Enabled) { Enable-ScheduledTask -TaskName $TaskName | Out-Null; $task = Get-ScheduledTask -TaskName $TaskName }
  $agents = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { $_.CommandLine -like "*$Entry*" -and $_.CommandLine -match '\sremote(?:\s|$)' })
  if ($agents.Count -gt 0) {
    Remove-Item -LiteralPath $FailurePath -Force -ErrorAction SilentlyContinue
    Write-Health 'process_alive' "pid=$($agents[0].ProcessId); backend connectivity requires RPC verification"
    exit 0
  }
  $problem = Get-RdcPackageProblem $PackageDir
  if ($problem) {
    $av = Get-RdcAvCorrelation (Get-Date).AddDays(-1)
    $detail = "integrity failure: $problem"
    if ($av) { $detail += "; correlated avp event at $($av.TimeCreated.ToUniversalTime().ToString('o')); review antivirus before maintenance" }
    Write-Health 'integrity_blocked' $detail
    Write-Log $detail
    exit 21
  }
  $failure = $null
  if (Test-Path $FailurePath) { try { $failure = Get-Content $FailurePath -Raw | ConvertFrom-Json } catch { Write-Log 'discarding invalid retry state' } }
  $plan = Get-RdcRetryPlan $failure ([datetimeoffset]::UtcNow)
  if (-not $plan.Allowed) { Write-Health 'backoff' "delay=$($plan.DelayMinutes)m"; exit 0 }
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  # Allow a just-started launcher to validate dependencies and connect.
  if ($task.State -eq 'Running' -and ((Get-Date) - $info.LastRunTime).TotalSeconds -lt 60) { Write-Health 'starting' 'launcher startup grace'; exit 0 }
  if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 2 }
  [pscustomobject]@{Count=($plan.Count+1);LastAttemptUtc=[datetimeoffset]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $FailurePath -Encoding utf8
  Start-ScheduledTask -TaskName $TaskName
  Write-Log "agent absent; started task attempt=$($plan.Count+1)"
  Write-Health 'starting' 'agent task started'
} catch { Write-Health 'supervisor_error' $_.Exception.Message; Write-Log $_.Exception.Message; exit 1 }
