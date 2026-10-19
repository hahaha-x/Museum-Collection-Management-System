using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace Museum.Rfid
{
    // 业务层与硬件之间的内存状态。所有访问在同一把锁下完成，返回 JSON 快照，
    // 避免 HTTP 序列化期间被采集线程修改。HTTP 线程永远不调用 Alien SDK。
    internal sealed class ReaderState
    {
        private readonly object gate = new object();
        private readonly Dictionary<string, ScanRecord> scans = new Dictionary<string, ScanRecord>();
        private readonly Func<DateTime> clock;
        private readonly int freshMs, timeoutMs, retentionMs;
        private readonly string readerIp, antennas;
        private string activeId;
        private bool connected, reading, stopped;
        private DateTime roundStarted, latestAt;
        private ObservationRecord latest;
        private List<TagRecord> latestTags = new List<TagRecord>();

        public ReaderState(string ip, string antennaSequence, int freshness, int timeout, int retention, Func<DateTime> now)
        {
            readerIp = ip; antennas = antennaSequence;
            freshMs = freshness; timeoutMs = timeout; retentionMs = retention; clock = now;
        }

        public void SetConnected(bool value) { lock (gate) { connected = value; } }

        public string BeginRound()
        {
            lock (gate)
            {
                Expire();
                reading = true;
                roundStarted = clock();
                if (activeId == null) return null;
                ScanRecord scan = scans[activeId];
                if (scan.status != "queued") { activeId = null; return null; }
                scan.status = "running";
                scan.started_at = Stamp(roundStarted);
                return scan.scan_id;
            }
        }

        public void FinishRound(string id, ObservationRecord observation, List<TagRecord> tags)
        {
            lock (gate)
            {
                Expire();
                reading = false;
                latest = observation;
                latestTags = tags;
                latestAt = clock();
                latestStarted = Stamp(roundStarted);
                if (id == null) return; // 本轮开始后才创建的任务，必须留到下一轮。
                ScanRecord scan = scans[id];
                // 超时任务不可因迟到的 SDK 响应重新变成成功。
                if (scan.status == "running")
                {
                    scan.finished_at = Stamp(clock());
                    scan.status = observation.valid ? "completed" : "failed";
                    scan.valid = observation.valid;
                    scan.observation = observation;
                    scan.tags = tags;
                    scan.error_message = observation.error_message;
                }
                activeId = null;
            }
        }

        public void Shutdown()
        {
            lock (gate)
            {
                stopped = true; connected = false; reading = false;
                if (activeId != null) Fail(scans[activeId], "服务正在停止。");
            }
        }

        public ApiReply Request(string method, string path)
        {
            lock (gate)
            {
                Expire();
                if (path == "/health")
                {
                    if (method != "GET") return MethodError("GET");
                    return Reply(200, new {
                        service_status = stopped ? "stopping" : "running",
                        source = "device", reader_ip = readerIp, antenna_sequence = antennas,
                        reader_connected = connected, collecting = reading, scan_busy = activeId != null,
                        last_observation_at = latest == null ? null : latest.collected_at,
                        last_observation_valid = latest != null && latest.valid,
                        stale = IsStale(), last_error = latest == null ? null : latest.error_message
                    });
                }
                if (path == "/observations/latest")
                {
                    if (method != "GET") return MethodError("GET");
                    if (latest == null) return Reply(503, new { error = "no_observation", message = "尚未完成首轮采集。" });
                    bool stale = IsStale();
                    return Reply(stale || !latest.valid || !connected ? 503 : 200, new {
                        stale = stale, age_ms = Math.Max(0, (clock() - latestAt).TotalMilliseconds),
                        started_at = latestStarted, observation = latest, tags = latestTags
                    });
                }
                if (path == "/scans")
                {
                    if (method != "POST") return MethodError("POST");
                    if (stopped) return Reply(503, new { error = "stopping" });
                    if (activeId != null) return Reply(409, new { error = "reader_busy", message = "已有扫描任务，请等待其结束。" });
                    if (scans.Count >= 256) return Reply(429, new { error = "scan_capacity", message = "扫描记录已满，请等待过期清理。" });
                    string id = "scan_" + Guid.NewGuid().ToString("N");
                    DateTime now = clock();
                    var scan = new ScanRecord {
                        scan_id = id, status = "queued", source = "device", requested_at = Stamp(now),
                        deadline = now.AddMilliseconds(timeoutMs), deadline_at = Stamp(now.AddMilliseconds(timeoutMs)),
                        expires = now.AddMilliseconds(timeoutMs + retentionMs),
                        reader_ip = readerIp, antenna_sequence = antennas
                    };
                    scans.Add(id, scan); activeId = id;
                    return Reply(202, scan, "/scans/" + id);
                }
                if (path.StartsWith("/scans/", StringComparison.Ordinal))
                {
                    if (method != "GET") return MethodError("GET");
                    ScanRecord scan;
                    if (!scans.TryGetValue(path.Substring(7), out scan))
                        return Reply(404, new { error = "scan_not_found", message = "任务不存在、已过期，或服务已重启。" });
                    return Reply(200, scan);
                }
                return Reply(404, new { error = "not_found" });
            }
        }

        private string latestStarted;
        private bool IsStale() { return latest == null || (clock() - latestAt).TotalMilliseconds > freshMs; }
        private void Expire()
        {
            DateTime now = clock();
            if (activeId != null)
            {
                var scan = scans[activeId];
                if ((scan.status == "queued" || scan.status == "running") && now >= scan.deadline)
                {
                    bool queued = scan.status == "queued";
                    Fail(scan, "扫描超时；同步设备调用可能仍在结束中。");
                    // 正在运行的任务保留 busy，直到 SDK 返回，不并发启动第二个硬件操作。
                    if (queued) activeId = null;
                }
            }
            foreach (string id in scans.Where(p => p.Key != activeId && now >= p.Value.expires).Select(p => p.Key).ToArray())
                scans.Remove(id);
        }
        private void Fail(ScanRecord scan, string reason)
        {
            scan.status = "failed"; scan.valid = false; scan.error_message = reason; scan.finished_at = Stamp(clock());
        }
        private static string Stamp(DateTime time) { return time.ToString("o", CultureInfo.InvariantCulture); }
        private static ApiReply MethodError(string allowed) { return new ApiReply(405, "{\"error\":\"method_not_allowed\"}", null, allowed); }
        private static ApiReply Reply(int code, object value, string location = null)
        {
            return new ApiReply(code, new JavaScriptSerializer().Serialize(value), location, null);
        }
    }

    internal sealed class ScanRecord
    {
        public string scan_id, status, source, reader_ip, antenna_sequence;
        public string requested_at, started_at, finished_at, deadline_at, error_message;
        public bool valid;
        public ObservationRecord observation;
        public List<TagRecord> tags = new List<TagRecord>();
        [ScriptIgnore] public DateTime deadline;
        [ScriptIgnore] public DateTime expires;
    }

    internal sealed class ApiReply
    {
        public readonly int Code;
        public readonly string Json, Location, Allow;
        public ApiReply(int code, string json, string location = null, string allow = null)
        { Code = code; Json = json; Location = location; Allow = allow; }
    }

    internal sealed class RfidApi : IDisposable
    {
        private readonly HttpListener listener = new HttpListener();
        private readonly string token, prefix;
        private Thread server;
        public readonly ReaderState State;

        public RfidApi(Settings s) : this(s.ApiPort, s.ApiToken,
            new ReaderState(s.Ip, s.Antennas, s.FreshnessMs, s.ScanTimeoutMs, s.ScanRetentionMs, () => DateTime.UtcNow)) { }

        internal RfidApi(int port, string apiToken, ReaderState state)
        {
            token = apiToken; State = state;
            // 不用 + 或 * 前缀，不把硬件控制接口暴露到校园网。
            prefix = "http://127.0.0.1:" + port + "/";
            listener.Prefixes.Add(prefix);
        }

        public void Start()
        {
            try { listener.Start(); }
            catch (HttpListenerException ex)
            {
                throw new InvalidOperationException("HTTP 监听失败：" + prefix + "。检查端口占用及 README 中的 URL 访问权限说明。", ex);
            }
            Console.WriteLine("RFID API: " + prefix);
            Console.WriteLine("本机调用密钥（仅交给可信后端，勿放入网页）：" + token);
            server = new Thread(Serve) { IsBackground = true, Name = "RFID HTTP" };
            server.Start();
        }

        private void Serve()
        {
            while (listener.IsListening)
            {
                HttpListenerContext context;
                try { context = listener.GetContext(); }
                catch (HttpListenerException) { break; }
                catch (ObjectDisposedException) { break; }
                // 只处理小型内存快照，不等待硬件，也不读取请求正文。
                try
                {
                    ApiReply reply;
                    if (!string.Equals(context.Request.Headers["X-RFID-Token"], token, StringComparison.Ordinal))
                        reply = new ApiReply(401, "{\"error\":\"unauthorized\"}");
                    else if (!string.IsNullOrEmpty(context.Request.Headers["Origin"]))
                        reply = new ApiReply(403, "{\"error\":\"backend_only\"}");
                    else if (context.Request.HasEntityBody)
                        reply = new ApiReply(400, "{\"error\":\"body_not_supported\"}");
                    else
                        reply = State.Request(context.Request.HttpMethod, context.Request.Url.AbsolutePath.TrimEnd('/'));
                    context.Response.StatusCode = reply.Code;
                    // 错误请求可能带未读取的正文，关闭连接避免污染下一次请求。
                    context.Response.KeepAlive = false;
                    context.Response.ContentType = "application/json; charset=utf-8";
                    context.Response.Headers["Cache-Control"] = "no-store";
                    if (reply.Location != null) context.Response.Headers["Location"] = reply.Location;
                    if (reply.Allow != null) context.Response.Headers["Allow"] = reply.Allow;
                    byte[] bytes = Encoding.UTF8.GetBytes(reply.Json);
                    context.Response.ContentLength64 = bytes.Length;
                    context.Response.OutputStream.Write(bytes, 0, bytes.Length);
                }
                catch (Exception ex) { Console.Error.WriteLine("HTTP 请求处理失败：" + ex.Message); }
                finally { try { context.Response.Close(); } catch { } }
            }
        }

        public void Dispose()
        {
            State.Shutdown();
            listener.Close();
            if (server != null) server.Join(2000);
        }
    }
}
