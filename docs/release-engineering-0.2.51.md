# 0.2.51：macOS 导出后打开文件夹

macOS 的 `os.name` 为 `posix`，原代码却用它判断 `darwin`，因此落入 Linux 分支调用
`xdg-open`，导致用户在导出后打开所在文件夹时报错。

现改用 `sys.platform` 识别 macOS，并调用原生 `open`。Windows 继续使用 `os.startfile`，
Linux 继续使用 `xdg-open`。目录作为单独参数传递，支持中文、空格和带音符的路径；
访问范围和导出完整性检查仍在启动外部程序之前执行。

回归使用真实创建及正式导出的合成项目，检查 macOS/POSIX 通过实际应用动作入口选择
`open`，同时验证 Windows、Linux，以及非法路径、缺失文件和被改动的导出文件不会启动
文件管理器。新增 macOS 原生 CI：在实际系统身份下检查 `/usr/bin/open` 存在及程序选择。
CI 拦截进程启动，不将该检查描述为 Finder 窗口的人工验收。

源码版在项目目录更新后，结束原来的服务进程并按原方式重新启动。已经导出的文件可以
直接在 Finder 中访问，不需要重新审查或重新导出。
