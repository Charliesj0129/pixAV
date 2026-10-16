param([ValidateSet('capture', 'observe', 'halt', 'stopped', 'quiesce', 'quiescence')][string] $Mode = 'observe')

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
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr CommandLineToArgvW(string commandLine, out int argc);
    [DllImport("kernel32.dll")]
    public static extern IntPtr LocalFree(IntPtr memory);
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
function Assert-PixavLaunch([string] $CommandLine) {
    if (!$pixavSpec.environment) {return}
    [int]$pixavCount=0
    $pixavArgv=[PixavNativeProcess]::CommandLineToArgvW($CommandLine,[ref]$pixavCount)
    if ($pixavArgv -eq [IntPtr]::Zero) {throw 'NATIVE_LAUNCH_ARGUMENTS_UNAVAILABLE'}
    try {
        $pixavArguments=@()
        for ($pixavIndex=1;$pixavIndex -lt $pixavCount;$pixavIndex++) {
            $pixavPointer=[Runtime.InteropServices.Marshal]::ReadIntPtr($pixavArgv,$pixavIndex*[IntPtr]::Size)
            $pixavArguments += [Runtime.InteropServices.Marshal]::PtrToStringUni($pixavPointer)
        }
        $pixavExpected=@('-avd',$pixavSpec.avd_name,'-datadir',$pixavSpec.environment.avd_path,'-sysdir',(Join-Path $pixavSdk 'system-images\android-35\google_apis_playstore\x86_64'),'-port',$pixavPort.ToString(),'-cores','2','-memory','4096','-gpu','swiftshader_indirect','-no-window','-no-snapshot-load','-no-snapshot-save','-no-boot-anim','-ramdisk',$pixavSpec.environment.private_ramdisk.path)
        if ($pixavArguments.Count -ne $pixavExpected.Count) {throw 'NATIVE_LAUNCH_BINDING_MISMATCH'}
        for ($pixavIndex=0;$pixavIndex -lt $pixavExpected.Count;$pixavIndex++) {
            if (![string]::Equals($pixavArguments[$pixavIndex],$pixavExpected[$pixavIndex],[StringComparison]::OrdinalIgnoreCase)) {throw 'NATIVE_LAUNCH_BINDING_MISMATCH'}
        }
    } finally {[void][PixavNativeProcess]::LocalFree($pixavArgv)}
}

function Open-PixavProcess([string] $Role, [string] $ExpectedPath) {
    $pixavEntry = $pixavSpec.processes.$Role
    if (!$pixavEntry -or [long]$pixavEntry.pid -le 0) { throw 'NATIVE_PROCESS_BINDING_MISSING' }
    $pixavAccess = [uint32](0x1000 -bor 0x100000)
    if ($Mode -eq 'halt' -and $Role -ne 'adb') { $pixavAccess = $pixavAccess -bor 1 }
    $pixavHandle = [PixavNativeProcess]::OpenProcess($pixavAccess, $false, [uint32]$pixavEntry.pid)
    if ($pixavHandle -eq [IntPtr]::Zero) {
        $pixavWinError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($Mode -in @('halt','stopped') -and $Role -ne 'adb' -and $pixavWinError -eq 87) {
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
    $pixavAlreadyStopped = $Mode -in @('halt','stopped') -and [PixavNativeProcess]::WaitForSingleObject($pixavHandle,0) -eq 0
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

function Invoke-PixavRoot([string] $Script, [ValidateSet('ROOT','FRAMEWORK','MODULES','SCOPE','PACKAGE','QUIESCENCE')][string] $Operation = 'ROOT') {
    $pixavInfo=New-Object Diagnostics.ProcessStartInfo
    $pixavInfo.FileName=$pixavAdb
    $pixavInfo.Arguments='-s '+$pixavSpec.serial+' shell /debug_ramdisk/su -p -c /system/bin/sh'
    $pixavInfo.UseShellExecute=$false
    $pixavInfo.CreateNoWindow=$true
    $pixavInfo.RedirectStandardInput=$true
    $pixavInfo.RedirectStandardOutput=$true
    $pixavInfo.RedirectStandardError=$true
    $pixavClient=[Diagnostics.Process]::Start($pixavInfo)
    $pixavOutput=$pixavClient.StandardOutput.ReadToEndAsync()
    $pixavError=$pixavClient.StandardError.ReadToEndAsync()
    $pixavClient.StandardInput.WriteLine($Script)
    $pixavClient.StandardInput.Close()
    # Listing modules scans installed APKs. A cold two-core AVD can take more
    # than ten seconds; keep this read bounded within the host's 45s deadline.
    $pixavQueryTimeout=10000
    if ($Operation -eq 'MODULES') {$pixavQueryTimeout=30000}
    if (!$pixavClient.WaitForExit($pixavQueryTimeout)) {$pixavClient.Kill();throw ('NATIVE_'+$Operation+'_OBSERVATION_TIMEOUT')}
    if ($pixavClient.ExitCode -ne 0) {throw 'NATIVE_ROOT_OBSERVATION_REJECTED'}
    return $pixavOutput.Result.Trim()
}

function Assert-PixavEnvironment {
    $pixavEnvironment=$pixavSpec.environment
    if (!$pixavEnvironment) {return}
    if ($pixavEnvironment.profile -ne 'gphotos_pixel_xl_infinity_v1' -or $pixavEnvironment.boot_id -ne $pixavSpec.boot_id) {throw 'NATIVE_ENVIRONMENT_BINDING_MISMATCH'}
    $pixavStatusScript=@'
test "$(cat /proc/sys/kernel/random/boot_id)" = '__BOOT__' || exit 73
test "$(id -u)" = 0 || exit 74
test "$(/debug_ramdisk/magisk -V)" = 30700 || exit 75
/data/adb/modules/zygisk_vector/cli --json status
'@
    $pixavStatus=(Invoke-PixavRoot ($pixavStatusScript.Replace('__BOOT__',$pixavSpec.boot_id)) 'FRAMEWORK')|ConvertFrom-Json
    if (!$pixavStatus.success -or $pixavStatus.data.'Version Code' -ne 3080 -or $pixavStatus.data.'API Version' -ne 102) {throw 'NATIVE_FRAMEWORK_CHANGED'}
    $pixavModules=(Invoke-PixavRoot '/data/adb/modules/zygisk_vector/cli --json modules ls' 'MODULES')|ConvertFrom-Json
    $pixavEnabled=@($pixavModules.data|Where-Object {$_.STATUS -eq 'enabled'})
    if (!$pixavModules.success -or $pixavEnabled.Count -ne 1 -or $pixavEnabled[0].PACKAGE -ne 'io.github.samson910022.pixelifyphotos') {throw 'NATIVE_MODULE_SET_CHANGED'}
    $pixavScope=(Invoke-PixavRoot '/data/adb/modules/zygisk_vector/cli --json scope ls io.github.samson910022.pixelifyphotos' 'SCOPE')|ConvertFrom-Json
    if (!$pixavScope.success -or @($pixavScope.data).Count -ne 1 -or $pixavScope.data[0].APP_PACKAGE -ne 'com.google.android.apps.photos' -or $pixavScope.data[0].USER_ID -ne 0) {throw 'NATIVE_PHOTOS_SCOPE_CHANGED'}
    foreach ($pixavPackage in @('io.github.samson910022.pixelifyphotos','com.google.android.apps.photos')) {
        $pixavInstalled=Invoke-PixavRead ('shell pm path '+$pixavPackage)
        $pixavPaths=@()
        foreach ($pixavLine in @($pixavInstalled -split '\r?\n')) {
            if ($pixavLine -notmatch '^package:(/[A-Za-z0-9_./=+~-]+\.apk)$') {throw 'NATIVE_PACKAGE_PATH_UNCONFIRMED'}
            $pixavPaths+= $matches[1]
        }
        $pixavSplitContract=$pixavPackage -eq 'com.google.android.apps.photos' -and $pixavEnvironment.schema -eq 'windows-pixel-environment-v2'
        if ($pixavSplitContract) {
            if ($pixavPaths.Count -ne @($pixavEnvironment.photos_apks).Count) {throw 'NATIVE_PHOTOS_APK_SET_CHANGED'}
            foreach ($pixavPath in $pixavPaths) {
                $pixavExpected=@($pixavEnvironment.photos_apks|Where-Object {$_.path -ceq $pixavPath})
                if ($pixavExpected.Count -ne 1) {throw 'NATIVE_PHOTOS_APK_SET_CHANGED'}
                $pixavHash=(Invoke-PixavRead ('shell sha256sum '+$pixavPath)).Split(' ')[0]
                $pixavSize=Invoke-PixavRead ('shell stat -c %s '+$pixavPath)
                if ($pixavHash -cne $pixavExpected[0].sha256 -or $pixavSize -cne ([string]$pixavExpected[0].size_bytes)) {throw 'NATIVE_PHOTOS_APK_SET_CHANGED'}
            }
        } else {
            if ($pixavPaths.Count -ne 1) {throw 'NATIVE_PACKAGE_PATH_UNCONFIRMED'}
            $pixavHash=(Invoke-PixavRead ('shell sha256sum '+$pixavPaths[0])).Split(' ')[0]
        }
        $pixavVersion=(Invoke-PixavRoot ('dumpsys package '+$pixavPackage+' | grep -m1 versionName=') 'PACKAGE').Trim() -replace '^versionName=',''
        if ($pixavPackage -eq 'com.google.android.apps.photos') {
            if ((!$pixavSplitContract -and $pixavHash -cne $pixavEnvironment.photos_apk_sha256) -or $pixavVersion -cne $pixavEnvironment.photos_version) {throw 'NATIVE_PHOTOS_PACKAGE_CHANGED'}
        } elseif ($pixavHash -ne '6cac6601adbfb2e27ff4596c8d38735a7a7d0cbf228f646c5e73dbfc8b74203a' -or $pixavVersion -ne '1.4.1') {throw 'NATIVE_MODULE_PACKAGE_CHANGED'}
        if ((Invoke-PixavRead ('shell pm path '+$pixavPackage)) -cne $pixavInstalled) {throw 'NATIVE_PACKAGE_PATH_CHANGED_DURING_OBSERVATION'}
    }
}

function Get-PixavQuiescence {
    if ($pixavSpec.session_id -notmatch '^[a-f0-9-]{36}$' -or $pixavSpec.boot_id -notmatch '^[a-f0-9-]{36}$') {throw 'NATIVE_SESSION_MARKER_INVALID'}
    $pixavShell=@'
test "$(cat /proc/sys/kernel/random/boot_id)" = '__BOOT__' || exit 73
test "$(id -u)" = 0 || exit 74
__STOP_APPS__
tagged=$(grep -a -F -l 'PIXAV_PHOTOS_UI_SESSION=' /proc/[0-9]*/environ 2>/dev/null | wc -l)
tagged=$((tagged+0))
apps=0
for package in com.google.android.apps.photos dev.mobile.maestro dev.mobile.maestro.test; do
  if pidof "$package" >/dev/null 2>&1; then apps=$((apps+1)); fi
done
printf '%s %s\n' "$tagged" "$apps"
'@
    $pixavStop=''
    if ($Mode -eq 'quiesce') {$pixavStop='am force-stop com.google.android.apps.photos; am force-stop dev.mobile.maestro; am force-stop dev.mobile.maestro.test'}
    $pixavShell=$pixavShell.Replace('__BOOT__',$pixavSpec.boot_id).Replace('__SESSION__',$pixavSpec.session_id).Replace('__STOP_APPS__',$pixavStop)
    $pixavOutput=Invoke-PixavRoot $pixavShell 'QUIESCENCE'
    if ($pixavOutput -notmatch '^([0-9]+) ([0-9]+)$') {throw 'NATIVE_QUIESCENCE_UNCONFIRMED'}
    return [int]$matches[1] -eq 0 -and [int]$matches[2] -eq 0
}

try {
    if ($Mode -notin @('halt','stopped')) {Open-PixavProcess 'adb' $pixavAdb}
    Open-PixavProcess 'emulator' $pixavEmulator
    Open-PixavProcess 'qemu' $pixavQemu
    if ($Mode -notin @('halt','stopped')) {
        $pixavListener=@(Get-NetTCPConnection -State Listen -LocalPort 5037 -ErrorAction Stop)
        if ($pixavListener.Count -ne 1 -or $pixavListener[0].LocalAddress -ne '127.0.0.1' -or $pixavListener[0].OwningProcess -ne $pixavSpec.processes.adb.pid) {throw 'NATIVE_ADB_OWNER_MISMATCH'}
    }
    $pixavEmulatorCim=Get-CimInstance Win32_Process -Filter ('ProcessId='+$pixavSpec.processes.emulator.pid)
    if (!$pixavRecords.emulator.original_process_absent) {
        if (!$pixavEmulatorCim -or $pixavEmulatorCim.CommandLine -notmatch ('-avd\s+'+[regex]::Escape($pixavSpec.avd_name)+'(?:\s|$)') -or $pixavEmulatorCim.CommandLine -notmatch ('-port\s+'+$pixavPort+'(?:\s|$)')) {throw 'NATIVE_AVD_PROCESS_BINDING_MISMATCH'}
        if ($Mode -notin @('halt','stopped')) {Assert-PixavLaunch $pixavEmulatorCim.CommandLine}
    }
    $pixavQemuCim=Get-CimInstance Win32_Process -Filter ('ProcessId='+$pixavSpec.processes.qemu.pid)
    if (!$pixavRecords.qemu.original_process_absent -and (!$pixavQemuCim -or $pixavQemuCim.ParentProcessId -ne $pixavSpec.processes.emulator.pid)) {throw 'NATIVE_QEMU_PARENT_MISMATCH'}
    if ($Mode -notin @('halt','stopped')) {Assert-PixavLaunch $pixavQemuCim.CommandLine}
    $pixavChildren=@(Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$pixavSpec.processes.emulator.pid)|Where-Object {$_.Name -eq 'qemu-system-x86_64-headless.exe'})
    if (@($pixavChildren|Where-Object {$_.ProcessId -ne $pixavSpec.processes.qemu.pid}).Count -ne 0) {throw 'NATIVE_CHILD_SET_MISMATCH'}
    if ($Mode -in @('halt','stopped')) {
        foreach ($pixavRole in @('emulator','qemu')) {
            if ($pixavHandles.ContainsKey($pixavRole)) {
                $pixavHandle=$pixavHandles[$pixavRole]
                if ($Mode -eq 'halt' -and [PixavNativeProcess]::WaitForSingleObject($pixavHandle,0) -ne 0) {
                    if (![PixavNativeProcess]::TerminateProcess($pixavHandle,0)) {throw 'NATIVE_PROCESS_STOP_REJECTED'}
                }
                $pixavWait=0
                if ($Mode -eq 'halt') {$pixavWait=10000}
                if ([PixavNativeProcess]::WaitForSingleObject($pixavHandle,$pixavWait) -ne 0) {throw 'NATIVE_PROCESS_STOP_UNCONFIRMED'}
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
        Assert-PixavEnvironment
        $pixavResult='NATIVE_IDENTITY_OBSERVED'
        if ($Mode -in @('quiesce','quiescence')) {
            if (Get-PixavQuiescence) {$pixavResult='NATIVE_UI_QUIESCENT'} else {$pixavResult='NATIVE_UI_WRITERS_ACTIVE'}
        }
    }
    $pixavResponse=[ordered]@{schema='windows-native-control-v1'; sdk_root=$pixavSdk; serial=$pixavSpec.serial; avd_name=$pixavSpec.avd_name; boot_id=$pixavBoot; processes=$pixavRecords; result=$pixavResult}
    if ($pixavSpec.environment -and $Mode -notin @('halt','stopped')) {$pixavResponse.environment_verified=$true}
    $pixavResponse|ConvertTo-Json -Depth 5 -Compress
} catch {
    $pixavCode=$_.Exception.Message
    if ($pixavCode -notmatch '^NATIVE_[A-Z_]{1,80}$') {$pixavCode='NATIVE_CONTROL_UNCONFIRMED'}
    [Console]::Error.WriteLine((@{code=$pixavCode}|ConvertTo-Json -Compress))
    exit 1
} finally {
    foreach ($pixavHandle in $pixavHandles.Values) {[void][PixavNativeProcess]::CloseHandle($pixavHandle)}
}
