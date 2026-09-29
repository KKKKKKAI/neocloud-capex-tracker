# Windows OpenSSH twin of admin_tunnel.sh: forward localhost:8081 to the
# server's admin panel, then browse http://localhost:8081.
#
#   .\scripts\admin_tunnel.ps1 [-HostAlias capex] [-Port 8081]
#
# Needs a `capex` entry in %USERPROFILE%\.ssh\config (see
# scripts/ssh_config.example) and the SSH key on the Windows side.
param(
    [string]$HostAlias = "capex",
    [int]$Port = 8081
)

$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Error "localhost:$Port is already in use (another tunnel still open?)"
    exit 1
}
Write-Host "admin panel: http://localhost:$Port   (Ctrl-C closes the tunnel)"
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L "${Port}:127.0.0.1:${Port}" $HostAlias
