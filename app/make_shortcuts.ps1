# Creates the clickable desktop apps. Re-run this if you move the project
# folder, rename the apps, or regenerate the icons.
# Run it from the 'Hollow Floor' folder:
#   powershell -ExecutionPolicy Bypass -File app\make_shortcuts.ps1

$ErrorActionPreference = 'Stop'

$app     = $PSScriptRoot
$root    = Split-Path $app -Parent

# Find the Python that has Ursina installed: newest first, plain `python` last
# (on many PCs that is only the Microsoft Store stub, with no packages).
function Find-GamePython {
    $candidates = @(@('py', '-3.14'), @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'),
                    @('py', '-3.10'), @('py', '-3'), @('python'))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @extra -c "import importlib.util, sys; print(sys.executable if importlib.util.find_spec('ursina') else '')" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    }
    return $null
}

$python = Find-GamePython
if (-not $python) {
    throw "Could not find a Python with Ursina installed. From the game folder run:  py -m pip install -r requirements.txt"
}
# pythonw.exe sits next to python.exe and starts without a console window
$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "using Python: $pythonw"

$desktop = [Environment]::GetFolderPath('Desktop')
$start   = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

$apps = @(
    @{ Name = 'Hollow Floor';        Script = 'HollowFloor.pyw'; Icon = 'HollowFloor.ico';
       Desc = 'Hollow Floor - infinite first-person explorer' },
    @{ Name = 'Horror Toggle Board'; Script = 'ToggleBoard.pyw'; Icon = 'ToggleBoard.ico';
       Desc = 'Horror effects test board' }
)

$shell = New-Object -ComObject WScript.Shell
foreach ($a in $apps) {
    foreach ($dir in @($desktop, $start)) {
        $lnk = Join-Path $dir ($a.Name + '.lnk')
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath       = $pythonw
        $s.Arguments        = '"' + (Join-Path $app $a.Script) + '"'
        $s.WorkingDirectory = $root
        $s.IconLocation     = (Join-Path $app $a.Icon) + ',0'
        $s.Description      = $a.Desc
        $s.WindowStyle      = 1
        $s.Save()
        Write-Host "created: $lnk"
    }
}
