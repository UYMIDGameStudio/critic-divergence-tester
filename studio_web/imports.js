function encodingSelector(id) {
  const options = {
    auto: tr('自动识别（推荐）'),
    'utf-8': tr('UTF-8 · 多语言'),
    'utf-16': tr('UTF-16 · 带编码标记'),
    'utf-16-le': 'UTF-16 LE',
    'utf-16-be': 'UTF-16 BE',
    'utf-32': tr('UTF-32 · 带编码标记'),
    'utf-32-le': 'UTF-32 LE',
    'utf-32-be': 'UTF-32 BE',
    gb18030: tr('GB18030 / GBK · 简体中文'),
    big5: tr('Big5 · 繁体中文'),
    big5hkscs: tr('Big5 HKSCS · 香港繁体'),
    cp1252: tr('Windows-1252 · 英德法及拉丁文字'),
    'iso8859-1': tr('ISO-8859-1 · 西欧'),
    'iso8859-15': tr('ISO-8859-15 · 西欧'),
    shift_jis: tr('Shift-JIS · 日语'),
    cp932: tr('Windows-932 · 日语'),
    euc_jp: tr('EUC-JP · 日语'),
    iso2022_jp: tr('ISO-2022-JP · 日语'),
    cp1251: tr('Windows-1251 · 俄语'),
    'koi8-r': tr('KOI8-R · 俄语'),
    'iso8859-5': tr('ISO-8859-5 · 西里尔文'),
  };
  return ui`<label for="${id}">文本文件编码</label><select id="${id}">${Object.entries(
    options,
  )
    .map(([value, label]) => `<option value="${value}">${label}</option>`)
    .join('')}</select>`;
}
function extractionEncodingControl(view) {
  const name = view.project.source.name.toLowerCase();
  if (name.endsWith('.pdf'))
    return ui`<details class="card"><summary>扫描文档识别语言</summary>${ocrLanguageSelector('retry-ocr-language')}<button class="secondary" id="retry-ocr">按所选语言重新识别</button><p class="muted">扫描页需要本机 OCR 引擎、对应语言包及 PDF 渲染组件。</p></details>`;
  if (
    ![
      '.txt',
      '.text',
      '.log',
      '.md',
      '.markdown',
      '.html',
      '.htm',
      '.csv',
      '.tsv',
      '.rtf',
    ].some((suffix) => name.endsWith(suffix))
  )
    return '';
  return ui`<details class="card"><summary>编码识别与乱码调整</summary><p>当前编码：${esc(view.extraction.metadata?.encoding || tr('尚未识别'))}。请检查英文、中文及其他语言的原文字母和标点。</p>${encodingSelector('retry-encoding')}<button id="retry-with-encoding" class="secondary">按所选编码重新识别</button></details>`;
}
function bindImportControls() {
  document
    .getElementById('retry-with-encoding')
    ?.addEventListener('click', () =>
      act('retry_extraction', {
        encoding: document.getElementById('retry-encoding').value,
      }),
    );
  document
    .getElementById('retry-ocr')
    ?.addEventListener('click', () =>
      act('retry_extraction', {
        ocr_language: document.getElementById('retry-ocr-language').value,
      }),
    );
}
function ocrLanguageSelector(id) {
  const options = {
    'chi_sim+chi_tra+eng': tr('简体 + 繁体 + 英文'),
    eng: tr('英文'),
    chi_sim: tr('简体中文'),
    chi_tra: tr('繁体中文'),
    deu: tr('德语'),
    fra: tr('法语'),
    jpn: tr('日语'),
    rus: tr('俄语'),
    lat: tr('拉丁语'),
    'eng+chi_sim+chi_tra+deu+fra+jpn+rus+lat':
      tr('全部八种语言（需要相应语言包）'),
  };
  return ui`<label for="${id}">扫描 PDF 的 OCR 语言</label><select id="${id}">${Object.entries(
    options,
  )
    .map(([value, label]) => `<option value="${value}">${label}</option>`)
    .join('')}</select>`;
}
