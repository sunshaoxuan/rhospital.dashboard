param(
    [string]$SshKey = "C:\workspace\Secure\sunsxaws.pem",
    [string]$RemoteUser = "ubuntu",
    [string]$RemoteHost = "160.16.91.200",
    [string]$RemoteAppDir = "/home/ubuntu/rhospital-statistics",
    [string]$ImageName = "hospital-ops-dashboard",
    [string]$ImageTag = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Checked([string]$Label, [scriptblock]$Command) {
    Write-Host ""
    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

git diff --quiet
if ($LASTEXITCODE -ne 0) {
    throw "working tree has unstaged or uncommitted changes. commit before release"
}
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    throw "index has staged changes. commit before release"
}

$shortSha = (git rev-parse --short=12 HEAD).Trim()
if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    $ImageTag = "$shortSha-$timestamp"
}

$releaseDir = Join-Path $root ".release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$imageTarName = "$ImageName-$ImageTag.tar"
$imageTarPath = Join-Path $releaseDir $imageTarName
$remote = "$RemoteUser@$RemoteHost"
$remoteReleaseDir = "$RemoteAppDir/releases/$ImageTag"

Invoke-Checked "compile python files" { python -m compileall -q app tests }
Invoke-Checked "run unit tests" { python -m unittest discover -s tests }
Invoke-Checked "build docker image" { docker build --pull -t "$ImageName`:$ImageTag" -t "$ImageName`:latest" . }
Invoke-Checked "save docker image" { docker save "$ImageName`:$ImageTag" "$ImageName`:latest" -o $imageTarPath }
Invoke-Checked "create remote release directory" { ssh -i $SshKey -o BatchMode=yes $remote "mkdir -p '$remoteReleaseDir'" }
Invoke-Checked "upload statistics release" {
    scp -i $SshKey -o BatchMode=yes $imageTarPath docker-compose.statistics.yml scripts/remote-update-statistics.sh "$remote`:$remoteReleaseDir/"
}
Invoke-Checked "activate statistics image" {
    ssh -i $SshKey -o BatchMode=yes $remote "chmod +x '$remoteReleaseDir/remote-update-statistics.sh' && '$remoteReleaseDir/remote-update-statistics.sh' '$RemoteAppDir' '$ImageName' '$ImageTag' '$remoteReleaseDir/$imageTarName' '$remoteReleaseDir/docker-compose.statistics.yml'"
}

Write-Host ""
Write-Host "Statistics release complete"
Write-Host "Image: $ImageName`:$ImageTag"
Write-Host "Remote: ${RemoteHost}:$RemoteAppDir"
