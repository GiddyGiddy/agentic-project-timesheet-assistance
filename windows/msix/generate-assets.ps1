param([Parameter(Mandatory)][string]$Destination)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($asset in @(
    @{Name = 'Square44x44Logo.png'; Size = 44},
    @{Name = 'Square150x150Logo.png'; Size = 150},
    @{Name = 'StoreLogo.png'; Size = 50}
)) {
    $bitmap = New-Object System.Drawing.Bitmap($asset.Size, $asset.Size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $face = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 6)
    $hands = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(255, 190, 60), 6)
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::FromArgb(21, 43, 72))
        $graphics.ScaleTransform(($asset.Size / 100.0), ($asset.Size / 100.0))
        $graphics.DrawEllipse($face, 17, 17, 66, 66)
        $graphics.DrawLine($hands, 50, 50, 50, 29)
        $graphics.DrawLine($hands, 50, 50, 67, 59)
        $bitmap.Save((Join-Path $Destination $asset.Name), [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $hands.Dispose()
        $face.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}
