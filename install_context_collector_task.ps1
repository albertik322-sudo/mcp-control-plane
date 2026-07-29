$ErrorActionPreference = "Stop"
if (Get-Variable PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    $Global:PSNativeCommandUseErrorActionPreference = $false
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$TaskRunner = Join-Path $Root "context_collector_task.py"
$HiddenRunner = Join-Path $Root "context_collector_hidden.vbs"
$Log = Join-Path $Root ".runtime\context_collector.log"
$TaskName = "CodexContextCollectorSilent"
$LegacyTaskName = "CodexContextCollector"

if (!(Test-Path $TaskRunner)) {
    throw "Task runner not found: $TaskRunner"
}
if (!(Test-Path $Pythonw)) {
    throw "pythonw.exe not found: $Pythonw"
}
if (!(Test-Path $HiddenRunner)) {
    throw "Hidden runner not found: $HiddenRunner"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null

$fso = New-Object -ComObject Scripting.FileSystemObject
$HiddenRunnerShort = $fso.GetFile($HiddenRunner).ShortPath

# Best-effort cleanup for the original console-based task. This can require an elevated token.
& cmd.exe /c "schtasks /Delete /TN $LegacyTaskName /F >nul 2>nul"

$TaskCommand = "wscript.exe $HiddenRunnerShort"
& schtasks.exe /Create /TN $TaskName /SC MINUTE /MO 15 /TR $TaskCommand /F | Out-Null
Set-ScheduledTask `
    -TaskName $TaskName `
    -Settings (New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew) | Out-Null
& schtasks.exe /Run /TN $TaskName | Out-Null
& schtasks.exe /Query /TN $TaskName /FO LIST
