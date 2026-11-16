[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoBrowser,
    [switch]$SmokeTest
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runDirectory = Join-Path $projectRoot '.run'
$startedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$launcherLock = $null
$exitCode = 0

function Test-LocalPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync('127.0.0.1', $Port)
        return ($connection.Wait(500) -and $client.Connected)
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Get-Setting([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -ne $value) { return $value }
    foreach ($line in Get-Content -LiteralPath (Join-Path $projectRoot '.env')) {
        if ($line -match ('^\s*(?:export\s+)?' + [regex]::Escape($Name) + '\s*=\s*(.*)$')) {
            return $Matches[1].Trim().Trim('"', "'")
        }
    }
    return ''
}

function Start-ServiceProcess([string]$Name, [string]$Executable, [string[]]$Arguments) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runDirectory "$Name.log") `
        -RedirectStandardError (Join-Path $runDirectory "$Name.error.log")
    $startedProcesses.Add($process)
    return $process
}

function Wait-Service([string]$Name, [System.Diagnostics.Process]$Process, [string]$Url) {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) { throw "$Name exited. See .run/$Name.error.log and .run/$Name.log." }
        try {
            # A 401 from /api/auth/me also proves that the API finished its lifespan startup.
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        }
        catch {
            if ($Name -eq 'api' -and $null -ne $_.Exception.Response -and
                [int]$_.Exception.Response.StatusCode -eq 401) { return }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name was not ready within 120 seconds. See .run/$Name.error.log and .run/$Name.log."
}

function Stop-OwnedProcessTree([System.Diagnostics.Process]$Process) {
    if ($Process.HasExited) { return }
    # PowerShell process control also works in environments where taskkill is unavailable.
    $children = @()
    try { $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Process.Id)" -ErrorAction Stop) }
    catch { Write-Warning 'Cannot inspect child processes; stopping the directly launched process.' }
    foreach ($child in $children) {
        $childProcess = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $childProcess) {
            try {
                if ($childProcess.StartTime -ge $Process.StartTime) { Stop-OwnedProcessTree $childProcess }
            }
            finally { $childProcess.Dispose() }
        }
    }
    if (-not $Process.HasExited) { $Process.Kill() }
}

function Wait-LauncherExit {
    if ([Console]::IsInputRedirected) {
        throw 'Interactive console input is required. Double-click start.bat, or use -SmokeTest for an unattended check.'
    }
    if (-not ('LauncherConsole' -as [type])) {
        Add-Type -Path (Join-Path $projectRoot 'scripts/LauncherConsole.cs')
    }
    $consoleInput = [LauncherConsole]::new()
    try {
        Write-Host 'Keep this window open. Press ENTER or Ctrl+C in this window to stop KnowFlow.'
        while ($true) {
            foreach ($process in $startedProcesses) {
                if ($process.HasExited) { throw 'A service exited unexpectedly. Check .run logs.' }
            }
            if ($consoleInput.StopRequested()) {
                Write-Host 'Stopping KnowFlow...'
                return
            }
            Start-Sleep -Milliseconds 100
        }
    }
    finally { $consoleInput.Dispose() }
}

try {
    foreach ($command in @('uv.exe', 'node.exe')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Missing $command. Install uv, Python 3.12 and Node.js 24/npm 11, then reopen this launcher."
        }
    }
    foreach ($path in @('.env', 'apps/api/.venv/Scripts/python.exe', 'apps/web/node_modules/vite/bin/vite.js')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $path))) {
            throw "Missing $path. Configure .env, then run: uv sync --project apps/api; npm --prefix apps/web ci"
        }
    }
    if ([int]((& node.exe --version) -replace '^v(\d+).*', '$1') -ne 24) {
        throw 'This project requires Node.js 24 (see apps/web/package.json).'
    }
    foreach ($port in @(8000, 5173)) {
        if (Test-LocalPort $port) { throw "Port $port is already in use. Stop its existing service before starting KnowFlow." }
    }
    Write-Host 'Environment checks passed.' -ForegroundColor Green
    if ($Check) {
        exit 0
    }

    New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
    try {
        $launcherLock = [System.IO.File]::Open((Join-Path $runDirectory 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    }
    catch { throw 'Another KnowFlow launcher is running. Use its window to stop it first.' }

    Write-Host 'Starting API (single worker)...'
    if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $projectRoot '.uv-cache' }
    # Local services and configured infrastructure endpoints bypass the system proxy.
    $localBypass = @($env:NO_PROXY -split ',') + @('127.0.0.1', 'localhost', '::1')
    foreach ($name in @('KNOWFLOW_MINIO_ENDPOINT', 'KNOWFLOW_ELASTICSEARCH_URL')) {
        $value = Get-Setting $name
        if ($value) { $localBypass += ([uri]$value).Host }
    }
    $env:NO_PROXY = ($localBypass | Where-Object { $_ } | Select-Object -Unique) -join ','
    $api = Start-ServiceProcess 'api' 'uv.exe' @('run', '--no-sync', '--project', 'apps/api', '--env-file', '.env',
        'uvicorn', 'knowflow.app.main:create_app', '--factory', '--host', '127.0.0.1', '--port', '8000')
    Wait-Service 'api' $api 'http://127.0.0.1:8000/api/auth/me'
    Write-Host 'Starting Web...'
    $web = Start-ServiceProcess 'web' 'node.exe' @('apps/web/node_modules/vite/bin/vite.js',
        'apps/web', '--host', '127.0.0.1', '--port', '5173', '--strictPort')
    Wait-Service 'web' $web 'http://127.0.0.1:5173/'
    Wait-Service 'api' $api 'http://127.0.0.1:5173/api/auth/me'
    Write-Host 'KnowFlow is ready: http://127.0.0.1:5173/' -ForegroundColor Green
    Write-Host 'Logs: .run/api.log, .run/api.error.log, .run/web.log, .run/web.error.log'
    if (-not $NoBrowser -and -not $SmokeTest) { Start-Process 'http://127.0.0.1:5173/' }
    if (-not $SmokeTest) {
        Wait-LauncherExit
    }
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $exitCode = 1
}
finally {
    # Stop process trees created by this invocation.
    for ($index = $startedProcesses.Count - 1; $index -ge 0; $index--) {
        $process = $startedProcesses[$index]
        try { Stop-OwnedProcessTree $process }
        catch { Write-Warning "Could not stop process $($process.Id): $($_.Exception.Message)" }
        $process.Dispose()
    }
    if ($null -ne $launcherLock) { $launcherLock.Dispose() }
    if ($startedProcesses.Count -gt 0) { Write-Host 'KnowFlow stopped.' }
}
exit $exitCode
