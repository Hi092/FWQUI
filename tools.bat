@echo off
if "%1"=="screenshot" goto screenshot
if "%1"=="brightness" goto brightness
if "%1"=="volume" goto volume
if "%1"=="mute" goto mute
if "%1"=="unmute" goto unmute
if "%1"=="disable-bt" goto disable_bt
if "%1"=="enable-bt" goto enable_bt
if "%1"=="disable-mouse" goto disable_mouse
if "%1"=="enable-mouse" goto enable_mouse
if "%1"=="disable-kb" goto disable_kb
if "%1"=="enable-kb" goto enable_kb
if "%1"=="lock" goto lock_screen
if "%1"=="sleep" goto sleep_pc
if "%1"=="shutdown" goto shutdown_pc
echo unknown command: %1
goto eof

:screenshot
powershell -command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; $bmp=New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); $g=[System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen(0,0,0,0,$bmp.Size); $bmp.Save('C:\tools\screenshot.png'); Write-Output 'screenshot saved to C:\tools\screenshot.png'"
goto eof

:brightness
powershell -command "$wmi=Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue; if($wmi){$wmi|%{$_.WmiSetBrightness(1,%2)}; Write-Output 'brightness set to %2'} else {Write-Output 'WMI brightness not supported on this machine'}"
goto eof

:volume
powershell -command "$vol=[int]('%2'*655.35); $obj=New-Object -ComObject WScript.Shell; 1..50|%{$obj.SendKeys([char]174)}; 1..([int]('%2'/2))|%{$obj.SendKeys([char]175)}; Write-Output 'volume set to %2%%'"
goto eof

:mute
powershell -command "$obj=New-Object -ComObject WScript.Shell; $obj.SendKeys([char]173); Write-Output 'muted'"
goto eof

:unmute
powershell -command "$obj=New-Object -ComObject WScript.Shell; $obj.SendKeys([char]173); Write-Output 'unmuted'"
goto eof

:disable_bt
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.Name -like '*Bluetooth*'}|ForEach-Object{$_.Disable()}; Write-Output 'bluetooth disabled'"
goto eof

:enable_bt
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.Name -like '*Bluetooth*'}|ForEach-Object{$_.Enable()}; Write-Output 'bluetooth enabled'"
goto eof

:disable_mouse
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.PNPClass -eq 'Mouse' -or $_.Name -like '*mouse*'}|ForEach-Object{$_.Disable(); Write-Output ('disabled: '+$_.Name)}; Write-Output 'mouse disabled'"
goto eof

:enable_mouse
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.PNPClass -eq 'Mouse' -or $_.Name -like '*mouse*'}|ForEach-Object{$_.Enable(); Write-Output ('enabled: '+$_.Name)}; Write-Output 'mouse enabled'"
goto eof

:disable_kb
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.PNPClass -eq 'Keyboard' -or $_.Name -like '*keyboard*'}|ForEach-Object{$_.Disable(); Write-Output ('disabled: '+$_.Name)}; Write-Output 'keyboard disabled'"
goto eof

:enable_kb
powershell -command "Get-WmiObject Win32_PnPEntity|Where-Object{$_.PNPClass -eq 'Keyboard' -or $_.Name -like '*keyboard*'}|ForEach-Object{$_.Enable(); Write-Output ('enabled: '+$_.Name)}; Write-Output 'keyboard enabled'"
goto eof

:lock_screen
rundll32.exe user32.dll,LockWorkStation
echo screen locked
goto eof

:sleep_pc
powershell -command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend',$false,$false); Write-Output 'sleeping'"
goto eof

:shutdown_pc
shutdown /s /t 5 /f
echo shutting down in 5 seconds
goto eof

:eof
