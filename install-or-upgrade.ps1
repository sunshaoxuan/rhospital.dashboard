Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-DetectedBindIp {
    $ip = $null
    try {
        $ip = Get-NetIPConfiguration |
            Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
            ForEach-Object { $_.IPv4Address.IPAddress } |
            Where-Object { $_ -notmatch '^(127\.|169\.254\.)' } |
            Select-Object -First 1
    } catch {
        $ip = $null
    }
    if (-not $ip) {
        $ip = [System.Net.Dns]::GetHostAddresses($env:COMPUTERNAME) |
            Where-Object { $_.AddressFamily -eq 'InterNetwork' -and $_.IPAddressToString -notmatch '^(127\.|169\.254\.)' } |
            Select-Object -First 1 |
            ForEach-Object { $_.IPAddressToString }
    }
    return $ip
}

function Test-RealIp([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $trimmed = $Value.Trim().ToLowerInvariant()
    if ($trimmed -in @("localhost", "0.0.0.0", "::", "::1")) { return $false }
    if ($trimmed -match '^127\.') { return $false }
    return $true
}

function Test-ConfiguredValue([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    if ($Value -match '<.*>') { return $false }
    if ($Value -in @("replace_me", "changeme")) { return $false }
    return $true
}

function Read-EnvFile([string]$Path) {
    $map = [ordered]@{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
                $map[$matches[1].Trim()] = $matches[2].Trim()
            }
        }
    }
    return $map
}

function Write-EnvFile([string]$Path, [hashtable]$Values) {
    @(
        "DASHBOARD_PUBLIC_IP=$($Values.DASHBOARD_PUBLIC_IP)"
        "DASHBOARD_PUBLIC_PORT=$($Values.DASHBOARD_PUBLIC_PORT)"
        "PROD_DB_URL=$($Values.PROD_DB_URL)"
        "PROD_DB_USERNAME=$($Values.PROD_DB_USERNAME)"
        "PROD_DB_PASSWORD=$($Values.PROD_DB_PASSWORD)"
        "OPS_DASHBOARD_TIME_ZONE=$($Values.OPS_DASHBOARD_TIME_ZONE)"
        "OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=$($Values.OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS)"
    ) | Set-Content -Path $Path -Encoding UTF8
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ((Test-Path ".git") -and $env:SKIP_GIT_PULL -ne "1") {
    git pull --ff-only
}

$envPath = Join-Path $root ".env"
$envValues = Read-EnvFile $envPath
$detectedIp = Get-DetectedBindIp

if (-not (Test-RealIp $detectedIp)) {
    Write-Host "无法自动检测可访问的真实 IPv4，请手工输入。"
    $detectedIp = ""
}

if (-not (Test-Path $envPath)) {
    Write-Host "首次安装，需要创建本机 .env。"
    $bindIp = Read-Host "看板访问 IP，不能是 localhost/127/0.0.0.0 [$detectedIp]"
    if ([string]::IsNullOrWhiteSpace($bindIp)) { $bindIp = $detectedIp }
    if (-not (Test-RealIp $bindIp)) { throw "DASHBOARD_PUBLIC_IP 必须是真实 IP，不能是 localhost/127/0.0.0.0" }

    $prodUrl = Read-Host "生产只读 PostgreSQL URL，例如 postgresql://1.2.3.4:5432/hospital"
    $prodUser = Read-Host "生产只读数据库用户名"
    $securePassword = Read-Host "生产只读数据库密码" -AsSecureString
    $prodPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    )

    $envValues = [ordered]@{
        DASHBOARD_PUBLIC_IP = $bindIp.Trim()
        DASHBOARD_PUBLIC_PORT = "18091"
        PROD_DB_URL = $prodUrl.Trim()
        PROD_DB_USERNAME = $prodUser.Trim()
        PROD_DB_PASSWORD = $prodPassword
        OPS_DASHBOARD_TIME_ZONE = "Asia/Tokyo"
        OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS = "10"
    }
    Write-EnvFile $envPath $envValues
} else {
    if (-not $envValues.Contains("DASHBOARD_PUBLIC_IP") -or -not (Test-RealIp $envValues.DASHBOARD_PUBLIC_IP)) {
        $bindIp = Read-Host "DASHBOARD_PUBLIC_IP 缺失或无效，请输入真实访问 IP [$detectedIp]"
        if ([string]::IsNullOrWhiteSpace($bindIp)) { $bindIp = $detectedIp }
        if (-not (Test-RealIp $bindIp)) { throw "DASHBOARD_PUBLIC_IP 必须是真实 IP，不能是 localhost/127/0.0.0.0" }
        $envValues.DASHBOARD_PUBLIC_IP = $bindIp.Trim()
    }
    if (-not $envValues.Contains("PROD_DB_URL") -or -not (Test-ConfiguredValue $envValues.PROD_DB_URL)) {
        $envValues.PROD_DB_URL = (Read-Host "生产只读 PostgreSQL URL，例如 postgresql://1.2.3.4:5432/hospital").Trim()
    }
    if (-not $envValues.Contains("PROD_DB_USERNAME") -or -not (Test-ConfiguredValue $envValues.PROD_DB_USERNAME)) {
        $envValues.PROD_DB_USERNAME = (Read-Host "生产只读数据库用户名").Trim()
    }
    if (-not $envValues.Contains("PROD_DB_PASSWORD") -or -not (Test-ConfiguredValue $envValues.PROD_DB_PASSWORD)) {
        $securePassword = Read-Host "生产只读数据库密码" -AsSecureString
        $envValues.PROD_DB_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        )
    }
    if (-not $envValues.Contains("OPS_DASHBOARD_TIME_ZONE")) { $envValues.OPS_DASHBOARD_TIME_ZONE = "Asia/Tokyo" }
    if (-not $envValues.Contains("OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS")) { $envValues.OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS = "10" }
    if (-not $envValues.Contains("DASHBOARD_PUBLIC_PORT") -or -not (Test-ConfiguredValue $envValues.DASHBOARD_PUBLIC_PORT)) {
        $envValues.DASHBOARD_PUBLIC_PORT = "18091"
    }
    Write-EnvFile $envPath $envValues
}

docker compose up -d --build

Write-Host ""
Write-Host "本地运营看板已启动或升级:"
Write-Host "http://$($envValues.DASHBOARD_PUBLIC_IP):$($envValues.DASHBOARD_PUBLIC_PORT)/"
Write-Host ""
docker compose ps
