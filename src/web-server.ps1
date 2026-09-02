[CmdletBinding()]
param(
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 8770,
    [string]$RuntimeRoot,
    [string]$WebRoot
)

$ErrorActionPreference = 'Stop'
$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) { $RuntimeRoot = Join-Path $script:ProjectRoot 'runtime' }
if ([string]::IsNullOrWhiteSpace($WebRoot)) { $WebRoot = Join-Path $script:ProjectRoot 'web' }
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$WebRoot = [System.IO.Path]::GetFullPath($WebRoot)
$script:Utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.Directory]::CreateDirectory($RuntimeRoot) | Out-Null

function Resolve-ListenIp {
    param([string]$Value)
    if ($Value -eq 'localhost') { return [System.Net.IPAddress]::Loopback }
    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($Value, [ref]$parsed)) { return $parsed }
    $addresses = [System.Net.Dns]::GetHostAddresses($Value)
    $address = @($addresses | Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } | Select-Object -First 1)[0]
    if ($null -eq $address) { throw "No IPv4 address resolved for $Value" }
    return $address
}
function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $tmp = $Path + '.tmp'
    [System.IO.File]::WriteAllText($tmp, ($Value | ConvertTo-Json -Depth 8), $script:Utf8)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    [System.IO.File]::Move($tmp, $Path)
}

function Read-JsonFile {
    param([string]$Path, [object]$Fallback)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $Fallback }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $Fallback }
}

function Read-RecentJsonLines {
    param([string]$Path, [int]$Limit)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    $safeLimit = [math]::Max(1, [math]::Min(100, $Limit))
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8 | Select-Object -Last $safeLimit)
    $items = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try { $items += ($line | ConvertFrom-Json) } catch { }
    }
    return @($items)
}
function Get-MonitorPayload {
    $path = Join-Path $RuntimeRoot 'monitor.json'
    $raw = Read-JsonFile -Path $path -Fallback ([pscustomobject]@{ state='not_started' })
    $pidValue = 0
    $alive = $false
    if ($raw.PSObject.Properties.Name -contains 'pid') {
        $alive = [int]::TryParse([string]$raw.pid, [ref]$pidValue) -and
                 ($null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue))
    }
    $age = $null
    if ($raw.PSObject.Properties.Name -contains 'last_tick_at' -and $raw.last_tick_at) {
        try { $age = [math]::Max(0, [math]::Round(((Get-Date).ToUniversalTime() - ([datetimeoffset]::Parse([string]$raw.last_tick_at)).UtcDateTime).TotalSeconds, 1)) } catch { }
    }
    $interval = if ($raw.PSObject.Properties.Name -contains 'interval_seconds') { [int]$raw.interval_seconds } else { 60 }
    $stale = (-not $alive) -or ($null -eq $age) -or ($age -gt ($interval * 2.5))
    return [ordered]@{
        state = [string]$raw.state
        pid = $pidValue
        process_alive = $alive
        interval_seconds = $interval
        started_at = if ($raw.PSObject.Properties.Name -contains 'started_at') { $raw.started_at } else { $null }
        last_tick_at = if ($raw.PSObject.Properties.Name -contains 'last_tick_at') { $raw.last_tick_at } else { $null }
        heartbeat_age_seconds = $age
        stale = $stale
        last_error = if ($raw.PSObject.Properties.Name -contains 'last_error') { $raw.last_error } else { $null }
    }
}
function Get-QueryLimit {
    param([string]$Query, [int]$Default = 20)
    if ([string]::IsNullOrWhiteSpace($Query)) { return $Default }
    foreach ($pair in $Query.TrimStart('?').Split('&')) {
        $parts = $pair.Split('=', 2)
        if ($parts.Count -eq 2 -and $parts[0] -eq 'limit') {
            $value = 0
            if ([int]::TryParse($parts[1], [ref]$value)) { return [math]::Max(1, [math]::Min(100, $value)) }
        }
    }
    return $Default
}

function New-JsonBytes {
    param([object]$Value)
    return $script:Utf8.GetBytes(($Value | ConvertTo-Json -Depth 10 -Compress))
}

function Get-StaticBytes {
    param([string]$Name)
    $allowed = @{
        '/' = @('index.html','text/html; charset=utf-8')
        '/app.js' = @('app.js','application/javascript; charset=utf-8')
        '/style.css' = @('style.css','text/css; charset=utf-8')
    }
    if (-not $allowed.ContainsKey($Name)) { return $null }
    $entry = $allowed[$Name]
    $path = Join-Path $WebRoot $entry[0]
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    return [pscustomobject]@{ Bytes=[System.IO.File]::ReadAllBytes($path); ContentType=$entry[1] }
}
function Write-HttpResponse {
    param(
        [System.Net.Sockets.NetworkStream]$Stream,
        [int]$StatusCode,
        [string]$Reason,
        [string]$ContentType,
        [byte[]]$Body,
        [bool]$HeadOnly = $false,
        [hashtable]$ExtraHeaders = @{}
    )
    if ($null -eq $Body) { $Body = [byte[]]@() }
    $headers = "HTTP/1.1 $StatusCode $Reason`r`nContent-Type: $ContentType`r`nContent-Length: $($Body.Length)`r`nConnection: close`r`nCache-Control: no-store`r`nX-Content-Type-Options: nosniff`r`n"
    foreach ($key in $ExtraHeaders.Keys) { $headers += "$key`: $($ExtraHeaders[$key])`r`n" }
    $headers += "`r`n"
    $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($headers)
    $Stream.Write($headerBytes, 0, $headerBytes.Length)
    if (-not $HeadOnly -and $Body.Length -gt 0) { $Stream.Write($Body, 0, $Body.Length) }
    $Stream.Flush()
}

function Write-ErrorResponse {
    param($Stream, [int]$Code, [string]$Reason, [string]$Message, [bool]$HeadOnly=$false, [hashtable]$Headers=@{})
    $body = New-JsonBytes ([ordered]@{ error=$Reason; message=$Message })
    Write-HttpResponse -Stream $Stream -StatusCode $Code -Reason $Reason -ContentType 'application/json; charset=utf-8' -Body $body -HeadOnly $HeadOnly -ExtraHeaders $Headers
}
function Handle-Client {
    param([System.Net.Sockets.TcpClient]$Client)
    $stream = $Client.GetStream()
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII, $false, 4096, $true)
    $requestLine = $reader.ReadLine()
    if ([string]::IsNullOrWhiteSpace($requestLine) -or $requestLine.Length -gt 8192) {
        Write-ErrorResponse $stream 400 'Bad Request' 'invalid request line'
        return
    }
    $parts = $requestLine.Split(' ')
    if ($parts.Count -ne 3) { Write-ErrorResponse $stream 400 'Bad Request' 'invalid request line'; return }
    $method = $parts[0].ToUpperInvariant()
    $target = $parts[1]
    $rawPath = [System.Uri]::UnescapeDataString($target.Split('?', 2)[0])
    if ($rawPath.Contains('..') -or $rawPath.Contains('\')) {
        Write-ErrorResponse $stream 400 'Bad Request' 'path traversal is not allowed'
        return
    }
    for ($i=0; $i -lt 100; $i++) {
        $line = $reader.ReadLine()
        if ($null -eq $line -or $line -eq '') { break }
        if ($line.Length -gt 8192) { Write-ErrorResponse $stream 431 'Request Header Fields Too Large' 'header too large'; return }
    }
    $headOnly = ($method -eq 'HEAD')
    if ($method -notin @('GET','HEAD')) {
        Write-ErrorResponse $stream 405 'Method Not Allowed' 'read-only dashboard supports GET and HEAD only' $headOnly @{ Allow='GET, HEAD' }
        return
    }
    $uri = $null
    if (-not [System.Uri]::TryCreate(('http://localhost' + $target), [System.UriKind]::Absolute, [ref]$uri)) {
        Write-ErrorResponse $stream 400 'Bad Request' 'invalid request target' $headOnly
        return
    }
    $path = [System.Uri]::UnescapeDataString($uri.AbsolutePath)
    if ($path.Contains('..') -or $path.Contains('\')) {
        Write-ErrorResponse $stream 400 'Bad Request' 'path traversal is not allowed' $headOnly
        return
    }
    $static = Get-StaticBytes -Name $path
    if ($null -ne $static) {
        Write-HttpResponse $stream 200 'OK' $static.ContentType $static.Bytes $headOnly
        return
    }
    $payload = $null
    if ($path -eq '/api/monitor') {
        $payload = Get-MonitorPayload
    } elseif ($path -eq '/api/summary') {
        $payload = Read-JsonFile (Join-Path $RuntimeRoot 'summary.json') ([ordered]@{ observed_at=$null; project_count=0; projects=@() })
    } elseif ($path -match '^/api/projects/([A-Za-z0-9_-]+)$') {
        $projectPath = Join-Path (Join-Path $RuntimeRoot 'projects') ($Matches[1] + '.json')
        if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) { Write-ErrorResponse $stream 404 'Not Found' 'project snapshot not found' $headOnly; return }
        $payload = Read-JsonFile $projectPath ([ordered]@{})
    } elseif ($path -eq '/api/events') {
        $limit = Get-QueryLimit -Query $uri.Query
        $payload = [ordered]@{ items = @(Read-RecentJsonLines (Join-Path $RuntimeRoot 'history\events.jsonl') $limit) }
    } elseif ($path -eq '/api/runs') {
        $limit = Get-QueryLimit -Query $uri.Query
        $payload = [ordered]@{ items = @(Read-RecentJsonLines (Join-Path $RuntimeRoot 'history\runs.jsonl') $limit) }
    } else {
        Write-ErrorResponse $stream 404 'Not Found' 'route not found' $headOnly
        return
    }
    Write-HttpResponse $stream 200 'OK' 'application/json; charset=utf-8' (New-JsonBytes $payload) $headOnly
}
$listenIp = Resolve-ListenIp $ListenAddress
$listener = New-Object System.Net.Sockets.TcpListener($listenIp, $Port)
$pidPath = Join-Path $RuntimeRoot 'web.pid'
$heartbeatPath = Join-Path $RuntimeRoot 'web.json'
$startedAt = (Get-Date).ToUniversalTime().ToString('o')
$listener.Start()
[System.IO.File]::WriteAllText($pidPath, [string]$PID, $script:Utf8)

function Write-WebHeartbeat {
    param([AllowNull()][string]$LastRequestAt)
    $value = [ordered]@{
        state = 'running'
        pid = $PID
        listen_address = $ListenAddress
        port = $Port
        started_at = $startedAt
        last_request_at = $LastRequestAt
    }
    Write-JsonAtomic -Path $heartbeatPath -Value $value
}

Write-WebHeartbeat $null
try {
    while ($true) {
        $client = $listener.AcceptTcpClient()
        try {
            Handle-Client -Client $client
            Write-WebHeartbeat ((Get-Date).ToUniversalTime().ToString('o'))
        } catch {
            try { Write-ErrorResponse $client.GetStream() 500 'Internal Server Error' 'request failed' } catch { }
        } finally {
            $client.Close()
        }
    }
} finally {
    $listener.Stop()
    if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath -Force }
}
