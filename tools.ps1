param(
    [string]$action,
    [string]$val = ""
)

switch ($action) {
    "volume" {
        $target = [int]$val
        if ($target -lt 0) { $target = 0 }
        if ($target -gt 100) { $target = 100 }
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class KB {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, IntPtr dwExtraInfo);
}
"@
        $zi = [IntPtr]::Zero
        [KB]::keybd_event(0xAD, 0, 0, $zi)
        [KB]::keybd_event(0xAD, 0, 2, $zi)
        Start-Sleep -Milliseconds 50
        $steps = [math]::Round($target / 2)
        for ($i = 0; $i -lt $steps; $i++) {
            [KB]::keybd_event(0xAF, 0, 0, $zi)
            [KB]::keybd_event(0xAF, 0, 2, $zi)
            Start-Sleep -Milliseconds 10
        }
        Write-Output "volume set to $target"
    }
    "mute" {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class KBM {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, IntPtr dwExtraInfo);
}
"@
        $zi = [IntPtr]::Zero
        [KBM]::keybd_event(0xAD, 0, 0, $zi)
        [KBM]::keybd_event(0xAD, 0, 2, $zi)
        Write-Output "mute toggled"
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
        schtasks /Create /TN "BlockInput" /TR "powershell -ExecutionPolicy Bypass -File C:\tools\block_on.ps1" /SC ONCE /ST 00:00 /F /RL HIGHEST 2>&1 | Out-Null
        schtasks /Run /TN "BlockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        schtasks /Delete /TN "BlockInput" /F 2>&1 | Out-Null
        Write-Output "mouse disabled"
    }
    "mouse_enable" {
        schtasks /Create /TN "UnblockInput" /TR "powershell -ExecutionPolicy Bypass -File C:\tools\block_off.ps1" /SC ONCE /ST 00:00 /F /RL HIGHEST 2>&1 | Out-Null
        schtasks /Run /TN "UnblockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        schtasks /Delete /TN "UnblockInput" /F 2>&1 | Out-Null
        Write-Output "mouse enabled"
    }
    "kb_disable" {
        schtasks /Create /TN "BlockInput" /TR "powershell -ExecutionPolicy Bypass -File C:\tools\block_on.ps1" /SC ONCE /ST 00:00 /F /RL HIGHEST 2>&1 | Out-Null
        schtasks /Run /TN "BlockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        schtasks /Delete /TN "BlockInput" /F 2>&1 | Out-Null
        Write-Output "keyboard disabled"
    }
    "kb_enable" {
        schtasks /Create /TN "UnblockInput" /TR "powershell -ExecutionPolicy Bypass -File C:\tools\block_off.ps1" /SC ONCE /ST 00:00 /F /RL HIGHEST 2>&1 | Out-Null
        schtasks /Run /TN "UnblockInput" 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        schtasks /Delete /TN "UnblockInput" /F 2>&1 | Out-Null
        Write-Output "keyboard enabled"
    }
    "bt_disable" {
        # Stop bluetooth service
        $svc = Get-Service | Where-Object { $_.DisplayName -like "*Bluetooth*" -and $_.Status -eq "Running" } | Select-Object -First 1
        if ($svc) {
            Stop-Service $svc.Name -Force 2>&1 | Out-Null
            Set-Service $svc.Name -StartupType Disabled 2>&1 | Out-Null
            Write-Output "bluetooth disabled: $($svc.DisplayName)"
        } else {
            # Try net stop as fallback
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
        Write-Output "Available: volume, mute, lock, sleep, shutdown, reboot, mouse_disable, mouse_enable, kb_disable, kb_enable, bt_disable, bt_enable, printer_status, printer_pause, printer_resume, printer_clear, printer_test"
    }
}
