$RdcVersion = '0.2.51'
$RdcEntryHash = 'A4145198DC75CD34E7C7452C2054B4CC0D29E199AE1459961A17E4DA25A13500'
function Get-RdcFileHash([string]$Path) {
  $stream = [System.IO.File]::OpenRead($Path)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','') } finally { $stream.Dispose(); $sha.Dispose() }
}
function Get-RdcPackageProblem([string]$PackageDir) {
  $metadata = Join-Path $PackageDir 'package.json'
  if (-not (Test-Path -LiteralPath $metadata)) { return 'package.json missing' }
  try { $package = Get-Content -LiteralPath $metadata -Raw | ConvertFrom-Json } catch { return 'package.json invalid' }
  if ($package.version -ne $RdcVersion) { return "version mismatch: $($package.version)" }
  $entry = Join-Path $PackageDir 'dist\index.js'
  if (-not (Test-Path -LiteralPath $entry)) { return 'entry missing' }
  if ((Get-RdcFileHash $entry) -ne $RdcEntryHash) { return 'entry hash mismatch' }
  foreach ($dependency in $package.dependencies.PSObject.Properties.Name) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir "node_modules\$dependency\package.json"))) { return "dependency missing: $dependency" }
  }
  return $null
}
function Get-RdcRetryPlan($State, [datetimeoffset]$Now) {
  $count = 0
  $elapsed = [double]::PositiveInfinity
  if ($State) {
    try {
      $last = [datetimeoffset]::Parse([string]$State.LastAttemptUtc, [cultureinfo]::InvariantCulture)
      $elapsed = ($Now.ToUniversalTime() - $last.ToUniversalTime()).TotalMinutes
      $count = [Math]::Max(0, [int]$State.Count)
    } catch { $elapsed = [double]::PositiveInfinity }
  }
  if ($elapsed -lt -1 -or $elapsed -ge 1440) { $elapsed = [double]::PositiveInfinity; $count = 0 }
  $delay = [Math]::Min(30, [Math]::Pow(2, [Math]::Min($count, 5)))
  return [pscustomobject]@{ Allowed=($elapsed -ge $delay); Count=$count; DelayMinutes=$delay }
}
function Get-RdcAvCorrelation([datetime]$Since) {
  try {
    $events = Get-WinEvent -FilterHashtable @{LogName='Application';Id=4662;StartTime=$Since} -MaxEvents 500 -ErrorAction Stop
    return $events | Where-Object { $_.ProviderName -eq 'avp' -and ($_.Properties.Value -join ' ') -like "*$RdcEntryHash*" } | Select-Object -First 1
  } catch { return $null }
}
