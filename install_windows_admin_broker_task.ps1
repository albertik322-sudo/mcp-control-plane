param(
    [string]$TaskName = "CodexWindowsAdminBroker"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = (Get-Command python.exe -ErrorAction Stop).Source
}

$AdminRoot = Join-Path $Root ".runtime\windows_admin"
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$User = $Identity.Name
$UserSid = $Identity.User.Value
$AdministratorsSid = "S-1-5-32-544"
$SystemSid = "S-1-5-18"

if (Test-Path $AdminRoot) {
    & takeown.exe /F $AdminRoot /A /R /D Y | Out-Null
    & icacls.exe $AdminRoot /setowner "*$AdministratorsSid" /T /C | Out-Null
    & icacls.exe $AdminRoot /grant "*${AdministratorsSid}:(OI)(CI)(F)" "*${SystemSid}:(OI)(CI)(F)" /T /C | Out-Null
}

foreach ($dir in @($AdminRoot, "$AdminRoot\inbox", "$AdminRoot\outbox", "$AdminRoot\processed", "$AdminRoot\logs")) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

icacls $AdminRoot /inheritance:r | Out-Null
icacls $AdminRoot /grant:r "*${UserSid}:(OI)(CI)(F)" "*${AdministratorsSid}:(OI)(CI)(F)" "*${SystemSid}:(OI)(CI)(F)" /T /C | Out-Null
icacls $AdminRoot /setintegritylevel "(OI)(CI)M" | Out-Null

$Broker = Join-Path $Root "windows_admin_broker.py"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Broker`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddDays(1)
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Elevated allowlisted admin broker for Codex Windows MCP."
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

Write-Host "Installed scheduled task $TaskName for $User"
Write-Host "Broker: $Broker"
Write-Host "Runtime: $AdminRoot"
