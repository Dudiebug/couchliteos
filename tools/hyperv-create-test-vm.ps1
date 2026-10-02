#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Create a Hyper-V Generation 2 test VM that boots a CouchLiteOS ISO.

.DESCRIPTION
The VM covers boot, launcher, Settings, persistence (install to the virtual
disk), and Remote Desktop. It cannot emulate the iMac's NVIDIA Kepler GPU,
Broadcom Wi-Fi, or audio; test those on the hardware (docs/BUILDING.md#testing).

The CouchLiteOS ISO ships Debian's Microsoft-signed shim, so Secure Boot stays
on with the "Microsoft UEFI Certificate Authority" template. Use
-SecureBoot Off if your host or ISO needs it. The script never replaces or
deletes an existing VM or disk.

.EXAMPLE
.\hyperv-create-test-vm.ps1 -IsoPath D:\iso\couchliteos-0.1.12-imac2013-amd64.iso -Start

.EXAMPLE
.\hyperv-create-test-vm.ps1 -IsoPath .\couchliteos-0.1.12-amd64.iso -Name CouchLiteOS-Intel -SwitchName LAN -SecureBoot Off
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$IsoPath,
    [string]$Name = 'CouchLiteOS-Test',
    [string]$SwitchName = 'Default Switch',
    [ValidateRange(2, 64)][int]$MemoryGB = 4,
    [ValidateRange(1, 32)][int]$ProcessorCount = 4,
    [ValidateRange(16, 512)][int]$DiskGB = 32,
    [string]$VhdPath,
    [ValidateSet('On', 'Off')][string]$SecureBoot = 'On',
    [ValidateRange(640, 7680)][int]$Width = 1920,
    [ValidateRange(480, 4320)][int]$Height = 1080,
    [switch]$Start
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) {
    throw 'Hyper-V PowerShell module not found. Enable Hyper-V (Microsoft-Hyper-V-All) and reboot first.'
}
$iso = (Resolve-Path -LiteralPath $IsoPath).ProviderPath
if ([IO.Path]::GetExtension($iso) -ne '.iso') { throw "Not an ISO file: $iso" }
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) {
    throw "A VM named '$Name' already exists. Choose another -Name or remove it yourself; this script never deletes VMs."
}
if (-not (Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue)) {
    $available = (Get-VMSwitch | Select-Object -ExpandProperty Name) -join ', '
    throw "Virtual switch '$SwitchName' not found. Available: $available"
}
if (-not $VhdPath) {
    $VhdPath = Join-Path (Get-VMHost).VirtualHardDiskPath "$Name.vhdx"
}
if (Test-Path -LiteralPath $VhdPath) {
    throw "Disk already exists: $VhdPath. Choose another -VhdPath; this script never overwrites disks."
}

$vm = New-VM -Name $Name -Generation 2 -MemoryStartupBytes ($MemoryGB * 1GB) `
    -NewVHDPath $VhdPath -NewVHDSizeBytes ($DiskGB * 1GB) -SwitchName $SwitchName
Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
Set-VMProcessor -VM $vm -Count $ProcessorCount
Set-VM -VM $vm -AutomaticCheckpointsEnabled $false -CheckpointType Disabled
$dvd = Add-VMDvdDrive -VM $vm -Path $iso -Passthru
if ($SecureBoot -eq 'On') {
    Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority -FirstBootDevice $dvd
} else {
    Set-VMFirmware -VM $vm -EnableSecureBoot Off -FirstBootDevice $dvd
}
# Cage uses the hyperv_drm KMS device; pick the mode the launcher starts in.
if (Get-Command Set-VMVideo -ErrorAction SilentlyContinue) {
    Set-VMVideo -VM $vm -ResolutionType Single -HorizontalResolution $Width -VerticalResolution $Height
}
# GRUB and the kernel mirror their console to COM1; read it with a pipe client.
$pipe = "\\.\pipe\$Name-com1"
Set-VMComPort -VM $vm -Number 1 -Path $pipe

Write-Output "Created Generation 2 VM '$Name'"
Write-Output "  ISO:         $iso"
Write-Output "  Disk:        $VhdPath ($DiskGB GiB, for 'Install CouchLiteOS' persistence tests)"
Write-Output "  Secure Boot: $SecureBoot$(if ($SecureBoot -eq 'On') { ' (Microsoft UEFI Certificate Authority)' })"
Write-Output "  Network:     $SwitchName"
Write-Output "  Serial:      $pipe"
if ($Start) {
    Start-VM -VM $vm
    Write-Output "Started. Open the console with: vmconnect.exe localhost '$Name'"
} else {
    Write-Output "Start it with: Start-VM -Name '$Name'; vmconnect.exe localhost '$Name'"
}
Write-Output 'After installing to the virtual disk, eject the ISO with:'
Write-Output "  Get-VMDvdDrive -VMName '$Name' | Set-VMDvdDrive -Path `$null"
