param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [int]$Port = 3017,
    [string]$ConformanceVersion = "0.2.0-alpha.10",
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverPath = Join-Path $projectRoot "tests\conformance_http_server.py"
$serverProcess = $null
$exitCode = 1

try {
    $env:MCP_CONFORMANCE_PORT = [string]$Port
    $serverProcess = Start-Process `
        -FilePath $Python `
        -ArgumentList @("`"$serverPath`"") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($serverProcess.HasExited) {
            throw "Conformance server exited early with code $($serverProcess.ExitCode)."
        }
        try {
            $client = [System.Net.Sockets.TcpClient]::new()
            $client.Connect("127.0.0.1", $Port)
            $client.Dispose()
            $ready = $true
            break
        }
        catch {
            if ($null -ne $client) {
                $client.Dispose()
            }
            Start-Sleep -Milliseconds 250
        }
    }

    if (-not $ready) {
        throw "Conformance server did not listen on 127.0.0.1:$Port."
    }

    $conformanceArgs = @(
        "--yes",
        "@modelcontextprotocol/conformance@$ConformanceVersion",
        "server",
        "--url",
        "http://127.0.0.1:$Port/mcp",
        "--scenario",
        "server-stateless",
        "--spec-version",
        "2026-07-28"
    )
    if ($VerboseOutput) {
        $conformanceArgs += "--verbose"
    }
    & npx.cmd @conformanceArgs
    $exitCode = $LASTEXITCODE
}
finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
        $serverProcess.WaitForExit()
    }
    Remove-Item Env:MCP_CONFORMANCE_PORT -ErrorAction SilentlyContinue
}

exit $exitCode
