# Shared filesystem and manifest checks for the Windows portable installer.
# Shipped beside both entry scripts and included in the release manifest.
Set-StrictMode -Version Latest

function Resolve-PortablePath([string] $Path) {
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { throw 'An absolute local path is required.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($full.StartsWith('\\') -or $full.Length -le 3) { throw 'A local directory below the drive root is required.' }
    Assert-PortableUnlinked $full
    return $full
}

function Assert-PortableUnlinked([string] $Path) {
    $cursor = $Path
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked paths are not supported: $cursor" }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}

function Test-PortableWithin([string] $Path, [string] $Root) {
    return $Path.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PortableSeparate([string] $Left, [string] $Right) {
    if ((Test-PortableWithin $Left $Right) -or (Test-PortableWithin $Right $Left)) { throw 'Program, project and shortcut directories must not overlap.' }
}

function Read-PortableJson([string] $Path) {
    $raw = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    # Windows PowerShell silently accepts repeated JSON keys. Check each object
    # before conversion can hide a duplicate, including case and escape aliases.
    $frames = New-Object 'Collections.Generic.List[object]'
    foreach ($token in [regex]::Matches($raw, '"(?:[^"\\]|\\.)*"|[{}\[\]:,]')) {
        $value = $token.Value
        if ($value -eq '{' -or $value -eq '[') {
            $frames.Add(@{ Object = ($value -eq '{'); Key = ($value -eq '{'); Names = (New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)) })
        } elseif ($value -eq '}' -or $value -eq ']') {
            if ($frames.Count -eq 0) { throw 'Invalid JSON structure.' }
            $frames.RemoveAt($frames.Count - 1)
        } elseif ($frames.Count -gt 0) {
            $frame = $frames[$frames.Count - 1]
            if ($value -eq ':') { $frame.Key = $false }
            elseif ($value -eq ',') { $frame.Key = $frame.Object }
            elseif ($frame.Object -and $frame.Key -and $value.StartsWith('"')) {
                $name = @((ConvertFrom-Json ('[' + $value + ']')))[0]
                if (-not $frame.Names.Add($name)) { throw 'Duplicate JSON property.' }
                $frame.Key = $false
            }
        }
    }
    return ConvertFrom-Json $raw
}

function Get-PortableFiles([string] $Root) {
    $files = New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::OrdinalIgnoreCase)
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count) {
        $directory = $pending.Pop()
        Assert-PortableUnlinked $directory
        foreach ($path in [IO.Directory]::GetFileSystemEntries($directory)) {
            Assert-PortableUnlinked $path
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer) { $pending.Push($path) }
            else {
                $name = $path.Substring($Root.Length + 1).Replace('\', '/')
                if ($files.ContainsKey($name)) { throw 'Duplicate portable file path.' }
                $files.Add($name, $path)
            }
        }
    }
    return ,$files
}

function Get-PortableDigest([string] $Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Get-PortableManifest([string] $Root) {
    $Root = Resolve-PortablePath $Root
    $files = Get-PortableFiles $Root
    if (-not $files.ContainsKey('release-manifest.json')) { throw 'Missing release manifest.' }
    $manifest = Read-PortableJson $files['release-manifest.json']
    if ($manifest -isnot [pscustomobject]) { throw 'Invalid release manifest object.' }
    $keys = @($manifest.PSObject.Properties.Name)
    foreach ($required in @('version', 'platform', 'signed', 'files')) {
        if ($keys -cnotcontains $required) { throw "Missing manifest field: $required" }
    }
    foreach ($key in $keys) {
        if (@('version', 'platform', 'signed', 'files', 'build') -cnotcontains $key) { throw "Unknown manifest field: $key" }
    }
    if ($manifest.version -isnot [string] -or $manifest.version -cnotmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { throw 'Invalid release version.' }
    if ($manifest.platform -isnot [string] -or $manifest.platform -cne 'win32' -or $manifest.signed -isnot [bool]) { throw 'Invalid Windows release metadata.' }
    if ($manifest.files -isnot [pscustomobject]) { throw 'Invalid manifest file map.' }
    if ($keys -ccontains 'build' -and $manifest.build -isnot [pscustomobject]) { throw 'Invalid build metadata.' }
    $entries = @($manifest.files.PSObject.Properties)
    if ($entries.Count -eq 0 -or $entries.Count -gt 50000) { throw 'Invalid release file count.' }
    $declared = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $entries) {
        $name = $entry.Name
        if (-not $name -or $name -match '[\\:\x00-\x1f\x7f]' -or $name.StartsWith('/') -or $name -ieq 'release-manifest.json') { throw 'Invalid release path.' }
        foreach ($part in $name.Split('/')) {
            if (-not $part -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
                $part -match '[<>"|?*]' -or $part -match '^(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$') { throw 'Noncanonical release path.' }
        }
        if (-not $declared.Add($name)) { throw 'Duplicate release path.' }
        if ($entry.Value -isnot [string] -or $entry.Value -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid release digest.' }
        if (-not $files.ContainsKey($name)) { throw "Missing release file: $name" }
        if ((Get-PortableDigest $files[$name]) -cne $entry.Value) { throw "Release verification failed: $name" }
    }
    foreach ($required in @('DocumentReviewStudio.exe', 'portable-common.ps1', '安装或升级.ps1', '卸载程序.ps1', '使用说明.md', 'LICENSE')) {
        if (-not $declared.Contains($required)) { throw "Missing required release file: $required" }
    }
    if ($files.Count -ne $declared.Count + 1) { throw 'Release contains files absent from the manifest.' }
    return $manifest
}

function Enter-PortableLock([string] $Root) {
    Assert-PortableUnlinked $Root
    [IO.Directory]::CreateDirectory($Root) | Out-Null
    $path = Join-Path $Root '.install.lock'
    Assert-PortableUnlinked $path
    try { return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another installation or uninstall is running, or the installation directory is not writable.' }
}

function Remove-PortableTree([string] $Path, [string] $Root) {
    $resolved = Resolve-PortablePath $Path
    $rootPath = Resolve-PortablePath $Root
    if ($resolved.Equals($rootPath, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-PortableWithin $resolved $rootPath)) { throw 'Refusing cleanup outside the installation directory.' }
    if (Test-Path -LiteralPath $resolved) {
        $null = Get-PortableFiles $resolved
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Recover-PortableStaging([string] $Root) {
    # The installation lock must be held. Only a staging directory explicitly
    # named in our journal is owned; unrelated and legacy directories are kept.
    $path = Join-Path $Root '.install-staging.json'
    Assert-PortableUnlinked $path
    if (-not [IO.File]::Exists($path)) { return }
    $journal = Read-PortableJson $path
    if ($journal -isnot [pscustomobject] -or $journal.product -cne 'DocumentReviewStudio' -or
        $journal.stage -isnot [string] -or $journal.stage -cnotmatch '^\.staging-[a-f0-9]{32}$') { throw 'Invalid interrupted-install journal.' }
    Remove-PortableTree (Join-Path $Root $journal.stage) $Root
    [IO.File]::Delete($path)
}

function Invoke-PortableExecutable([string] $Executable, [string[]] $Arguments, [int] $TimeoutSeconds = 180) {
    $process = New-Object Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    # .NET Framework has no ArgumentList. Apply Windows native quoting rules,
    # including quotes and trailing backslashes, without going through a shell.
    $quoted = foreach ($argument in $Arguments) {
        '"' + [regex]::Replace([regex]::Replace($argument, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }
    $process.StartInfo.Arguments = $quoted -join ' '
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        $null = $process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errorOutput = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Portable operation timed out: $($Arguments[0])"
        }
        $stdout = $output.Result
        $stderr = $errorOutput.Result
        if ($stdout) { Write-Output $stdout.TrimEnd() }
        if ($process.ExitCode -ne 0) { throw "Portable operation failed (exit $($process.ExitCode)): $($Arguments[0]). $stderr" }
    } finally { $process.Dispose() }
}

function Read-PortableReceipt([string] $Root, [string] $ShortcutRoot) {
    $path = Join-Path $Root '.installation.json'
    Assert-PortableUnlinked $path
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $receipt = Read-PortableJson $path
    if ($receipt -isnot [pscustomobject] -or $receipt.product -cne 'DocumentReviewStudio' -or $receipt.format -ne 1 -or
        $receipt.install_root -cne $Root -or $receipt.shortcut_root -cne $ShortcutRoot -or $receipt.versions -isnot [array]) { throw 'Invalid installation receipt.' }
    foreach ($version in $receipt.versions) {
        if ($version -isnot [string] -or $version -cnotmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { throw 'Invalid installed version.' }
    }
    return $receipt
}

function Write-PortableAtomic([string] $Path, [byte[]] $Bytes) {
    Assert-PortableUnlinked $Path
    $temp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllBytes($temp, $Bytes)
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    } finally {
        if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
    }
}

function Initialize-PortableShortcut {
        # WScript.Shell rejects paths outside the system ANSI code page. Use
        # IShellLinkW explicitly so an English Windows installation can install
        # into Chinese (or other Unicode) directories without changing locale.
        if (-not ('StudioPortable.UnicodeShortcut' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
namespace StudioPortable {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink {}

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int command);
        void SetShowCmd(int command);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public static class UnicodeShortcut {
        public static string ReadTarget(string file) {
            object instance = new ShellLink();
            try {
                ((IPersistFile)instance).Load(file, 0);
                var path = new StringBuilder(32768);
                ((IShellLinkW)instance).GetPath(path, path.Capacity, IntPtr.Zero, 4);
                return path.ToString();
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
        public static void Save(string file, string target, string workingDirectory) {
            object instance = new ShellLink();
            try {
                var link = (IShellLinkW)instance;
                link.SetPath(target);
                link.SetWorkingDirectory(workingDirectory);
                ((IPersistFile)instance).Save(file, true);
            } finally { Marshal.FinalReleaseComObject(instance); }
        }
    }
}
'@
        }
}

function Set-PortableShortcut([string] $Root, [string] $Target, [string] $WorkingDirectory) {
    Assert-PortableUnlinked $Root
    [IO.Directory]::CreateDirectory($Root) | Out-Null
    $path = Join-Path $Root 'Document Review Studio.lnk'
    Assert-PortableUnlinked $path
    $temp = Join-Path $Root ('.studio-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        Initialize-PortableShortcut
        [StudioPortable.UnicodeShortcut]::Save($temp, $Target, $WorkingDirectory)
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temp, $path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $path) }
    } finally {
        if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
    }
}
