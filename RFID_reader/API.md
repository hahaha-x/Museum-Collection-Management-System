# RFID 本机采集服务接口

## 范围和启动

本次仅修改 RFID_reader。museum_app 的 Flask 路由、数据库、模拟线程没有修改。
服务启动后自动连续采集，HTTP 接口用于获取状态和观测，或申请一轮新的采集。
网页浏览器应只访问 Flask；Flask 再访问此服务，不能直接在浏览器里暴露密钥。

```powershell
cd D:\Project\museum\RFID_reader
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
.\bin\local\RfidCollector.exe --self-test
.\bin\local\RfidCollector.exe --http-self-test
# 可选：用你自己生成的至少 16 字符随机值设置固定密钥，Flask 也使用同一值。
# $env:RFID_API_TOKEN = '你自己生成的随机密钥'
.\bin\local\RfidCollector.exe
```

先关闭 Gateway 及其他采集程序。程序会打印接口地址及本次密钥。
默认仅监听 `http://127.0.0.1:5080/`。所有请求都需要 `X-RFID-Token`。
不开放 CORS；带 Origin 的浏览器请求会被拒绝。没有远程控制、任意命令透传和标签写入接口。
没有 start/stop HTTP 接口，当前用程序启动和 Ctrl+C 控制采集。
默认扫描是配置天线的一轮采集，不是数秒窗口汇总，也不证明每个天线已完成覆盖。

App.config 参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| ApiPort | 5080 | 本机 HTTP 端口 |
| FreshnessMs | 5000 | 最近观测超过此时间视为过期 |
| ScanTimeoutMs | 90000 | 单个任务从申请起的最大等待时间，包含排队 |
| ScanRetentionMs | 600000 | 任务在其截止时间之后保留的时长 |
| PollIntervalMs | 1000 | 每轮完成后的等待时间，不是固定采样周期 |

修改配置后重新编译复制到输出目录。端口被占用时启动失败，不会继续连接硬件。
Windows 的 HttpListener 可能需要 URL 访问权限。如果确认是拒绝访问，而非端口占用，
可由管理员为当前账户配置一次精确的 URL 保留（把用户名替换为 `whoami` 的输出）：

```powershell
netsh http add urlacl url=http://127.0.0.1:5080/ user="计算机名\用户名"
```

只在确有权限错误时执行，不要改成通配监听地址或给 Everyone 授权。
程序本身不修改系统权限。受限沙箱可能直接拒绝 HTTP 服务初始化，应在普通本机终端测试。

## GET /health

返回 200 仅说明 HTTP 服务可用，不代表 Reader 正常。

主要字段：

- service_status：running / stopping。
- reader_connected：当前 SDK 是否完成连接、登录和初始化；不是持续网络探针。
- collecting：当前是否正在执行一轮采集。
- scan_busy：是否存在尚未释放的请求任务。
- last_observation_valid：上一轮是否有效。
- last_observation_at：上一轮的电脑 UTC 采集时间。
- stale：没有观测或最近观测过期。
- last_error：最近一轮错误。
- reader_ip、antenna_sequence、source。

Reader 调用卡住时，HTTP 线程仍能响应；应结合 stale 判断，不能只看 reader_connected。

## GET /observations/latest

返回结构：

```json
{
  "stale": false,
  "age_ms": 200,
  "started_at": "2026-10-16T10:00:00.0000000Z",
  "observation": {
    "observation_id": "obs_示例",
    "collected_at": "2026-10-16T10:00:01.0000000Z",
    "valid": true,
    "epc_list": ["0011223344556691"],
    "source": "device",
    "error_message": null
  },
  "tags": []
}
```

这里 tags 仅为结构示意；真实结果包含 EPC、天线、读取次数、RSSI 和设备时间。
200：存在新鲜、有效观测且设备连接标志正常。正常空读也返回 200，epc_list=[]。
503：没有观测、最新观测失败、已过期或设备已断开。已有观测时仍返回快照用于页面诊断，不能据此办理业务。
只适合设备状态和实时展示。入库核验用下面的新扫描接口，不复用点击前的最新读数。

## POST /scans

无请求正文（不要发送 `{}`），无需传 EPC 或数据库字段。
返回 202，JSON 包含 scan_id，Location 指向 `/scans/{scan_id}`。

服务只允许一个待完成扫描。409 表示设备已有任务；不自动覆盖或合并别人的请求。
创建任务时若后台已经开始一轮采集，此轮结果不会分配给新任务，新任务等待下一轮。
当前轮询间隔也会影响排队时间。服务不会为了 HTTP 请求并发调用 SDK。
首次连接、重连或解析失败会使本任务失败；后台仍会按原间隔重连，可在恢复后申请新任务。
最多保留 256 个任务；达到容量返回 429，等待过期记录清理。

## GET /scans/{scan_id}

已存在任务返回 200。必须检查 JSON 中的 status 和 valid，而不只是 HTTP 状态。

| status | 含义 |
|---|---|
| queued | 等待新一轮采集 |
| running | 已开始该轮采集 |
| completed | 本轮有效完成，可能正常读到零张标签 |
| failed | 连接、命令、解析错误，或者任务超时/服务停止 |

主要字段：scan_id、source、reader_ip、antenna_sequence、requested_at、started_at、finished_at、deadline_at、valid、error_message、observation、tags。
排队和运行中 valid=false，observation=null；失败时 observation 可能为 null。
任务超时后迟到的成功响应不会将其改成成功。SDK 同步调用不支持强制安全取消，
运行中任务即使已经标记超时，scan_busy 也要等 SDK 返回才解除。
服务重启会清空内存任务；不存在、已清理或重启前的任务返回 404。
保存结果用于审计不代表仍可办理入库，后端必须检查 finished_at 距现在是否超过业务有效期。

## 通用错误

| 状态码 | 含义 |
|---|---|
| 400 | 请求带了不支持的正文 |
| 401 | 缺少或错误的 X-RFID-Token |
| 403 | 带 Origin 的请求，不允许浏览器直接调用 |
| 404 | 未知路径或不存在的任务 |
| 405 | 方法错误，参见 Allow 响应头 |
| 409 | 已有任务未释放 |
| 429 | 内存任务记录达到上限 |
| 503 | 没有可用观测、设备异常、过期，或服务停止 |

## Python 调用示例

下面是后续接入 Flask 的调用示例，本次未加入 museum_app。仅使用 Python 标准库。
RFID_API_TOKEN 应与 C# 启动时使用/显示的密钥相同。

```python
import json
import os
import time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

BASE = "http://127.0.0.1:5080"
TOKEN = os.environ["RFID_API_TOKEN"]
opener = build_opener(ProxyHandler({}))  # 本机服务不经过系统代理

def rfid_request(method, path):
    req = Request(BASE + path, method=method,
                  headers={"X-RFID-Token": TOKEN})
    try:
        with opener.open(req, timeout=3) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"RFID HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("RFID 服务无法访问，请检查采集程序") from exc

# 此示例用于命令行联调；不要在 Flask 请求中照搬整个等待循环。
task = rfid_request("POST", "/scans")
scan_id = task["scan_id"]
deadline = time.monotonic() + 100
while time.monotonic() < deadline:
    result = rfid_request("GET", "/scans/" + scan_id)
    if result["status"] == "failed":
        raise RuntimeError(result["error_message"])
    if result["status"] == "completed":
        if not result["valid"] or result["source"] != "device":
            raise RuntimeError("不是有效的实机扫描")
        print(result["observation"]["epc_list"])
        break
    time.sleep(0.5)
else:
    raise TimeoutError("等待扫描结果超时；不要自动重复创建任务")
```

Flask 集成时应将“创建扫描”和“查询扫描”拆成两个快速路由。页面轮询 Flask，
避免一次网页请求挂起 90 秒。只有 C# 采集线程等待硬件响应。

## 后续接入 museum_app 时必须处理

1. 真机模式关闭/隔离原 `_do_read()` 的模拟读数，不混入真实观测；不要在 Flask 调试重载时启动多个采集进程。
2. 后端记录 scan_id 与当前登录用户、入库操作的关系。采集服务不知道业务用户，不负责授权。
3. 提交入库时由后端重新获取该 scan_id，验证 completed、valid、source、finished_at 新鲜度及目标 EPC。
4. 不相信浏览器提交的 EPC 列表；多标签时要求明确选择/核验，不取第一张。
5. 扫描成功只说明读到标签，不会自动写库存。数据库更新和流水写入仍由原 Flask 事务完成。
6. 保存 observation_id/scan_id 与业务记录关系；幂等入库请求防止重复提交，不在采集服务消费任务。
7. API 返回的是观测批次，不能只插入 EPC 明细后丢弃空读、失败、时间及来源信息。

## 已验证与待验证

- `--self-test`：解析、前导零、去重、空读、故障、新任务、排队、互斥、过期、迟到响应、任务清理。
- `--http-self-test`：本机 HTTP 认证、方法、正文、来源限制、任务创建/查询、设备忙、最新观测。
- 两类测试均不连接 Reader；实机命令返回格式、扫描耗时、多天线覆盖及断线恢复必须在实验课验证。
- 程序没有持久化任务队列，重启后任务只能从日志审计，不能继续通过旧 scan_id 查询。
