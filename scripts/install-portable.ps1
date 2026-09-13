[CmdletBinding()]
param(
    [string] $InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\DocumentReviewStudio'),
    [string] $ProjectRoot = (Join-Path $env:LOCALAPPDATA 'DocumentReviewStudio\projects'),
    [string] $ShortcutRoot = ([Environment]::GetFolderPath('Programs')),
    [ValidateRange(1, 3600)] [int] $OperationTimeoutSeconds = 180
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'portable-common.ps1')

$releaseRoot = Resolve-PortablePath $PSScriptRoot
$manifest = Get-PortableManifest $releaseRoot
$InstallRoot = Resolve-PortablePath $InstallRoot
$ProjectRoot = Resolve-PortablePath $ProjectRoot
$ShortcutRoot = Resolve-PortablePath $ShortcutRoot
Assert-PortableSeparate $InstallRoot $ProjectRoot
Assert-PortableSeparate $InstallRoot $ShortcutRoot
Assert-PortableSeparate $ProjectRoot $ShortcutRoot
$lock = Enter-PortableLock $InstallRoot
$stage = Join-Path $InstallRoot ('.staging-' + [guid]::NewGuid().ToString('N'))
$target = Join-Path $InstallRoot $manifest.version
$receiptPath = Join-Path $InstallRoot '.installation.json'
$shortcutPath = Join-Path $ShortcutRoot 'Document Review Studio.lnk'
$published = $false
$committed = $false
$receiptChanged = $false
$shortcutChanged = $false
$stagingOwned = $false
$stagingJournal = Join-Path $InstallRoot '.install-staging.json'
try {
    Recover-PortableStaging $InstallRoot
    $receipt = Read-PortableReceipt $InstallRoot $ShortcutRoot
    Assert-PortableUnlinked $target
    Assert-PortableUnlinked $shortcutPath
    $oldReceipt = if ([IO.File]::Exists($receiptPath)) { [IO.File]::ReadAllBytes($receiptPath) } else { $null }
    $oldShortcut = if ([IO.File]::Exists($shortcutPath)) { [IO.File]::ReadAllBytes($shortcutPath) } else { $null }
    if (Test-Path -LiteralPath $target) {
        $null = Get-PortableManifest $target
        if ((Get-PortableDigest (Join-Path $target 'release-manifest.json')) -cne
            (Get-PortableDigest (Join-Path $releaseRoot 'release-manifest.json'))) {
            throw 'This version already exists with different contents. Use a new release version.'
        }
        Write-Output 'Verified existing installation; repairing its registered shortcut.'
    } else {
        $journal = @{ product = 'DocumentReviewStudio'; stage = [IO.Path]::GetFileName($stage) }
        Write-PortableAtomic $stagingJournal ([Text.Encoding]::UTF8.GetBytes(($journal | ConvertTo-Json)))
        $stagingOwned = $true
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        foreach ($entry in $manifest.files.PSObject.Properties) {
            $destination = Join-Path $stage $entry.Name
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            Copy-Item -LiteralPath (Join-Path $releaseRoot $entry.Name) -Destination $destination
        }
        Copy-Item -LiteralPath (Join-Path $releaseRoot 'release-manifest.json') -Destination (Join-Path $stage 'release-manifest.json')
        $null = Get-PortableManifest $stage
        $stageExe = Join-Path $stage 'DocumentReviewStudio.exe'
        Invoke-PortableExecutable $stageExe @('--self-test') $OperationTimeoutSeconds
        if (Test-Path -LiteralPath $ProjectRoot) {
            foreach ($project in Get-ChildItem -LiteralPath $ProjectRoot -Directory -Force -Filter '*.document-review-studio') {
                Assert-PortableUnlinked $project.FullName
                Invoke-PortableExecutable $stageExe @('studio-manage', 'upgrade-check', $project.FullName) $OperationTimeoutSeconds
                $backup = Join-Path $ProjectRoot ('backups\pre-upgrade-' + [guid]::NewGuid().ToString('N') + '.zip')
                Assert-PortableUnlinked $backup
                Invoke-PortableExecutable $stageExe @('studio-manage', 'backup', $project.FullName, $backup) $OperationTimeoutSeconds
            }
        }
        # Neither project commands nor the self-test may modify program files.
        $null = Get-PortableManifest $stage
        Assert-PortableUnlinked $target
        [IO.Directory]::Move($stage, $target)
        $published = $true
    }
    $versions = @($manifest.version)
    if ($receipt) { $versions = @($receipt.versions) + $versions }
    $updated = @{ product = 'DocumentReviewStudio'; format = 1; install_root = $InstallRoot; shortcut_root = $ShortcutRoot; versions = @($versions | Select-Object -Unique) }
    Write-PortableAtomic $receiptPath ([Text.Encoding]::UTF8.GetBytes(($updated | ConvertTo-Json -Depth 4)))
    $receiptChanged = $true
    Set-PortableShortcut $ShortcutRoot (Join-Path $target 'DocumentReviewStudio.exe') $target
    $shortcutChanged = $true
    $committed = $true
    Write-Output "Installed $($manifest.version). Previous program versions are preserved."
    Write-Output "Upgrade checks and backups cover only *.document-review-studio projects in: $ProjectRoot"
    Write-Output 'Custom libraries and ArgumentWorkbench research projects require their own backups before upgrade.'
} finally {
    try {
        if (-not $committed) {
            if ($shortcutChanged) {
                if ($null -ne $oldShortcut) { Write-PortableAtomic $shortcutPath $oldShortcut }
                elseif ([IO.File]::Exists($shortcutPath)) { [IO.File]::Delete($shortcutPath) }
            }
            if ($receiptChanged) {
                if ($null -ne $oldReceipt) { Write-PortableAtomic $receiptPath $oldReceipt }
                elseif ([IO.File]::Exists($receiptPath)) { [IO.File]::Delete($receiptPath) }
            }
            if ($published) { Remove-PortableTree $target $InstallRoot }
        }
        if (Test-Path -LiteralPath $stage) { Remove-PortableTree $stage $InstallRoot }
        if ($stagingOwned -and [IO.File]::Exists($stagingJournal)) { [IO.File]::Delete($stagingJournal) }
    } finally { $lock.Dispose() }
}
