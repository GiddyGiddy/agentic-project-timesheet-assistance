param(
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$CertificateThumbprint
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'sdk-tools.ps1')
$signTool = Find-WindowsSdkTool 'SignTool.exe'
$Package = (Resolve-Path -LiteralPath $Package).Path
if (-not $Package.EndsWith('-unsigned.msix', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Select an unsigned Preview MSIX produced by build-msix.ps1.'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($Package)
try {
    $entry = $archive.GetEntry('AppxManifest.xml')
    if ($null -eq $entry) { throw 'Package manifest is missing.' }
    $stream = $entry.Open()
    try {
        $manifest = New-Object System.Xml.XmlDocument
        $manifest.XmlResolver = $null
        $manifest.Load($stream)
    }
    finally { $stream.Dispose() }
    if ($manifest.Package.Identity.Name -ne 'GiddyGiddy.TimesheetAssistant.Preview' -or
        $manifest.Package.Identity.Publisher -ne 'CN=TimesheetAssistantPreview' -or
        $null -ne $archive.GetEntry('AppxSignature.p7x')) {
        throw 'Only the unsigned Preview identity can be signed with this pilot helper.'
    }
}
finally { $archive.Dispose() }
$certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$CertificateThumbprint"
$usages = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
    ForEach-Object { $_.EnhancedKeyUsages } | ForEach-Object { $_.Value })
if (-not $certificate.HasPrivateKey -or $certificate.Subject -ne 'CN=TimesheetAssistantPreview' -or
    $certificate.NotBefore -gt (Get-Date) -or $certificate.NotAfter -le (Get-Date) -or
    '1.3.6.1.5.5.7.3.3' -notin $usages) {
    throw 'Choose a currently valid Preview code-signing certificate with a private key in CurrentUser\My.'
}
$signed = $Package.Substring(0, $Package.Length - '-unsigned.msix'.Length) + '-pilot-signed.msix'
if (Test-Path -LiteralPath $signed) { throw 'The signed output already exists; choose a fresh package version.' }
Copy-Item -LiteralPath $Package -Destination $signed
try {
    & $signTool sign /fd SHA256 /s My /sha1 $CertificateThumbprint $signed
    if ($LASTEXITCODE -ne 0) { throw 'Pilot signing failed.' }
    Export-Certificate -Cert $certificate -FilePath "$signed.cer" -Type CERT | Out-Null
    $hash = (Get-FileHash -LiteralPath $signed -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$signed.sha256", "$hash  $([IO.Path]::GetFileName($signed))`n")
}
catch {
    Remove-Item -LiteralPath $signed -Force
    throw
}
Write-Output "Pilot-signed MSIX: $signed"
Write-Output "Public certificate: $signed.cer"
Write-Output 'This does not establish Windows trust. An administrator must review/trust the public certificate before installation.'
Write-Output 'No trusted certificate stores were modified. This untimestamped pilot package is not a production release.'
