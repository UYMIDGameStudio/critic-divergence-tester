# 0.2.50：本机页面完整传输

0.2.49 的本机浏览器验收曾间歇加载超时。本轮以空项目独立复现：页面声明266949字节，
有时浏览器仅收到261509或201454字节，报告 `ERR_CONTENT_LENGTH_MISMATCH`，页面仍在
等待完成。服务端记录的写入已经返回；因此不能将此现象仅归为测试等待时间不足。

## 对照与调整

延长连接收尾等待、分段发送、移除测试拦截或改变半关闭步骤均仍可复现。
独立标准库服务器使用相同界面内容时，添加 `Connection: close` 也可复现，省略后连续
40次加载通过。本项目省略该冗余响应头的对照也连续40次通过。

现有本机服务器发送 HTTP/1.0 响应，使用其默认关闭语义；继续在一个连接内只处理一个
请求。HTTP/1.1 响应仍明确声明关闭。过载响应采用相同 HTTP/1.0 规则。
连接数上限、请求读取截止、响应发送后的有界丢弃与实际连接回收继续执行。

协议依据为 [RFC 9112 §9.3](https://www.rfc-editor.org/rfc/rfc9112.html#section-9.3)。
关闭过程另参考 [Microsoft shutdown](https://learn.microsoft.com/en-us/windows/win32/api/winsock/nf-winsock-shutdown)
与 [closesocket](https://learn.microsoft.com/en-us/windows/win32/api/winsock/nf-winsock-closesocket)。
本轮证据确认了本机的触发条件及规避效果，不据此断言所有 Windows 或浏览器版本存在同一问题，
也未确定浏览器、传输中间层或操作系统内部的具体缺陷。

## 回归范围

新增真实浏览器检查：统一工作台、统一研究入口、独立研究入口、专业工作台各连续加载24次，
共96次。每次核对声明长度、完整字节及内容摘要，等待界面取得状态，并保留多个页面。
错误继续直接报告，不通过重复导航替代失败。机器证据保存到 `dist/browser-http-delivery/result.json`，
CI 同时保存该记录。既有草稿、多页冲突、拒绝请求等流程继续参加完整浏览器回归。

新增原始连接检查：HTTP/1.0 默认关闭、HTTP/1.1 明确关闭，完整大页面后必须收到 EOF，
客户端附带的第二个请求不能被继续处理。原有请求拒绝、并发过载和连接回收检查继续执行。
发行自检以响应的真实关闭语义及完整长度验收错误回传，仍为40项。

诊断原始记录保存在本机 `dist/wire-diagnostic*/result.json` 及 `dist/http-wire-*.log`。
上一版的真实 Word 客户端自动化验收限制继续有效，见 [0.2.49](release-engineering-0.2.49.md)。
