param([Parameter(Mandatory=$true)][string]$OutputDirectory,
      [ValidateSet('fixtures','verify')][string]$Mode='fixtures', [switch]$Prelaunch)
$ErrorActionPreference='Stop'
$reviewOutput=[IO.Path]::GetFullPath($OutputDirectory)
$reviewDist=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../dist'))
if (-not $reviewOutput.StartsWith($reviewDist+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {throw 'Evidence must stay inside repository dist'}
New-Item -ItemType Directory -Path $reviewOutput -Force | Out-Null
$reviewSource=Join-Path $reviewOutput 'source.docx'
if ($Mode -eq 'fixtures' -and (Test-Path -LiteralPath $reviewSource)) {throw 'Refusing to replace an existing native fixture'}
($Mode+'-starting') | Set-Content -LiteralPath (Join-Path $reviewOutput 'native-progress.txt') -Encoding UTF8
function Open-ReviewWord($Application,[string]$Path) {
    $reviewArguments=[object[]]@($Path,$false,$true,$false,[Type]::Missing,[Type]::Missing,$false,[Type]::Missing,[Type]::Missing,[Type]::Missing,[Type]::Missing,$true,$false)
    $reviewDocument=$Application.Documents.GetType().InvokeMember('Open',[Reflection.BindingFlags]::InvokeMethod,$null,$Application.Documents,$reviewArguments)
    $Application.Visible=$false
    return $reviewDocument
}
function Get-ReviewSnapshot($Document) {
    $reviewLinks=@()
    foreach($reviewLink in $Document.Hyperlinks) {
        $reviewLinks+=@{address=[string]$reviewLink.Address; subaddress=[string]$reviewLink.SubAddress; text=[string]$reviewLink.Range.Text; italic=[int]$reviewLink.Range.Font.Italic}
    }
    return @{text=$Document.Content.Text.TrimEnd([char[]]"`r`a");links=$reviewLinks;sections=$Document.Sections.Count}
}
$reviewWord=$null;$reviewOwned=$false;$reviewProcess=$null;$reviewOwnedBlank=$false
try {
    if ($Prelaunch) {
        $reviewServer=(Get-ItemProperty -LiteralPath 'Registry::HKEY_CLASSES_ROOT\CLSID\{000209FF-0000-0000-C000-000000000046}\LocalServer32').'(default)'
        if($reviewServer -notmatch '^(?:"([^"]+\.exe)"|(.+?\.exe))(?:\s|$)'){throw 'Word executable is not registered'}
        $reviewExecutable=if($Matches[1]){$Matches[1]}else{$Matches[2]}
        if([IO.Path]::GetFileName($reviewExecutable) -ine 'WINWORD.EXE'){throw 'Unexpected Word automation server'}
        $reviewProcess=Start-Process -FilePath $reviewExecutable -ArgumentList '/w','/q' -WindowStyle Hidden -PassThru
        $reviewProcess.Id | Set-Content -LiteralPath (Join-Path $reviewOutput 'owned-process-id.txt') -Encoding ASCII
        Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class ReviewWordWindow { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId); }'
        for($reviewAttempt=0;$reviewAttempt -lt 20 -and $null -eq $reviewWord -and -not $reviewProcess.HasExited;$reviewAttempt++) {
            Start-Sleep -Milliseconds 500
            try {
                $reviewActive=[Runtime.InteropServices.Marshal]::GetActiveObject('Word.Application')
                [uint32]$reviewOwner=0
                [void][ReviewWordWindow]::GetWindowThreadProcessId([IntPtr]$reviewActive.Hwnd,[ref]$reviewOwner)
                if($reviewOwner -eq $reviewProcess.Id){$reviewWord=$reviewActive;$reviewOwnedBlank=$true}
                else{[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewActive)}
            } catch {}
        }
        if($null -eq $reviewWord){'owned-automation-unavailable' | Set-Content -LiteralPath (Join-Path $reviewOutput 'native-progress.txt') -Encoding UTF8;throw 'The owned Word instance did not expose an automation object; native verification was not performed'}
    }
    'creating-automation-object' | Set-Content -LiteralPath (Join-Path $reviewOutput 'native-progress.txt') -Encoding UTF8
    if($null -eq $reviewWord){$reviewWord=New-Object -ComObject Word.Application}
    'automation-ready' | Set-Content -LiteralPath (Join-Path $reviewOutput 'native-progress.txt') -Encoding UTF8
    if ($reviewWord.Documents.Count -ne 0) {
        if(-not $reviewOwnedBlank -or $reviewWord.Documents.Count -ne 1){throw 'Refusing to use an instance with existing documents'}
        $reviewBlank=$reviewWord.Documents.Item(1)
        if($reviewBlank.Path -ne '' -or $reviewBlank.Content.Text -notmatch '^[\s\x07]*$'){throw 'Owned startup document is not empty'}
        $reviewBlank.Close(0);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewBlank)
    }
    $reviewOwned=$true;$reviewWord.Visible=$false;$reviewWord.DisplayAlerts=0;$reviewWord.AutomationSecurity=3
    if ($Mode -eq 'fixtures') {
        $reviewDoc=$reviewWord.Documents.Add()
        try {
            $reviewDoc.Content.Text="Alpha CITATION omega`r"
            $reviewRange=$reviewDoc.Range(0,6);$reviewRange.Font.Bold=1
            $reviewRange=$reviewDoc.Range(6,14)
            [void]$reviewDoc.Hyperlinks.Add($reviewRange,'https://example.invalid/study','','Owned source','CITATION')
            $reviewRange.Font.Italic=1
            $reviewDoc.SaveAs2($reviewSource,12)
        } finally {$reviewDoc.Close(0);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc)}
    } else {
        $reviewCases=Get-Content -LiteralPath (Join-Path $reviewOutput 'cases.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ((Get-FileHash -LiteralPath $reviewSource -Algorithm SHA256).Hash.ToLowerInvariant() -ne $reviewCases.source_sha256) {throw 'Native source changed'}
        $reviewDoc=Open-ReviewWord $reviewWord $reviewSource
        try {$reviewOriginal=Get-ReviewSnapshot $reviewDoc} finally {$reviewDoc.Close(0);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc)}
        $reviewRows=@()
        foreach($reviewCase in $reviewCases.cases) {
            if ($reviewCase.id -notmatch '^[a-z-]+$') {throw 'Invalid owned case name'}
            foreach($reviewKind in @('clean','tracked')) {
                $reviewPath=Join-Path $reviewOutput ($reviewCase.id+'-'+$reviewKind+'.docx')
                $reviewDoc=Open-ReviewWord $reviewWord $reviewPath
                try {
                    $reviewRevisions=$reviewDoc.Revisions.Count
                    if ($reviewKind -eq 'tracked') {if ($reviewRevisions -eq 0) {throw 'Missing native revisions'};$reviewDoc.Revisions.AcceptAll()}
                    $reviewAccepted=Get-ReviewSnapshot $reviewDoc
                    if ($reviewAccepted.text -ne $reviewCase.text -or $reviewAccepted.links.Count -ne 1 -or $reviewAccepted.links[0].text -ne $reviewCase.label) {throw ('Native text or link label mismatch: '+$reviewCase.id)}
                    if ($reviewAccepted.links[0].address -ne $reviewOriginal.links[0].address -or $reviewAccepted.links[0].italic -ne $reviewOriginal.links[0].italic) {throw 'Native link target or formatting changed'}
                    $reviewRows+=@{case=$reviewCase.id;kind=$reviewKind;revisions=$reviewRevisions;accepted=$reviewAccepted}
                } finally {$reviewDoc.Close(0);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc)}
            }
            $reviewDoc=Open-ReviewWord $reviewWord (Join-Path $reviewOutput ($reviewCase.id+'-tracked.docx'))
            try {
                $reviewDoc.Revisions.RejectAll();$reviewRejected=Get-ReviewSnapshot $reviewDoc
                if (($reviewRejected | ConvertTo-Json -Depth 8 -Compress) -ne ($reviewOriginal | ConvertTo-Json -Depth 8 -Compress)) {throw ('Native reject mismatch: '+$reviewCase.id)}
            } finally {$reviewDoc.Close(0);[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc)}
        }
        @{passed=$true;word_version=$reviewWord.Version;word_build=$reviewWord.Build;results=$reviewRows} | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $reviewOutput 'native-verification.json') -Encoding UTF8
    }
} finally {
    if ($null -ne $reviewWord) {if($reviewOwned){$reviewWord.Quit(0)};[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewWord)}
    if($null -ne $reviewProcess -and -not $reviewProcess.HasExited -and ($reviewOwned -or $null -eq $reviewWord)){Stop-Process -Id $reviewProcess.Id -Force}
}
