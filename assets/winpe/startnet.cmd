@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem VM2Q Forge WinPE/DISM entry point.
rem The helper disk contains the short-name request marker and the optional
rem first-boot network script. The target Windows disk and virtio-win ISO can
rem therefore use any drive letters assigned by WinPE.
wpeinit

set "RESULT="
set "WIN="
set "DRV="
set "DRV_ROOT="
set "DRV_ARCH="
for /l %%N in (1,1,30) do (
    for %%D in (A B C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
        if not defined RESULT if exist "%%D:\VM2QREQ.TXT" set "RESULT=%%D:"
        if not defined WIN if exist "%%D:\Windows\System32\config\SYSTEM" set "WIN=%%D:"
    )
    if defined RESULT if defined WIN goto :disks_ready
    ping 127.0.0.1 -n 3 >nul
)

goto :missing

:disks_ready
set "GUEST_ARCH=x86"
if exist "!WIN!\Windows\SysWOW64" set "GUEST_ARCH=amd64"
for /l %%N in (1,1,30) do (
    for %%D in (A B C D E F G H I J K L M N O P Q R S T U V W Y Z) do (
        if not defined DRV (
            for %%R in (w7 w8.1 w8 w10 w11) do (
                if not defined DRV if "!GUEST_ARCH!"=="amd64" if exist "%%D:\viostor\%%R\amd64\viostor.inf" (
                    set "DRV=%%D:"
                    set "DRV_ROOT=%%R"
                    set "DRV_ARCH=amd64"
                )
                if not defined DRV if "!GUEST_ARCH!"=="x86" if exist "%%D:\viostor\%%R\x86\viostor.inf" (
                    set "DRV=%%D:"
                    set "DRV_ROOT=%%R"
                    set "DRV_ARCH=x86"
                )
            )
        )
    )
    if defined DRV goto :ready
    ping 127.0.0.1 -n 3 >nul
)
goto :missing

:ready
set "LOG=!RESULT!\VM2QLOG.TXT"
set "OUT=!RESULT!\VM2QRES.TXT"
echo VM2Q Forge WinPE/DISM starting>"!LOG!"

if not defined WIN (
    echo status=missing-windows-partition>"!OUT!"
    echo Windows partition was not found>>"!LOG!"
    wpeutil shutdown
    exit /b 20
)
if not defined DRV (
    echo status=missing-virtio-iso>"!OUT!"
    echo virtio-win w7 amd64 driver directory was not found>>"!LOG!"
    wpeutil shutdown
    exit /b 21
)

if not exist "!RESULT!\VM2QSCR" mkdir "!RESULT!\VM2QSCR" >nul 2>&1
set "RC=0"
set "STOR_RC=0"
set "NET_RC=0"
set "SCSI_RC=0"
set "VIO_SERIAL_RC=0"
set "QGA_MSI_RC=0"
set "QGA_SCRIPT_RC=0"
set "QGA_REG_RC=0"
set "QGA_AUTOLOGON_RC=0"
set "REG_RC=0"
set "QGA_FILE=qemu-ga-i386.msi"
if "!GUEST_ARCH!"=="amd64" set "QGA_FILE=qemu-ga-x86_64.msi"
set "QGA="
if exist "!DRV!\guest-agent\!QGA_FILE!" set "QGA=!DRV!\guest-agent\!QGA_FILE!"
set "QGA_AUTOLOGON="
if exist "!RESULT!\VM2QALOG.REG" set "QGA_AUTOLOGON=!RESULT!\VM2QALOG.REG"

echo windows_drive=!WIN!>>"!LOG!"
echo driver_drive=!DRV!>>"!LOG!"
echo driver_root=!DRV_ROOT!>>"!LOG!"
echo driver_arch=!DRV_ARCH!>>"!LOG!"
echo guest_arch=!GUEST_ARCH!>>"!LOG!"

dism /English /Image:!WIN! /Add-Driver /Driver:!DRV!\viostor\!DRV_ROOT!\!DRV_ARCH! /Recurse /ForceUnsigned /ScratchDir:!RESULT!\VM2QSCR >>"!LOG!" 2>&1
if errorlevel 1 (
    set "STOR_RC=1"
    set "RC=1"
)

if exist "!DRV!\vioscsi\!DRV_ROOT!\!DRV_ARCH!\vioscsi.inf" (
    dism /English /Image:!WIN! /Add-Driver /Driver:!DRV!\vioscsi\!DRV_ROOT!\!DRV_ARCH! /Recurse /ForceUnsigned /ScratchDir:!RESULT!\VM2QSCR >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "SCSI_RC=1"
        set "RC=1"
    )
)

dism /English /Image:!WIN! /Add-Driver /Driver:!DRV!\NetKVM\!DRV_ROOT!\!DRV_ARCH! /Recurse /ForceUnsigned /ScratchDir:!RESULT!\VM2QSCR >>"!LOG!" 2>&1
if errorlevel 1 (
    set "NET_RC=1"
    set "RC=1"
)

if exist "!DRV!\vioserial\!DRV_ROOT!\!DRV_ARCH!\vioser.inf" (
    dism /English /Image:!WIN! /Add-Driver /Driver:!DRV!\vioserial\!DRV_ROOT!\!DRV_ARCH! /Recurse /ForceUnsigned /ScratchDir:!RESULT!\VM2QSCR >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "VIO_SERIAL_RC=1"
        set "RC=1"
    )
) else (
    set "VIO_SERIAL_RC=1"
    set "RC=1"
    echo vioserial driver was not found>>"!LOG!"
)

if not defined QGA (
    set "QGA_MSI_RC=1"
    set "RC=1"
    echo QEMU Guest Agent MSI was not found>>"!LOG!"
)

rem Stage the first-boot DHCP script and register it in the offline SOFTWARE
rem hive. The doubled percent signs preserve %ProgramData% for the target OS.
if exist "!RESULT!\VM2QNET.CMD" (
    if not exist "!WIN!\ProgramData\VM2Q-Forge" mkdir "!WIN!\ProgramData\VM2Q-Forge" >nul 2>&1
    copy /y "!RESULT!\VM2QNET.CMD" "!WIN!\ProgramData\VM2Q-Forge\configure-windows-virtio-net.cmd" >>"!LOG!" 2>&1
    reg load HKLM\VM2Q_SOFTWARE "!WIN!\Windows\System32\Config\SOFTWARE" >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "REG_RC=1"
        set "RC=1"
    ) else (
        reg add HKLM\VM2Q_SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce /v VM2QForgeVirtioNet /t REG_EXPAND_SZ /d "cmd.exe /c %%ProgramData%%\VM2Q-Forge\configure-windows-virtio-net.cmd" /f >>"!LOG!" 2>&1
        if errorlevel 1 (
            set "REG_RC=1"
            set "RC=1"
        )
        reg unload HKLM\VM2Q_SOFTWARE >>"!LOG!" 2>&1
    )
)

rem Stage QEMU Guest Agent and register its first-boot or bake installer.
if not exist "!RESULT!\VM2QGA.CMD" (
    set "QGA_SCRIPT_RC=1"
    set "RC=1"
    echo QEMU Guest Agent installer script was not found>>"!LOG!"
) else if defined QGA (
    if not exist "!WIN!\ProgramData\VM2Q-Forge" mkdir "!WIN!\ProgramData\VM2Q-Forge" >nul 2>&1
    copy /y "!QGA!" "!WIN!\ProgramData\VM2Q-Forge\!QGA_FILE!" >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "QGA_SCRIPT_RC=1"
        set "RC=1"
    )
    copy /y "!RESULT!\VM2QGA.CMD" "!WIN!\ProgramData\VM2Q-Forge\install-qemu-guest-agent.cmd" >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "QGA_SCRIPT_RC=1"
        set "RC=1"
    )
    reg load HKLM\VM2Q_QGA_SOFTWARE "!WIN!\Windows\System32\Config\SOFTWARE" >>"!LOG!" 2>&1
    if errorlevel 1 (
        set "QGA_REG_RC=1"
        set "RC=1"
    ) else (
        if defined QGA_AUTOLOGON (
            reg import "!QGA_AUTOLOGON!" >>"!LOG!" 2>&1
            if errorlevel 1 (
                set "QGA_AUTOLOGON_RC=1"
                set "RC=1"
            )
        )
        reg add HKLM\VM2Q_QGA_SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce /v VM2QForgeQemuGuestAgent /t REG_EXPAND_SZ /d "cmd.exe /c %%ProgramData%%\VM2Q-Forge\install-qemu-guest-agent.cmd" /f >>"!LOG!" 2>&1
        if errorlevel 1 (
            set "QGA_REG_RC=1"
            set "RC=1"
        )
        reg unload HKLM\VM2Q_QGA_SOFTWARE >>"!LOG!" 2>&1
        if errorlevel 1 (
            set "QGA_REG_RC=1"
            set "RC=1"
        )
    )
)

if "!RC!"=="0" (
    echo status=success>"!OUT!"
) else (
    echo status=failed>"!OUT!"
)
echo viostor_rc=!STOR_RC!>>"!OUT!"
echo vioscsi_rc=!SCSI_RC!>>"!OUT!"
echo netkvm_rc=!NET_RC!>>"!OUT!"
echo vioserial_rc=!VIO_SERIAL_RC!>>"!OUT!"
echo qga_msi_rc=!QGA_MSI_RC!>>"!OUT!"
echo qga_script_rc=!QGA_SCRIPT_RC!>>"!OUT!"
echo qga_registry_rc=!QGA_REG_RC!>>"!OUT!"
echo qga_autologon_rc=!QGA_AUTOLOGON_RC!>>"!OUT!"
if defined QGA_AUTOLOGON echo qga_bake=1 >>"!OUT!"
if not defined QGA_AUTOLOGON echo qga_bake=0 >>"!OUT!"
echo registry_rc=!REG_RC!>>"!OUT!"
echo windows_drive=!WIN!>>"!OUT!"
echo driver_drive=!DRV!>>"!OUT!"
echo driver_root=!DRV_ROOT!>>"!OUT!"
echo driver_arch=!DRV_ARCH!>>"!OUT!"
echo qga_msi=!QGA_FILE!>>"!OUT!"
echo status=!RC!>>"!LOG!"

wpeutil shutdown
exit /b !RC!

:missing
rem A result disk is required so the host can distinguish a clean DISM result
rem from a helper that never reached startnet.cmd.
wpeutil shutdown
exit /b 22
