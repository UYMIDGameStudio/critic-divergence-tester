param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateSet('fixtures','verify')][string]$Mode='fixtures'
)
$ErrorActionPreference='Stop'
$reviewOutput=[IO.Path]::GetFullPath($OutputDirectory)
$reviewWorkspace=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $reviewOutput.StartsWith($reviewWorkspace+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Native Office verification writes only inside this repository'
}
New-Item -ItemType Directory -Path $reviewOutput -Force | Out-Null
$reviewSamples=@(
    'English: Evidence supports the argument.',
    '简体中文：证据支持这一论点。',
    '繁體中文：證據支持這一論點。',
    'Deutsch: Die Belege stützen diese Schlussfolgerung.',
    'Français : Les preuves étayent cette conclusion.',
    '日本語：証拠はこの議論を支持する。',
    'Русский: Доказательства подтверждают этот вывод.',
    'Latina: Argumentum testimoniis confirmatur.'
)
$reviewReport=[ordered]@{ mode=$Mode; generated_at=(Get-Date).ToUniversalTime().ToString('o'); results=@() }
function Open-ReviewDocument($Application,[string]$Path) {
    $reviewDocuments=$Application.Documents
    # Word needs a document window for revision methods even while the parent
    # application is hidden. Visible controls that document window, not the app.
    $reviewOpenArguments=[object[]]@($Path,$false,$true,$false,[Type]::Missing,[Type]::Missing,$false,[Type]::Missing,[Type]::Missing,[Type]::Missing,[Type]::Missing,$true,$false)
    $reviewOpened=$reviewDocuments.GetType().InvokeMember('Open',[Reflection.BindingFlags]::InvokeMethod,$null,$reviewDocuments,$reviewOpenArguments)
    $Application.Visible=$false
    return $reviewOpened
}
$reviewWord=$null
try {
    $reviewWord=New-Object -ComObject Word.Application
    $reviewWord.Visible=$false
    $reviewWord.DisplayAlerts=0
    $reviewWord.AutomationSecurity=3
    $reviewReport.word_version=$reviewWord.Version
    $reviewReport.word_build=$reviewWord.Build
    if ($Mode -eq 'fixtures') {
        $reviewDoc=$reviewWord.Documents.Add()
        try {
            $reviewDoc.Content.Text=(@('Native Office acceptance fixture','相关人员及时完成报名。','Alpha BETA omega')+$reviewSamples+@('First numbered item','Second numbered item')) -join "`r"
            $reviewDoc.Content.Font.Name='Arial'
            $reviewDoc.Content.Font.NameFarEast='Microsoft JhengHei'
            $reviewDoc.Content.Font.Size=12
            $reviewDoc.Paragraphs.Item(1).Range.Font.Size=20
            $reviewDoc.Paragraphs.Item(1).Range.Font.Bold=1
            $reviewMixed=$reviewDoc.Paragraphs.Item(3).Range.Duplicate
            $reviewMixed.End=$reviewMixed.Start+6
            $reviewMixed.Font.Bold=1
            $reviewMixed.Start=$reviewMixed.End
            $reviewMixed.End=$reviewMixed.Start+4
            $reviewMixed.Font.Bold=0
            $reviewMixed.Font.Italic=1
            $reviewList=$reviewDoc.Range($reviewDoc.Paragraphs.Item(12).Range.Start,$reviewDoc.Content.End-1)
            $reviewList.ListFormat.ApplyNumberDefault()
            $reviewEnd=$reviewDoc.Range($reviewDoc.Content.End-1,$reviewDoc.Content.End-1)
            $reviewEnd.InsertParagraphAfter()
            $reviewEnd=$reviewDoc.Range($reviewDoc.Content.End-1,$reviewDoc.Content.End-1)
            $reviewTable=$reviewDoc.Tables.Add($reviewEnd,3,2)
            $reviewTable.Borders.Enable=1
            $reviewTable.Cell(1,1).Range.Text='Language'
            $reviewTable.Cell(1,2).Range.Text='Evidence'
            $reviewTable.Cell(2,1).Range.Text='Deutsch / Français'
            $reviewTable.Cell(2,2).Range.Text='First paragraph.'+"`r"+'Second paragraph.'
            $reviewTable.Cell(3,1).Range.Text='繁體中文 / 日本語'
            $reviewTable.Cell(3,2).Range.Text='確認済み。'
            $reviewEnd=$reviewDoc.Range($reviewDoc.Content.End-1,$reviewDoc.Content.End-1)
            $reviewEnd.InsertBreak(2)
            $reviewEnd=$reviewDoc.Range($reviewDoc.Content.End-1,$reviewDoc.Content.End-1)
            $reviewEnd.Text='Second section: layout and language checks'+"`r"+($reviewSamples -join "`r")
            $reviewDoc.Sections.Item(2).PageSetup.Orientation=1
            $reviewDoc.Sections.Item(1).Headers.Item(1).Range.Text='Document Review Studio / Fixture'
            $reviewDoc.Sections.Item(1).Footers.Item(1).Range.Text='QA document only'
            $reviewDoc.SaveAs2((Join-Path $reviewOutput 'source.docx'),16)
            $reviewDoc.ExportAsFixedFormat((Join-Path $reviewOutput 'source.pdf'),17)
            $reviewDoc.SaveAs2((Join-Path $reviewOutput 'source.doc'),0)
            $reviewReport.results+=@{file='source.docx'; pages=$reviewDoc.ComputeStatistics(2); sections=$reviewDoc.Sections.Count; tables=$reviewDoc.Tables.Count}
        } finally { $reviewDoc.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc) }
    } else {
        foreach ($reviewName in @('source','clean','tracked','split','project-clean','project-tracked')) {
            $reviewPath=Join-Path $reviewOutput ($reviewName+'.docx')
            if (-not (Test-Path -LiteralPath $reviewPath)) { throw "Missing generated QA file: $reviewName" }
            # OpenAndRepair is deliberately false: automatic repair cannot count as valid output.
            $reviewDoc=Open-ReviewDocument $reviewWord $reviewPath
            try {
                $reviewRow=@{file=$reviewName+'.docx'; pages=$reviewDoc.ComputeStatistics(2); sections=$reviewDoc.Sections.Count; tables=$reviewDoc.Tables.Count; revisions=$reviewDoc.Revisions.Count; text=$reviewDoc.Content.Text}
                $reviewDoc.ExportAsFixedFormat((Join-Path $reviewOutput ($reviewName+'-word.pdf')),17)
                if ($reviewName.EndsWith('tracked')) {
                    $reviewDoc.Revisions.AcceptAll()
                    $reviewRow.accepted_text=$reviewDoc.Content.Text
                    $reviewDoc.ExportAsFixedFormat((Join-Path $reviewOutput ($reviewName+'-accepted-word.pdf')),17)
                }
                $reviewReport.results+=$reviewRow
            } finally { $reviewDoc.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc) }
        }
        $reviewDoc=Open-ReviewDocument $reviewWord (Join-Path $reviewOutput 'tracked.docx')
        try {
            $reviewDoc.Revisions.RejectAll()
            $reviewReport.rejected_text=$reviewDoc.Content.Text
        } finally { $reviewDoc.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc) }
        $reviewDoc=Open-ReviewDocument $reviewWord (Join-Path $reviewOutput 'project-tracked.docx')
        try {
            $reviewDoc.Revisions.RejectAll()
            $reviewReport.project_rejected_text=$reviewDoc.Content.Text
        } finally { $reviewDoc.Close(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDoc) }
    }
} finally {
    if ($null -ne $reviewWord) { $reviewWord.Quit(0); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewWord) }
}
if ($Mode -eq 'fixtures') {
    $reviewExcel=$null
    try {
        $reviewExcel=New-Object -ComObject Excel.Application
        $reviewExcel.Visible=$false
        $reviewExcel.DisplayAlerts=$false
        $reviewExcel.AutomationSecurity=3
        $reviewBook=$reviewExcel.Workbooks.Add()
        try {
            $reviewSheet=$reviewBook.Worksheets.Item(1)
            for ($reviewIndex=0; $reviewIndex -lt $reviewSamples.Count; $reviewIndex++) {
                $reviewSheet.Cells.Item($reviewIndex+1,1).Value2=$reviewSamples[$reviewIndex]
            }
            $reviewSheet.Cells.Item(1,2).Value2=12
            $reviewSheet.Cells.Item(2,2).Value2=30
            $reviewSheet.Cells.Item(3,2).Formula='=SUM(B1:B2)'
            $reviewBook.SaveAs((Join-Path $reviewOutput 'source.xls'),56)
            $reviewReport.excel_version=$reviewExcel.Version
        } finally { $reviewBook.Close($false); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewBook) }
    } finally { if ($null -ne $reviewExcel) { $reviewExcel.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewExcel) } }
    $reviewPowerPoint=$null
    try {
        $reviewPowerPoint=New-Object -ComObject PowerPoint.Application
        $reviewPowerPoint.AutomationSecurity=3
        $reviewDeck=$reviewPowerPoint.Presentations.Add(0)
        try {
            for ($reviewIndex=0; $reviewIndex -lt 2; $reviewIndex++) {
                $reviewSlide=$reviewDeck.Slides.Add($reviewIndex+1,12)
                $reviewText=$reviewSlide.Shapes.AddTextbox(1,36,36,640,420)
                $reviewText.TextFrame.TextRange.Text=($reviewSamples[($reviewIndex*4)..($reviewIndex*4+3)] -join "`r")
            }
            $reviewDeck.SaveAs((Join-Path $reviewOutput 'source.ppt'),1)
            $reviewReport.powerpoint_version=$reviewPowerPoint.Version
        } finally { $reviewDeck.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewDeck) }
    } finally { if ($null -ne $reviewPowerPoint) { $reviewPowerPoint.Quit(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($reviewPowerPoint) } }
}
$reviewReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $reviewOutput ($Mode+'-native.json')) -Encoding utf8
Write-Output (Join-Path $reviewOutput ($Mode+'-native.json'))
