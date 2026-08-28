[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CertificatePath,

    [switch]$LocalMachine,
    [switch]$CheckOnly,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$resolvedPath = (Resolve-Path -LiteralPath $CertificatePath).Path
$certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($resolvedPath)

$basicConstraints = $certificate.Extensions | Where-Object {
    $_ -is [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]
}
if (-not $basicConstraints -or -not $basicConstraints.CertificateAuthority) {
    throw "The selected certificate is not a CA certificate: $resolvedPath"
}

$storeLocation = if ($LocalMachine) {
    [System.Security.Cryptography.X509Certificates.StoreLocation]::LocalMachine
} else {
    [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
}

Write-Host "Certificate: $($certificate.Subject)"
Write-Host "Thumbprint:  $($certificate.Thumbprint)"
Write-Host "Store:       $storeLocation\Root"

if ($CheckOnly) {
    Write-Host "Certificate validation passed; the trust store was not modified."
    $certificate.Dispose()
    exit 0
}

if (-not $Force) {
    $answer = Read-Host "Trust this LAN CA for hand Web HTTPS? Type YES to continue"
    if ($answer -cne "YES") {
        Write-Host "Cancelled."
        exit 1
    }
}

$store = [System.Security.Cryptography.X509Certificates.X509Store]::new("Root", $storeLocation)
try {
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
} catch {
    $hint = if ($LocalMachine) {
        "Run PowerShell as Administrator for a LocalMachine installation."
    } else {
        "Ask the system administrator to permit CurrentUser root certificate installation."
    }
    throw "Cannot open $storeLocation\Root for writing. $hint $($_.Exception.Message)"
}
try {
    $existing = $store.Certificates.Find(
        [System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,
        $certificate.Thumbprint,
        $false
    )
    if ($existing.Count -eq 0) {
        $store.Add($certificate)
        Write-Host "Certificate installed. Restart the browser before opening hand Web."
    } else {
        Write-Host "Certificate is already installed."
    }
} finally {
    $store.Close()
    $certificate.Dispose()
}
