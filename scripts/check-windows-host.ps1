$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this check on the Windows laptop.' }
$computer = Get-CimInstance Win32_ComputerSystem
$processor = Get-CimInstance Win32_Processor | Select-Object -First 1
$system = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
    Windows = $system.Caption
    Version = $system.Version
    CPU = $processor.Name
    RAM_GB = [math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    VirtualizationFirmwareEnabled = $processor.VirtualizationFirmwareEnabled
    HypervisorPresent = $computer.HypervisorPresent
    Architecture = $env:PROCESSOR_ARCHITECTURE
} | Format-List
Write-Host 'This was a read-only check. Verify WHPX acceleration with the Android Emulator accel-check command after installing the SDK.'
