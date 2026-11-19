param(
    [string]$Python = (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$Python = (Resolve-Path -LiteralPath $Python).Path

& $Python -c "import struct, sys; sys.exit(0 if sys.platform == 'win32' and struct.calcsize('P') == 8 else 'Build requires Windows x64 Python')"
if ($LASTEXITCODE -ne 0) { throw 'Unsupported build interpreter' }

$version = & $Python -c "import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['project']['version'])" (Join-Path $root 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Could not read the application version' }

$previousPreference = $ErrorActionPreference
try {
    # Windows PowerShell treats redirected native stderr (including INFO logs) as error records.
    $ErrorActionPreference = 'Continue'
    & $Python -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $root 'dist') `
        --workpath (Join-Path $root 'build\pyinstaller') `
        (Join-Path $PSScriptRoot 'TimesheetAssistant.spec')
    $buildExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousPreference
}
if ($buildExitCode -ne 0) { throw 'PyInstaller failed; no distributable was published' }

$bundle = Join-Path $root 'dist\TimesheetAssistant'
& $Python (Join-Path $PSScriptRoot 'verify_bundle.py') $bundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed; no distributable was published' }

$archive = Join-Path $root "dist\TimesheetAssistant-$version-win-x64-unsigned.zip"
Compress-Archive -LiteralPath $bundle -DestinationPath $archive -Force
$hash = Get-FileHash -LiteralPath $archive -Algorithm SHA256
[IO.File]::WriteAllText("$archive.sha256", "$($hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($archive))`n")
Write-Output "Unsigned build: $archive"
Write-Output "SHA256: $($hash.Hash.ToLowerInvariant())"
