# Requires mkcert (install once with: winget install FiloSottile.mkcert).
# Creates a locally trusted wildcard certificate for the default lab domain,
# then enables it in Traefik without putting any certificate material in Git.

[CmdletBinding()]
param(
    [string]$Domain = "lab.localhost"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CertDirectory = Join-Path $ProjectRoot "configs\traefik\certs"
$DynamicDirectory = Join-Path $ProjectRoot "configs\traefik\dynamic"
$CertificatePath = Join-Path $CertDirectory "cert.pem"
$KeyPath = Join-Path $CertDirectory "key.pem"
$LocalConfigPath = Join-Path $DynamicDirectory "local-certificate.yml"

$Mkcert = Get-Command mkcert.exe -ErrorAction SilentlyContinue
if (-not $Mkcert) {
    $WingetPath = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $Mkcert = Get-ChildItem -Path $WingetPath -Filter mkcert.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
}
if (-not $Mkcert) {
    throw "mkcert is required. Install it with: winget install FiloSottile.mkcert. Then open a new PowerShell window and run this script again."
}

$MkcertPath = if ($Mkcert.Source) { $Mkcert.Source } else { $Mkcert.FullName }
New-Item -ItemType Directory -Force -Path $CertDirectory | Out-Null

& $MkcertPath -install
& $MkcertPath -cert-file $CertificatePath -key-file $KeyPath "*.$Domain" $Domain
if ($LASTEXITCODE -ne 0) { throw "mkcert could not create the lab certificate." }

@"
# Generated locally by scripts/enable-local-tls.ps1. Do not commit.
tls:
  certificates:
    - certFile: /etc/traefik/certs/cert.pem
      keyFile: /etc/traefik/certs/key.pem
"@ | Set-Content -Path $LocalConfigPath -Encoding utf8

Write-Host "Trusted certificate enabled for $Domain and *.$Domain." -ForegroundColor Green
Write-Host "Restart Traefik with: docker compose up -d --force-recreate traefik"
