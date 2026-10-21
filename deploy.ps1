# Copy the project to the Pi and (re)start the service.
#   .\deploy.ps1                  copy code, restart wifi-meter if it is installed
#   .\deploy.ps1 -Install         copy code, then install polkit rule + systemd unit (prompts for sudo password)
#   .\deploy.ps1 -Target <host>   another Pi (an ssh host); the default comes from deploy.local.json
param(
    [switch]$Install,
    [string]$Target,
    [string]$RemoteDir = "pi_display"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Local settings stay out of git: copy deploy.local.example.json to deploy.local.json.
if (-not $Target) {
    if (-not (Test-Path deploy.local.json)) {
        throw "No -Target given and no deploy.local.json: copy deploy.local.example.json and set your Pi's ssh host"
    }
    $Target = (Get-Content deploy.local.json -Raw | ConvertFrom-Json).target
    if (-not $Target) { throw "deploy.local.json has no ""target""" }
}

$archive = Join-Path ([IO.Path]::GetTempPath()) "pi_display.tar"
tar --exclude=__pycache__ --exclude=*.so -cf $archive wifi_meter deploy tests pyproject.toml README.md
if ($LASTEXITCODE) { throw "tar failed" }

scp -q $archive "${Target}:/tmp/pi_display.tar"
if ($LASTEXITCODE) { throw "scp failed" }
Remove-Item $archive

ssh $Target "set -e; mkdir -p ~/$RemoteDir; rm -rf ~/$RemoteDir/wifi_meter; tar -xf /tmp/pi_display.tar -C ~/$RemoteDir; rm /tmp/pi_display.tar; find ~/$RemoteDir/deploy -type f \( -name '*.sh' -o -name '*.service' -o -name '*.rules' -o -name '*.dts' \) -exec sed -i 's/\r`$//' {} +; chmod +x ~/$RemoteDir/deploy/*.sh ~/$RemoteDir/deploy/*/*.sh"
if ($LASTEXITCODE) { throw "unpack failed" }
Write-Host "Copied to ${Target}:~/$RemoteDir"

if ($Install) {
    ssh -t $Target "sudo ~/$RemoteDir/deploy/install.sh"
} else {
    ssh $Target "if systemctl list-unit-files wifi-meter.service >/dev/null; then systemctl restart wifi-meter && echo 'wifi-meter restarted'; else echo 'service not installed yet: run .\deploy.ps1 -Install'; fi"
}
