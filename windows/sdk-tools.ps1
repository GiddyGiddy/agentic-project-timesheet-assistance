function Find-WindowsSdkTool {
    param([Parameter(Mandatory)][ValidateSet('MakeAppx.exe', 'SignTool.exe')][string]$Name)

    $bin = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path -LiteralPath $bin) {
        $versions = Get-ChildItem -LiteralPath $bin -Directory |
            Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
            Sort-Object { [version]$_.Name } -Descending
        foreach ($version in $versions) {
            $tool = Join-Path $version.FullName "x64\$Name"
            if (Test-Path -LiteralPath $tool -PathType Leaf) { return $tool }
        }
    }
    throw "Install the Windows 10/11 SDK with x64 $Name before packaging/signing."
}
