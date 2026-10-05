<#
One-time setup of the KumoSQL test runner on Windows. Run in a normal (non-admin) PowerShell:

  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -Dir D:\kumo-runner -DefenderExclusion   # last flag needs an admin shell

It installs Python 3.12 and Git with winget when they are missing, puts runner.py and config.json in the folder, stops the
machine sleeping while plugged in, signs git in to GitHub (a browser window opens once) by pushing a heartbeat to the
results branch, and registers a scheduled task that starts the runner at every log-on and restarts it if it dies.
#>
param(
  [string]$Dir = "$env:USERPROFILE\kumo-runner",
  [string]$Base = "https://raw.githubusercontent.com/walterogozaly/KumoSQL/claude/project-thread-lm6mcq/dell-runner",
  [switch]$DefenderExclusion
)
$ErrorActionPreference = "Stop"

function Find-Python {
  $attempts = @(
    @{ Exe = "py"; Args = @("-3.12", "-c", "import sys; print(sys.executable)") },
    @{ Exe = "py"; Args = @("-3.11", "-c", "import sys; print(sys.executable)") },
    @{ Exe = "python"; Args = @("-c", "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')") }
  )
  foreach ($attempt in $attempts) {
    if (-not (Get-Command $attempt.Exe -ErrorAction SilentlyContinue)) { continue }
    try {
      $exe = (& $attempt.Exe @($attempt.Args) 2>$null | Select-Object -First 1)
      if ($exe -and (Test-Path $exe) -and ($exe -notmatch "WindowsApps")) { return $exe }
    } catch {}
  }
  return $null
}

$python = Find-Python
if (-not $python) {
  Write-Host "Installing Python 3.12 with winget"
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $python = Find-Python
}
if (-not $python) { throw "Python 3.11+ not found. Install it from python.org (tick 'Add to PATH'), then rerun." }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "Installing Git with winget (accept the admin prompt)"
  winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git not found after install. Open a new PowerShell and rerun." }
git config --global core.longpaths true

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
foreach ($file in @("runner.py", "config.json")) {
  if ((Test-Path "$Dir\$file") -and ($file -eq "config.json")) { Write-Host "keeping your existing config.json"; continue }
  if (Test-Path "$PSScriptRoot\$file") { Copy-Item "$PSScriptRoot\$file" "$Dir\$file" -Force }
  else { Invoke-WebRequest "$Base/$file" -OutFile "$Dir\$file" }
}

powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15

if ($DefenderExclusion) {
  try { Add-MpPreference -ExclusionPath "$Dir\work"; Write-Host "Defender exclusion added for $Dir\work (faster tests)" }
  catch { Write-Warning "Defender exclusion needs an admin PowerShell; skipped." }
}

Write-Host "Signing git in: a browser window may open. Approve it once."
Push-Location $Dir
& $python runner.py init
$initOk = ($LASTEXITCODE -eq 0)
Pop-Location
if (-not $initOk) { throw "init failed: git could not push to the results branch. Fix the GitHub sign-in, then run: $python $Dir\runner.py init" }

$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "runner.py run" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "KumoSQL Dell Runner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "KumoSQL Dell Runner"

Write-Host ""
Write-Host "Done. The runner is running in the background (task 'KumoSQL Dell Runner')."
Write-Host "  status:  cd $Dir; & '$python' runner.py status"
Write-Host "  log:     Get-Content $Dir\work\logs\runner.log -Tail 20 -Wait"
Write-Host "  stop:    Stop-ScheduledTask 'KumoSQL Dell Runner'    remove: Unregister-ScheduledTask 'KumoSQL Dell Runner'"
