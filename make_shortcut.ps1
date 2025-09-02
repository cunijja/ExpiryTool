$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Bat    = Join-Path $Here 'start_expirytool.bat'
$Desk   = [Environment]::GetFolderPath('Desktop')
$Lnk    = Join-Path $Desk 'ExpiryTool.lnk'

$Wsh = New-Object -ComObject WScript.Shell
$S   = $Wsh.CreateShortcut($Lnk)
$S.TargetPath  = $Bat
$S.WorkingDirectory = $Here
$S.IconLocation = 'shell32.dll,44'
$S.Save()
Write-Host "Shortcut created on Desktop."
