param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'),
    [ValidateRange(0, 65535)][int]$Revision = 0,
    [string]$ReleasePublisher = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$Python = (Resolve-Path -LiteralPath $Python).Path
. (Join-Path $PSScriptRoot 'sdk-tools.ps1')
$makeAppx = Find-WindowsSdkTool 'MakeAppx.exe'
$bundle = Join-Path $root 'dist\TimesheetAssistant'
& $Python (Join-Path $PSScriptRoot 'verify_bundle.py') $bundle
if ($LASTEXITCODE -ne 0) { throw 'Build a verified standalone bundle before creating an MSIX.' }
$version = & $Python -c "import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['project']['version'])" (Join-Path $root 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Could not read the application version.' }
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'MSIX builds require a three-part numeric project version.' }
$packageVersion = [version]"$version.$Revision"
if (@($packageVersion.Major, $packageVersion.Minor, $packageVersion.Build) | Where-Object { $_ -gt 65535 }) {
    throw 'Each MSIX version component must be at most 65535.'
}
$work = Join-Path $root ("build\msix-preview-" + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $work 'package'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    Get-ChildItem -LiteralPath $bundle -Force | Copy-Item -Destination $stage -Recurse
    & (Join-Path $PSScriptRoot 'msix\generate-assets.ps1') -Destination (Join-Path $stage 'Assets')
    [xml]$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'msix\AppxManifest.xml') -Raw
    $manifest.Package.Identity.SetAttribute('Version', $packageVersion.ToString())
    $packageName = 'TimesheetAssistantPreview'
    if ($ReleasePublisher) {
        $publisher = [Security.Cryptography.X509Certificates.X500DistinguishedName]::new($ReleasePublisher)
        if ($publisher.Name -eq 'CN=TimesheetAssistantPreview') {
            throw 'The trusted release must use a separate publisher from the pilot.'
        }
        $manifest.Package.Identity.SetAttribute('Name', 'GiddyGiddy.TimesheetAssistant')
        $manifest.Package.Identity.SetAttribute('Publisher', $ReleasePublisher)
        $manifest.Package.Properties.DisplayName = 'Timesheet Assistant'
        $manifest.Package.Properties.PublisherDisplayName = 'Timesheet Assistant'
        $namespaces = [System.Xml.XmlNamespaceManager]::new($manifest.NameTable)
        $namespaces.AddNamespace('f', 'http://schemas.microsoft.com/appx/manifest/foundation/windows10')
        $namespaces.AddNamespace('uap', 'http://schemas.microsoft.com/appx/manifest/uap/windows10')
        $visual = $manifest.SelectSingleNode('/f:Package/f:Applications/f:Application/uap:VisualElements', $namespaces)
        if ($null -eq $visual) { throw 'MSIX visual elements are missing.' }
        $visual.SetAttribute('DisplayName', 'Timesheet Assistant')
        $packageName = 'TimesheetAssistant'
    }
    $manifest.Save((Join-Path $stage 'AppxManifest.xml'))
    $temporary = Join-Path $work 'preview.msix'
    & $makeAppx pack /d $stage /p $temporary /o
    if ($LASTEXITCODE -ne 0) { throw 'MakeAppx validation/packaging failed.' }
    $package = Join-Path $root "dist\$packageName-$packageVersion-x64-unsigned.msix"
    Move-Item -LiteralPath $temporary -Destination $package -Force
    $hash = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$package.sha256", "$hash  $([IO.Path]::GetFileName($package))`n")
    Write-Output "Unsigned MSIX: $package"
    Write-Output 'Signing and explicit certificate trust are required before normal installation.'
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force
}
