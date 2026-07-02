param(
    [string]$action,
    [string]$val = ""
)

$NIRCMD = "C:\tools\nircmd.exe"

switch ($action) {
    "volume" {
        $target = [int]$val
        if ($target -lt 0) { $target = 0 }
        if ($target -gt 100) { $target = 100 }
        $level = [math]::Round($target * 65535 / 100)
        & $NIRCMD setsysvolume $level
        Write-Output "volume set to $target"
    }
    "mute" {
        & $NIRCMD mutesysvolume 1
        Write-Output "muted"
    }
    "unmute" {
        & $NIRCMD mutesysvolume 0
        Write-Output "unmuted"
    }
    "lock" {
        rundll32.exe user32.dll,LockWorkStation
        Write-Output "screen locked"
    }
    "sleep" {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.Application]::SetSuspendState("Suspend", $false, $false)
        Write-Output "sleeping"
    }
    "shutdown" {
        (Get-WmiObject -Class Win32_OperatingSystem).Win32Shutdown(1)
        Write-Output "shutting down"
    }
    "reboot" {
        (Get-WmiObject -Class Win32_OperatingSystem).Win32Shutdown(2)
        Write-Output "rebooting"
    }
    "mouse_disable" {
        schtasks /Create /TN "BlockInput" /TR "wscript.exe C:\tools\hide_on.vbs" /SC ONCE /ST 00:00 /F /IT /RL LIMITED 2>&1 | Out-Null
        schtasks /Run /TN "BlockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 800
        schtasks /Delete /TN "BlockInput" /F 2>&1 | Out-Null
        Write-Output "mouse disabled"
    }
    "mouse_enable" {
        schtasks /Create /TN "UnblockInput" /TR "wscript.exe C:\tools\hide_off.vbs" /SC ONCE /ST 00:00 /F /IT /RL LIMITED 2>&1 | Out-Null
        schtasks /Run /TN "UnblockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 800
        schtasks /Delete /TN "UnblockInput" /F 2>&1 | Out-Null
        Write-Output "mouse enabled"
    }
    "kb_disable" {
        schtasks /Create /TN "KBBlock" /TR "wscript.exe C:\tools\hide_on.vbs" /SC ONCE /ST 00:00 /F /IT /RL LIMITED 2>&1 | Out-Null
        schtasks /Run /TN "KBBlock" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 800
        schtasks /Delete /TN "KBBlock" /F 2>&1 | Out-Null
        Write-Output "keyboard disabled"
    }
    "kb_enable" {
        schtasks /Create /TN "KBUnblock" /TR "wscript.exe C:\tools\hide_off.vbs" /SC ONCE /ST 00:00 /F /IT /RL LIMITED 2>&1 | Out-Null
        schtasks /Run /TN "KBUnblock" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 800
        schtasks /Delete /TN "KBUnblock" /F 2>&1 | Out-Null
        Write-Output "keyboard enabled"
    }
    "bt_disable" {
        $svc = Get-Service | Where-Object { $_.DisplayName -like "*Bluetooth*" -and $_.Status -eq "Running" } | Select-Object -First 1
        if ($svc) {
            Stop-Service $svc.Name -Force 2>&1 | Out-Null
            Set-Service $svc.Name -StartupType Disabled 2>&1 | Out-Null
            Write-Output "bluetooth disabled: $($svc.DisplayName)"
        } else {
            $result = net stop bthserv 2>&1
            Write-Output "bluetooth service stopped (or already off)"
        }
    }
    "bt_enable" {
        $svc = Get-Service | Where-Object { $_.DisplayName -like "*Bluetooth*" } | Select-Object -First 1
        if ($svc) {
            Set-Service $svc.Name -StartupType Automatic 2>&1 | Out-Null
            Start-Service $svc.Name 2>&1 | Out-Null
            Write-Output "bluetooth enabled: $($svc.DisplayName)"
        } else {
            net start bthserv 2>&1 | Out-Null
            Write-Output "bluetooth service started"
        }
    }
    "printer_status" {
        $printers = Get-WmiObject Win32_Printer | Where-Object { $_.Name -notlike "*Microsoft*" -and $_.Name -notlike "*Fax*" }
        foreach ($p in $printers) {
            $status = switch ($p.PrinterStatus) { 1 {"Ready"} 2 {"Unknown"} 3 {"Idle"} 4 {"Printing"} 5 {"Warmup"} default {"Unknown"} }
            $offline = if ($p.WorkOffline) {"Offline"} else {"Online"}
            $jobs = (Get-WmiObject Win32_PrintJob | Where-Object { $_.PrinterName -eq $p.Name }).Count
            Write-Output "$($p.Name): $status, $offline, Jobs: $jobs"
        }
    }
    "printer_pause" {
        $printer = Get-WmiObject Win32_Printer | Where-Object { $_.Name -like "*Xprinter*" -or $_.Name -like "*POS*" } | Select-Object -First 1
        if ($printer) {
            $printer.Pause() | Out-Null
            Write-Output "printer paused: $($printer.Name)"
        } else {
            Write-Output "printer not found"
        }
    }
    "printer_resume" {
        $printer = Get-WmiObject Win32_Printer | Where-Object { $_.Name -like "*Xprinter*" -or $_.Name -like "*POS*" } | Select-Object -First 1
        if ($printer) {
            $printer.Resume() | Out-Null
            Write-Output "printer resumed: $($printer.Name)"
        } else {
            Write-Output "printer not found"
        }
    }
    "printer_clear" {
        $jobs = Get-WmiObject Win32_PrintJob | Where-Object { $_.PrinterName -like "*Xprinter*" -or $_.PrinterName -like "*POS*" }
        foreach ($job in $jobs) {
            $job.Delete() | Out-Null
        }
        Write-Output "print queue cleared"
    }
    "printer_test" {
        $printer = Get-WmiObject Win32_Printer | Where-Object { $_.Name -like "*Xprinter*" -or $_.Name -like "*POS*" } | Select-Object -First 1
        if ($printer) {
            $testText = "`n`n=== TEST PAGE ===`nTime: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`nPrinter: $($printer.Name)`nStatus: OK`n`n`n"
            $testText | Out-Printer -Name $printer.Name
            Write-Output "test page sent to $($printer.Name)"
        } else {
            Write-Output "printer not found"
        }
    }
    default {
        Write-Output "Unknown action: $action"
        Write-Output "Available: volume, mute, unmute, lock, sleep, shutdown, reboot, mouse_disable, mouse_enable, kb_disable, kb_enable, bt_disable, bt_enable, printer_status, printer_pause, printer_resume, printer_clear, printer_test"
    }
}
