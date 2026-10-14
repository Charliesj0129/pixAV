param([ValidateSet('capture', 'observe', 'halt')][string] $Mode = 'observe')

# JSON arrives on stdin. Never accept a command line or credentials in it.
$ErrorActionPreference = 'Stop'
$pixavInput = [Console]::In.ReadToEnd()
if ($pixavInput.Length -gt 16384) { throw 'NATIVE_CONTROL_INPUT_LIMIT' }
$pixavSpec = $pixavInput | ConvertFrom-Json
if ($pixavSpec.schema -ne 'windows-native-control-v1') { throw 'NATIVE_CONTROL_SCHEMA_MISMATCH' }
if ($pixavSpec.serial -notmatch '^emulator-([0-9]{4,5})$') { throw 'NATIVE_SERIAL_INVALID' }
$pixavPort = [int]$matches[1]
if ($pixavPort -lt 5554 -or $pixavPort -gt 5682 -or $pixavPort % 2 -ne 0) { throw 'NATIVE_SERIAL_INVALID' }
if ($pixavSpec.avd_name -notmatch '^[A-Za-z0-9._-]+$') { throw 'NATIVE_AVD_NAME_INVALID' }
$pixavSdk = [IO.Path]::GetFullPath($pixavSpec.sdk_root).TrimEnd('\')
$pixavAdb = Join-Path $pixavSdk 'platform-tools\adb.exe'
$pixavEmulator = Join-Path $pixavSdk 'emulator\emulator.exe'
$pixavQemu = Join-Path $pixavSdk 'emulator\qemu\windows-x86_64\qemu-system-x86_64-headless.exe'

# GetProcessTimes and TerminateProcess use the SAME opened kernel handle. A
# recycled PID cannot turn verification of an old process into killing a new one.
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class PixavNativeProcess {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool GetProcessTimes(IntPtr process, out long created, out long exited, out long kernel, out long user);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint WaitForSingleObject(IntPtr process, uint milliseconds);
    [DllImport("kernel32.dll")]
    public static extern bool CloseHandle(IntPtr process);
}
'@

$pixavHandles = @{}
$pixavRecords = @{}
function Open-PixavProcess([string] $Role, [string] $ExpectedPath) {
    $pixavEntry = $pixavSpec.processes.$Role
    if (!$pixavEntry -or [long]$pixavEntry.pid -le 0) { throw 'NATIVE_PROCESS_BINDING_MISSING' }
    $pixavAccess = [uint32](0x1000 -bor 0x100000)
    if ($Mode -eq 'halt' -and $Role -ne 'adb') { $pixavAccess = $pixavAccess -bor 1 }
    $pixavHandle = [PixavNativeProcess]::OpenProcess($pixavAccess, $false, [uint32]$pixavEntry.pid)
    if ($pixavHandle -eq [IntPtr]::Zero) {
        $pixavWinError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($Mode -eq 'halt' -and $Role -ne 'adb' -and $pixavWinError -eq 87) {
            $pixavRecords[$Role] = @{pid=$pixavEntry.pid; original_process_absent=$true}
            return
        }
        throw 'NATIVE_PROCESS_OPEN_REJECTED'
    }
    $pixavHandles[$Role] = $pixavHandle
    [long]$pixavCreated=0; [long]$pixavExited=0; [long]$pixavKernel=0; [long]$pixavUser=0
    if (![PixavNativeProcess]::GetProcessTimes($pixavHandle, [ref]$pixavCreated, [ref]$pixavExited, [ref]$pixavKernel, [ref]$pixavUser)) { throw 'NATIVE_PROCESS_TIME_UNAVAILABLE' }
    $pixavPath = New-Object Text.StringBuilder 32768
    [uint32]$pixavLength=32768
    if (![PixavNativeProcess]::QueryFullProcessImageName($pixavHandle, 0, $pixavPath, [ref]$pixavLength)) { throw 'NATIVE_PROCESS_PATH_UNAVAILABLE' }
    if (![string]::Equals($pixavPath.ToString(), $ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'NATIVE_PROCESS_PATH_MISMATCH' }
    $pixavHash = (Get-FileHash -LiteralPath $ExpectedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Mode -ne 'capture' -and ($pixavEntry.created_filetime -ne $pixavCreated.ToString() -or $pixavEntry.sha256 -ne $pixavHash)) { throw 'NATIVE_PROCESS_IDENTITY_MISMATCH' }
    $pixavAlreadyStopped = $Mode -eq 'halt' -and [PixavNativeProcess]::WaitForSingleObject($pixavHandle,0) -eq 0
    $pixavRecords[$Role] = @{pid=[int]$pixavEntry.pid; created_filetime=$pixavCreated.ToString(); sha256=$pixavHash; original_process_absent=$pixavAlreadyStopped}
}

function Invoke-PixavRead([string] $Arguments) {
    $pixavInfo = New-Object Diagnostics.ProcessStartInfo
    $pixavInfo.FileName=$pixavAdb
    $pixavInfo.Arguments='-s '+$pixavSpec.serial+' '+$Arguments
    $pixavInfo.UseShellExecute=$false
    $pixavInfo.CreateNoWindow=$true
    $pixavInfo.RedirectStandardOutput=$true
    $pixavInfo.RedirectStandardError=$true
    $pixavClient=[Diagnostics.Process]::Start($pixavInfo)
    $pixavOutput=$pixavClient.StandardOutput.ReadToEndAsync()
    $pixavError=$pixavClient.StandardError.ReadToEndAsync()
    if (!$pixavClient.WaitForExit(10000)) {$pixavClient.Kill();throw 'NATIVE_ADB_OBSERVATION_TIMEOUT'}
    if ($pixavClient.ExitCode -ne 0) {throw 'NATIVE_ADB_OBSERVATION_REJECTED'}
    return $pixavOutput.Result.Trim()
}

try {
    if ($Mode -ne 'halt') {Open-PixavProcess 'adb' $pixavAdb}
    Open-PixavProcess 'emulator' $pixavEmulator
    Open-PixavProcess 'qemu' $pixavQemu
    if ($Mode -ne 'halt') {
        $pixavListener=@(Get-NetTCPConnection -State Listen -LocalPort 5037 -ErrorAction Stop)
        if ($pixavListener.Count -ne 1 -or $pixavListener[0].LocalAddress -ne '127.0.0.1' -or $pixavListener[0].OwningProcess -ne $pixavSpec.processes.adb.pid) {throw 'NATIVE_ADB_OWNER_MISMATCH'}
    }
    $pixavEmulatorCim=Get-CimInstance Win32_Process -Filter ('ProcessId='+$pixavSpec.processes.emulator.pid)
    if (!$pixavRecords.emulator.original_process_absent) {
        if (!$pixavEmulatorCim -or $pixavEmulatorCim.CommandLine -notmatch ('-avd\s+'+[regex]::Escape($pixavSpec.avd_name)+'(?:\s|$)') -or $pixavEmulatorCim.CommandLine -notmatch ('-port\s+'+$pixavPort+'(?:\s|$)')) {throw 'NATIVE_AVD_PROCESS_BINDING_MISMATCH'}
    }
    $pixavQemuCim=Get-CimInstance Win32_Process -Filter ('ProcessId='+$pixavSpec.processes.qemu.pid)
    if (!$pixavRecords.qemu.original_process_absent -and (!$pixavQemuCim -or $pixavQemuCim.ParentProcessId -ne $pixavSpec.processes.emulator.pid)) {throw 'NATIVE_QEMU_PARENT_MISMATCH'}
    $pixavChildren=@(Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$pixavSpec.processes.emulator.pid)|Where-Object {$_.Name -eq 'qemu-system-x86_64-headless.exe'})
    if (@($pixavChildren|Where-Object {$_.ProcessId -ne $pixavSpec.processes.qemu.pid}).Count -ne 0) {throw 'NATIVE_CHILD_SET_MISMATCH'}
    if ($Mode -eq 'halt') {
        foreach ($pixavRole in @('emulator','qemu')) {
            if ($pixavHandles.ContainsKey($pixavRole)) {
                $pixavHandle=$pixavHandles[$pixavRole]
                if ([PixavNativeProcess]::WaitForSingleObject($pixavHandle,0) -ne 0) {
                    if (![PixavNativeProcess]::TerminateProcess($pixavHandle,0)) {throw 'NATIVE_PROCESS_STOP_REJECTED'}
                }
                if ([PixavNativeProcess]::WaitForSingleObject($pixavHandle,10000) -ne 0) {throw 'NATIVE_PROCESS_STOP_UNCONFIRMED'}
            }
        }
        $pixavResult='ORIGINAL_NATIVE_WRITERS_STOPPED'
        $pixavBoot=$pixavSpec.boot_id
    } else {
        $pixavConsole=@(Get-NetTCPConnection -State Listen -LocalPort $pixavPort -ErrorAction Stop)
        if ($pixavConsole.Count -eq 0 -or @($pixavConsole|Where-Object {$_.OwningProcess -ne $pixavSpec.processes.qemu.pid -or $_.LocalAddress -notin @('127.0.0.1','::1')}).Count -ne 0) {throw 'NATIVE_CONSOLE_OWNER_MISMATCH'}
        $pixavName=Invoke-PixavRead 'shell getprop ro.boot.qemu.avd_name'
        $pixavBoot=Invoke-PixavRead 'shell cat /proc/sys/kernel/random/boot_id'
        if ($pixavName -ne $pixavSpec.avd_name -or $pixavBoot -notmatch '^[a-f0-9-]{36}$') {throw 'NATIVE_ANDROID_IDENTITY_MISMATCH'}
        if ($Mode -eq 'observe' -and $pixavBoot -ne $pixavSpec.boot_id) {throw 'NATIVE_BOOT_IDENTITY_MISMATCH'}
        $pixavResult='NATIVE_IDENTITY_OBSERVED'
    }
    [ordered]@{schema='windows-native-control-v1'; sdk_root=$pixavSdk; serial=$pixavSpec.serial; avd_name=$pixavSpec.avd_name; boot_id=$pixavBoot; processes=$pixavRecords; result=$pixavResult}|ConvertTo-Json -Depth 5 -Compress
} finally {
    foreach ($pixavHandle in $pixavHandles.Values) {[void][PixavNativeProcess]::CloseHandle($pixavHandle)}
}
