# 0.2.4 · Unicode 安装路径修复

0.2.3 的 GitHub Windows 安装验证暴露出系统编码差异：
旧 `WScript.Shell` 快捷方式接口在目标路径含系统 ANSI 编码无法表示的文字时失败。
本机用补充平面 Unicode 字符复现了同样的 `ArgumentException`。

0.2.4 使用 Windows `IShellLinkW` 的 Unicode 接口创建及读取快捷方式，
保留临时文件发布、失败回滚和路径链接检查。无需用户改变 Windows 语言或系统编码。
卸载先核对快捷方式再删除程序；损坏的快捷方式会阻止删除，
已被用户改成指向其他程序的快捷方式保持原样。

新增回归覆盖实际启动 Unicode 路径中的快捷方式、正确工作目录、卸载清理、
损坏快捷方式的删除前拦截，以及其他程序快捷方式的保留。
安装包内容变化使用新的补丁版本，避免与同版本不可覆盖检查冲突。

接口依据：[Microsoft IShellLinkW](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ishelllinkw)。
原始远端失败：[0.2.3 安装验证](https://github.com/UYMIDGameStudio/critic-divergence-tester/actions/runs/36109727809)。

本轮仅修补安装交付；稿件解析、审查协议与人工裁决方式保持兼容。
仍为未签名的 experimental preview，未以此宣称模型审查准确率或商业试用通过。
