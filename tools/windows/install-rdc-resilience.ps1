param()
$ErrorActionPreference = "Stop"
$Repo = Split-Path (Split-Path $PSScriptRoot)
$Launcher = Join-Path $PSScriptRoot "rdc-launcher.ps1"
$Watchdog = Join-Path $PSScriptRoot "rdc-watchdog.ps1"
$AgentTask = "Remote Desktop Commander Agent"
$WatchdogTask = "Remote Desktop Commander Watchdog"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$agentAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$agentSettings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $AgentTask -Action $agentAction -Trigger $agentTrigger -Settings $agentSettings -Description "Stable Remote Desktop Commander agent launcher." -Force | Out-Null
$watchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`""
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$watchSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $WatchdogTask -Action $watchAction -Trigger $watchTrigger -Settings $watchSettings -Description "Repairs disabled/stopped RDC user agent." -Force | Out-Null
Write-Host "Installed $AgentTask and $WatchdogTask for $User"
