$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SonyFolder = Join-Path $ProjectRoot 'resources\luts\sony'
$LogFolder = Join-Path $ProjectRoot 'logs'
$LogFile = Join-Path $LogFolder 'sony_lut_install.log'
$SonySupportPage = 'https://www.sony.co.in/electronics/support/software/00263050'

New-Item -ItemType Directory -Force -Path $SonyFolder | Out-Null
New-Item -ItemType Directory -Force -Path $LogFolder | Out-Null

function Write-InstallLog([string]$Message) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

Write-Host ''
Write-Host '============================================================'
Write-Host ' CorridorKey - Sony S-Log LUT installer'
Write-Host '============================================================'
Write-Host ('Destination: {0}' -f $SonyFolder)
Write-Host ''
Write-InstallLog 'Starting Sony LUT ZIP/CUBE import.'

try {
    Start-Process $SonySupportPage
} catch {
    Write-InstallLog ('Could not open Sony support page automatically: ' + $_.Exception.Message)
}

Write-Host 'Sony official download page opened in your browser.'
Write-Host 'Download the two Look Profile LUT ZIP archives:'
Write-Host '  1. Look Profile for S-Gamut / S-Log2'
Write-Host '  2. Look Profile for S-Gamut3.Cine / S-Log3'
Write-Host ''
Write-Host 'When the downloads finish, select both ZIP files in the next dialog.'
Write-Host ''

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.IO.Compression.FileSystem
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Select the downloaded Sony LUT ZIP archives or .cube files'
$dialog.Filter = 'Sony LUT ZIP/CUBE files (*.zip;*.cube)|*.zip;*.cube|ZIP archives (*.zip)|*.zip|CUBE LUT files (*.cube)|*.cube|All files (*.*)|*.*'
$dialog.Multiselect = $true
$downloads = Join-Path $env:USERPROFILE 'Downloads'
if (Test-Path -LiteralPath $downloads) { $dialog.InitialDirectory = $downloads }

$copied = 0
$skipped = 0
$selected = $dialog.ShowDialog()
if ($selected -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-InstallLog 'User cancelled the file selection. No LUTs were imported.'
    [System.Windows.Forms.MessageBox]::Show(
        'No files were selected. Download the Sony LUT ZIP archives and run the installer again.',
        'CorridorKey Sony LUT installer', 'OK', 'Information') | Out-Null
    $dialog.Dispose()
    Read-Host 'Press Enter to close'
    exit 0
}

foreach ($file in $dialog.FileNames) {
    $extension = [System.IO.Path]::GetExtension($file).ToLowerInvariant()
    if ($extension -eq '.cube') {
        $baseName = [System.IO.Path]::GetFileName($file)
        $target = Join-Path $SonyFolder $baseName
        $n = 2
        while (Test-Path -LiteralPath $target) {
            $target = Join-Path $SonyFolder ('{0}_{1}{2}' -f [System.IO.Path]::GetFileNameWithoutExtension($baseName), $n, '.cube')
            $n++
        }
        Copy-Item -LiteralPath $file -Destination $target
        $copied++
        Write-InstallLog ('Imported CUBE: ' + [System.IO.Path]::GetFileName($target))
    }
    elseif ($extension -eq '.zip') {
        $archive = $null
        try {
            $archive = [System.IO.Compression.ZipFile]::OpenRead($file)
            $foundInArchive = 0
            foreach ($entry in $archive.Entries) {
                if ([System.IO.Path]::GetExtension($entry.FullName) -ine '.cube') { continue }
                if ([string]::IsNullOrWhiteSpace($entry.Name)) { continue }
                $baseName = [System.IO.Path]::GetFileName($entry.FullName)
                $target = Join-Path $SonyFolder $baseName
                $n = 2
                while (Test-Path -LiteralPath $target) {
                    $target = Join-Path $SonyFolder ('{0}_{1}{2}' -f [System.IO.Path]::GetFileNameWithoutExtension($baseName), $n, '.cube')
                    $n++
                }
                $inputStream = $entry.Open()
                $outputStream = [System.IO.File]::Create($target)
                try { $inputStream.CopyTo($outputStream) }
                finally { $outputStream.Dispose(); $inputStream.Dispose() }
                $copied++
                $foundInArchive++
                Write-InstallLog ('Extracted CUBE: ' + [System.IO.Path]::GetFileName($target))
            }
            if ($foundInArchive -eq 0) {
                $skipped++
                Write-InstallLog ('No .cube entries found in archive: ' + [System.IO.Path]::GetFileName($file))
            }
        }
        catch {
            $skipped++
            Write-InstallLog ('Could not read archive ' + [System.IO.Path]::GetFileName($file) + ': ' + $_.Exception.Message)
        }
        finally {
            if ($null -ne $archive) { $archive.Dispose() }
        }
    }
    else {
        $skipped++
        Write-InstallLog ('Unsupported file type skipped: ' + $file)
    }
}
$dialog.Dispose()

Write-Host ''
Write-InstallLog ('Finished. Imported {0} CUBE file(s); skipped {1} file(s).' -f $copied, $skipped)
Write-Host ('Sony LUT folder: {0}' -f $SonyFolder)
Write-Host ('Install log: {0}' -f $LogFile)
if ($copied -gt 0) {
    [System.Windows.Forms.MessageBox]::Show(
        ('Imported {0} Sony .cube LUT file(s). Return to CorridorKey Preferences and click Refresh.' -f $copied),
        'CorridorKey Sony LUT installer', 'OK', 'Information') | Out-Null
} else {
    [System.Windows.Forms.MessageBox]::Show(
        'No .cube files were imported. Make sure you selected the Sony Look Profile ZIP archives, not the PDF summaries.',
        'CorridorKey Sony LUT installer', 'OK', 'Warning') | Out-Null
}
Read-Host 'Press Enter to close'
