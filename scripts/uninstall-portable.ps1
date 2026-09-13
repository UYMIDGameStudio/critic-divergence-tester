[CmdletBinding()]
param(
    [string] $InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\DocumentReviewStudio'),
    [string] $ProjectRoot = (Join-Path $env:LOCALAPPDATA 'DocumentReviewStudio\projects'),
    [string] $ShortcutRoot = ([Environment]::GetFolderPath('Programs')),
    [switch] $ConfirmRemoval
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'portable-common.ps1')
$InstallRoot = Resolve-PortablePath $InstallRoot
$ProjectRoot = Resolve-PortablePath $ProjectRoot
$ShortcutRoot = Resolve-PortablePath $ShortcutRoot
Assert-PortableSeparate $InstallRoot $ProjectRoot
Assert-PortableSeparate $InstallRoot $ShortcutRoot
Assert-PortableSeparate $ProjectRoot $ShortcutRoot
if (-not $ConfirmRemoval -and (Read-Host 'Remove registered program versions? Project data and backups are preserved. Type REMOVE') -cne 'REMOVE') { return }
if (-not (Test-Path -LiteralPath $InstallRoot)) { Write-Output 'No installed program directory.'; return }
$lock = Enter-PortableLock $InstallRoot
try {
    Recover-PortableStaging $InstallRoot
    $receipt = Read-PortableReceipt $InstallRoot $ShortcutRoot
    if (-not $receipt) { throw 'No installation receipt. Unregistered directories will not be removed.' }
    $targets = @()
    foreach ($version in $receipt.versions) {
        $target = Join-Path $InstallRoot $version
        Assert-PortableUnlinked $target
        if (Test-Path -LiteralPath $target) {
            $manifest = Get-PortableManifest $target
            if ($manifest.version -cne $version) { throw 'Installed version does not match its directory.' }
            $targets += $target
        }
    }
    # Verify every registered version before deleting any, preserving unknown files.
    foreach ($target in $targets) { Remove-PortableTree $target $InstallRoot }
    $shortcutPath = Join-Path $ShortcutRoot 'Document Review Studio.lnk'
    Assert-PortableUnlinked $shortcutPath
    if ([IO.File]::Exists($shortcutPath)) {
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        $owned = @($receipt.versions | ForEach-Object { Join-Path (Join-Path $InstallRoot $_) 'DocumentReviewStudio.exe' })
        if ($owned -contains $shortcut.TargetPath) { [IO.File]::Delete($shortcutPath) }
    }
    [IO.File]::Delete((Join-Path $InstallRoot '.installation.json'))
    Write-Output 'Registered program versions removed. Projects, backups and unregistered files were preserved.'
} finally { $lock.Dispose() }
