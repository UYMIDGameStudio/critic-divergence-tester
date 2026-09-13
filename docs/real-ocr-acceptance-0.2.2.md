# 真實 OCR 驗收：0.2.2

本機已實際執行八種語言的掃描 PDF 識別。產品 OCR 渲染改為 300 DPI 後，乾淨日文樣本在 144 DPI 下的漏句消失。繁體字形、拉丁長音與連字，以及多語混排仍有誤識；本次結果不構成所有文件無損識別的保證。

這是原生 Tesseract 執行結果，沒有替換模型回傳。每種語言只有一頁、三行現代橫排印刷文字，另有一份從真實 Word 匯出的兩頁多語、編號與表格文件。掃描樣本均已驗證沒有可提取文字層。字體缺字檢查與圖片目檢通過，來源 PDF 和識別文字保留於驗收證據中。

| 語言或樣本 | 144 DPI 字元錯誤率 | 生產 300 DPI 字元錯誤率 | 剩餘情況 |
|---|---:|---:|---|
| English | 0% | 0% | 本樣本沒有識別差異 |
| 简体中文 | 0% | 0% | 本樣本沒有識別差異 |
| 繁體中文 | 3.23% | 3.23% | `審` → `害`；全形逗號 → 半形逗號 |
| Deutsch | 0% | 0% | 本樣本的 ß、ü、Ä 等保留 |
| Français | 0% | 0% | 本樣本的重音、œ 等保留 |
| 日本語 | 10.91% | 0% | 144 DPI 曾誤識 `別` 並遺漏句尾 `重要です` |
| Русский | 0% | 0% | 本樣本的西里爾文字與 ё 保留 |
| Latina | 8.74% | 7.77% | 長音符號和 æ、œ 未可靠保留 |
| Word 多語混排、兩頁 | 9.44% | 10.51% | 語言混淆、標點、編號與表格閱讀順序 |

錯誤率為 Levenshtein 編輯距離除以參考字元數。只進行 NFC 和去空白處理，不移除大小寫、標點、重音、長音或連字差異。去空白是因 Tesseract TSV 會在中日文詞間插入空格。Word 參考文字採用經圖片目檢的視覺閱讀順序；最初 PDF 內容串流將頁尾排在正文前的參考版本另存，不用它評估最終錯誤率。

相同引擎與樣本的受控比較中，單語每頁渲染及 OCR 約由 0.20–0.30 秒增至 0.31–0.42 秒，多語兩頁由 2.40 秒增至 2.94 秒。這是單次本機量測，包含輸入圖片證據保存，未包含完整文件歸檔序列化；不是效能基準或延遲承諾。兩種解析度均保留每頁 16,000,000 像素及 8192 邊長限制。Tesseract 官方品質文件建議至少 300 DPI，與本次日文改善一致。[官方品質說明](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html)

產品仍要求人工確認 OCR 內容。真實驗收另檢查了原始檔案 SHA-256 綁定、OCR 區塊標記、頁碼及 `requires_confirmation`。`pipeline_pass` 表示這些流程條件通過，不代表轉寫完全正確。此驗收沒有覆蓋手寫、污損、模糊攝影、歷史字體、豎排日文、數學公式或大規模文件集合。

另以獨立目錄中的官方 `tessdata_best` 比較了繁體及拉丁模型，沒有替換原有模型：繁體仍為 3.23%，拉丁為 8.74%。本樣本沒有品質改善。兩種拉丁模型解出的 `lstm-unicharset` 均不包含 `ē ū ī ō ā æ œ` 或組合長音符 U+0304；其中基本拉丁句正確，但帶長音文字和連字不能靠提高解析度可靠恢復。原始字元表和實際差異保留於證據，沒有依語意自動改回原稿。[官方 fast 模型](https://github.com/tesseract-ocr/tessdata_fast)、[官方 best 模型](https://github.com/tesseract-ocr/tessdata_best)

使用 Tesseract `v5.5.3.20260724`、pypdf `6.18.1`、pypdfium2 `5.13.0`。執行驗收的 Python 未安裝 Pillow、reportlab 或 PyMuPDF；只有製作測試圖片時使用 Codex 隨附的 Pillow 和 reportlab，產品 OCR 路徑未依賴它們。

引擎由 [UB Mannheim 的 Windows 發行資訊](https://github.com/UB-Mannheim/tesseract/wiki) 取得 [Tesseract 5.5.3 發行檔](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3)，僅解包至 `D:/agent/.tools/tesseract-5.5.3`，沒有執行安裝程式或改動系統設定。下載檔 SHA-256 與官方 GitHub release asset digest 相符：`bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4`。本機 Authenticode 回傳 `UnknownError`，訊息為憑證時間有效期問題；這次驗收依據官方發布的雜湊校驗，沒有把 Authenticode 記錄為有效。

語言包固定並逐一驗證官方 Git blob SHA-1，另記錄 SHA-256：fast commit `87416418657359cb625c412a48b6e1d6d41c29bd`，best commit `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`。下載來源、位元組數、引擎與程式碼雜湊、實際依賴版本均在 JSON 證據內。這些工具只用於本機驗收，沒有隨產品重新分發。

可使用 `scripts/verify_real_ocr.py` 重現。先以具備 Pillow/reportlab 的 Python 執行 `--mode fixtures --native-pdf dist/native-office-0.2.2/source.pdf`；正式驗收使用下列命令，不會自動下載元件：

```powershell
& D:/agent/review-verification-20260905/Scripts/python.exe scripts/verify_real_ocr.py --mode verify --engine D:/agent/.tools/tesseract-5.5.3/tesseract.exe --provenance D:/agent/.tools/ocr-download-provenance.json
& D:/agent/review-verification-20260905/Scripts/python.exe scripts/verify_real_ocr.py --mode compare-dpi --engine D:/agent/.tools/tesseract-5.5.3/tesseract.exe
& D:/agent/review-verification-20260905/Scripts/python.exe scripts/verify_real_ocr.py --mode compare-models --engine D:/agent/.tools/tesseract-5.5.3/tesseract.exe --comparison-data D:/agent/.tools/tessdata-best-verification
```

本機證據位於 `dist/real-ocr-0.2.2/`：`report.json` 為目前生產流程；`production-144dpi.json` 保留舊生產結果；`dpi-comparison.json` 為受控解析度對照；`best-model-comparison.json` 為可選模型對照。`fixtures/manifest.json` 保存標準文字與來源綁定，`results/` 保存實際文字、完整 StructuredDocument 及渲染圖片。
