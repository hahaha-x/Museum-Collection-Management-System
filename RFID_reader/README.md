# Alien 9900 标签采集程序

本程序使用 C# 和官方 Alien .NET SDK 2.3.2，通过有线/无线局域网读取标签 EPC。
运行环境：Windows、.NET Framework 4.8；不依赖 Java。

现在程序同时提供本机 HTTP 服务，默认地址 `http://127.0.0.1:5080`。
`museum_app` 没有改动，不会自动使用此服务；其现有模拟采集也不会被本程序关闭。
完整接口、错误语义、Python 调用及后续接入注意事项见 [API.md](API.md)。

## 文件说明

| 文件 | 用途 |
|---|---|
| Program.cs | 连接、登录、轮询、解析 EPC、保存结果及离线测试，含中文注释 |
| RfidApi.cs | HTTP 接口、线程安全快照、新扫描任务、任务超时和结果保留 |
| ApiTests.cs | 不连接硬件的状态测试及本机 HTTP 集成测试 |
| API.md | 给 Flask 后端开发者使用的接口说明和 Python 示例 |
| App.config | IP、端口、账户、天线、采集间隔等配置 |
| RfidCollector.csproj | C# 项目描述，目标为 net48，不是 .NET 8 |
| build.ps1 | 使用 Windows Framework 编译器编译，不需要 NuGet 下载 |
| lib/AlienRFID2.dll | 从本机官方 SDK 复制的通信库，不是自己写的源代码 |
| lib/AlienRFID2.xml | SDK API 文档，用于编辑器提示，不是连接配置 |
| bin/local | 编译产物，可重新生成 |
| .gitignore | 忽略生成文件和采集数据的 Git 规则 |

## 编译和启动

先退出 Gateway，确认电脑能连接 Reader，且实际 RFID 天线已接好。
默认 Reader 为 192.168.1.100:23，用户名 alien，密码 password，天线为 3。
电脑不能使用与 Reader 相同的 IP，例如使用 192.168.1.10/24。

在 VS Code 终端执行：

```powershell
cd D:\Project\museum\RFID_reader
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
.\bin\local\RfidCollector.exe --self-test
.\bin\local\RfidCollector.exe --http-self-test
.\bin\local\RfidCollector.exe
```

修改 App.config 后重新执行 build.ps1，配置会复制到可执行文件旁。
也可直接修改 bin/local/RfidCollector.exe.config，但下一次编译会覆盖它。
使用 dotnet build 则需要首次恢复 .NET Framework 参考程序集包，输出在 bin/Debug/net48。
不要用 dotnet run 的 net8 项目直接引用此旧 SDK。

Ctrl+C 停止。SDK 同步操作可能需等待约 60 秒或更长时间超时，退出不会立即打断正在执行的命令。
启动时会显示接口地址和调用密钥。可在启动前设置至少 16 字符的 `RFID_API_TOKEN` 环境变量，
否则每次启动生成不同密钥。密钥只交给可信的 Flask 后端，不要放进网页 JavaScript。
所有接口都要求 `X-RFID-Token` 请求头；不要把服务绑定到公网或把 Reader 23 端口暴露到公网。
连接或登录失败会记录无效观测并重试；持续失败时请停止并检查网络、密码。
密码可以用当前终端的 RFID_PASSWORD 环境变量覆盖；不要上传真实密码。
Telnet 通信不加密，只在可信实验局域网使用。

## 数据和边界

- bin/local/data/observations-日期.jsonl：每轮一行，字段与主项目 RFID 观测契约一致。
- bin/local/data/details-日期.jsonl：对应观测 ID、天线、次数、RSSI、设备时间、原始响应。
- 正常无标签：valid=true，epc_list=[]；连接或解析失败：valid=false，并记录错误。
- 每轮 EPC 去重，保留前导零；详情保留不同天线的记录。
- collected_at 使用电脑 UTC 时间。截图中的设备时间为 1970 年，不作为业务时间。
- RSSI 保留厂商原始值，不是 dBm，不可直接推算距离。
- 两份日志不是事务写入；磁盘故障会终止程序，可能只有其中一份记录，按 observation_id 核对。
- 程序会设置 AutoMode=OFF、天线序列、Text 输出和关闭天线合并，每轮清列表后采集。
  不执行 Save，不修改 IP/射频功率/频段，不写标签；退出不自动恢复设备先前参数。
- 这是单读写器采集服务，尚未替换主项目的模拟接口，不会自动办理藏品出入库。
  单轮没有标签不能证明藏品丢失；完整盘点还需扫描范围、时间窗口和人工复核。

lib 中二进制来自本机下载的官方 SDK，仅作为开发依赖保留；再发布时请核对厂商许可。
离线自测验证截图两种 EPC、前导零、去重、空读、异常响应及唯一观测 ID，不代表实机测试。
接口状态自测还检查任务互斥、排队、迟到响应、超时和清理；HTTP 自测只在本机临时端口注入测试数据。
HTTP 自测中 source=device 是内部测试夹具，不是实机验证证据；生产模式不会自动回退到模拟数据。
