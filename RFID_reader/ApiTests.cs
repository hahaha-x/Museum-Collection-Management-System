using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Web.Script.Serialization;

namespace Museum.Rfid
{
    // 测试只注入固定数据，不实例化 clsReader；没有读写器时也能执行。
    internal static class ApiTests
    {
        private static void Check(bool value, string name)
        { if (!value) throw new Exception("接口自测失败：" + name); }
        private static Dictionary<string, object> Body(ApiReply r)
        { return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(r.Json); }
        private static string NewScan(ReaderState state)
        {
            var r = state.Request("POST", "/scans");
            Check(r.Code == 202, "创建任务");
            return (string)Body(r)["scan_id"];
        }
        private static ObservationRecord Observation(bool valid, params string[] epcs)
        {
            return new ObservationRecord {
                observation_id = "obs_test", collected_at = DateTime.UtcNow.ToString("o"),
                valid = valid, epc_list = epcs, source = "device", error_message = valid ? null : "test disconnect"
            };
        }

        public static void Run()
        {
            DateTime now = new DateTime(2026, 10, 16, 0, 0, 0, DateTimeKind.Utc);
            var state = new ReaderState("192.168.1.100", "3", 5000, 10000, 60000, () => now);
            Check(state.Request("GET", "/observations/latest").Code == 503, "未采集不得成功");
            Check(state.Request("GET", "/scans").Code == 405, "方法限制");
            Check(state.Request("GET", "/missing").Code == 404, "未知路径");

            // 已经在采集的一轮不能被请求事后认领。
            Check(state.BeginRound() == null, "普通轮询");
            string id = NewScan(state);
            Check(state.Request("POST", "/scans").Code == 409, "单任务互斥");
            state.SetConnected(true);
            state.FinishRound(null, Observation(true, "0011"), new List<TagRecord>());
            Check((string)Body(state.Request("GET", "/scans/" + id))["status"] == "queued", "不复用旧轮次");
            Check(state.BeginRound() == id, "下一轮领取任务");
            state.FinishRound(id, Observation(true), new List<TagRecord>());
            var completed = Body(state.Request("GET", "/scans/" + id));
            Check((string)completed["status"] == "completed" && (bool)completed["valid"], "空读有效完成");
            Check(!completed.ContainsKey("deadline") && !completed.ContainsKey("expires"), "不泄漏内部时间字段");
            Check(state.Request("GET", "/observations/latest").Code == 200, "新鲜读数");
            now = now.AddMilliseconds(5001);
            Check(state.Request("GET", "/observations/latest").Code == 503, "过期读数拒绝");

            string failed = NewScan(state);
            state.BeginRound(); state.SetConnected(false);
            state.FinishRound(failed, Observation(false), new List<TagRecord>());
            Check(!(bool)Body(state.Request("GET", "/scans/" + failed))["valid"], "故障无效");
            Check(state.Request("GET", "/observations/latest").Code == 503, "故障最新读数拒绝");

            string timed = NewScan(state);
            state.BeginRound(); now = now.AddSeconds(11);
            Check((string)Body(state.Request("GET", "/scans/" + timed))["status"] == "failed", "运行中超时");
            Check(state.Request("POST", "/scans").Code == 409, "SDK 未结束时不重复执行");
            state.FinishRound(timed, Observation(true, "0011"), new List<TagRecord>());
            Check(!(bool)Body(state.Request("GET", "/scans/" + timed))["valid"], "迟到成功不改写超时");

            string queued = NewScan(state);
            now = now.AddSeconds(11);
            Check((string)Body(state.Request("GET", "/scans/" + queued))["status"] == "failed", "排队超时");
            Check(state.BeginRound() == null, "排队过期后不执行");
            now = now.AddMinutes(2);
            Check(state.Request("GET", "/scans/" + id).Code == 404, "清理历史任务");
            string stopping = NewScan(state);
            state.Shutdown();
            Check((string)Body(state.Request("GET", "/scans/" + stopping))["status"] == "failed", "停止时失败");
            Check(state.Request("POST", "/scans").Code == 503, "停止后拒绝任务");
            Console.WriteLine("接口状态自测通过：新扫描、互斥、空读、故障、过期、超时和停止。");
        }

        // 独立可选的 HTTP 集成测试：使用本机临时端口，不访问实验设备。
        public static void Http()
        {
            var reservation = new TcpListener(IPAddress.Loopback, 0);
            reservation.Start();
            int port = ((IPEndPoint)reservation.LocalEndpoint).Port;
            reservation.Stop();
            const string token = "offline-http-test-token";
            var state = new ReaderState("offline-test", "3", 5000, 10000, 60000, () => DateTime.UtcNow);
            using (var api = new RfidApi(port, token, state))
            {
                api.Start();
                string root = "http://127.0.0.1:" + port;
                Check(Call(root + "/health", "GET", null).Code == 401, "HTTP 认证");
                Check(Call(root + "/health", "GET", token).Code == 200, "HTTP 健康状态");
                Check(Call(root + "/observations/latest", "GET", token).Code == 503, "HTTP 无数据");
                Check(Call(root + "/scans", "GET", token).Code == 405, "HTTP 方法限制");
                Check(Call(root + "/scans", "POST", token, "{}").Code == 400, "HTTP 拒绝未支持正文");
                Check(Call(root + "/health", "GET", token, null, "http://example.test").Code == 403, "HTTP 拒绝浏览器跨域");
                var accepted = Call(root + "/scans", "POST", token);
                Check(accepted.Code == 202, "HTTP 创建扫描 " + accepted.Code + " " + accepted.Json);
                string id = (string)Body(accepted)["scan_id"];
                Check(Call(root + "/scans", "POST", token).Code == 409, "HTTP 设备忙");
                Check(state.BeginRound() == id, "HTTP 任务领取");
                state.SetConnected(true);
                state.FinishRound(id, Observation(true, "0011223344556691"), new List<TagRecord>());
                var scan = Call(root + "/scans/" + id, "GET", token);
                Check(scan.Code == 200 && (bool)Body(scan)["valid"], "HTTP 读取结果");
                Check(Call(root + "/observations/latest", "GET", token).Code == 200, "HTTP 最新数据");
                Check(Call(root + "/scans/nonexistent", "GET", token).Code == 404, "HTTP 未知任务");
            }
            Console.WriteLine("HTTP 集成自测通过（本机注入数据，未连接硬件）。");
        }

        private static ApiReply Call(string url, string method, string token, string body = null, string origin = null)
        {
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = method; request.Proxy = null; request.Timeout = 3000;
            if (method == "POST" && body == null) request.ContentLength = 0;
            if (token != null) request.Headers["X-RFID-Token"] = token;
            if (origin != null) request.Headers["Origin"] = origin;
            if (body != null)
            {
                byte[] data = Encoding.UTF8.GetBytes(body); request.ContentLength = data.Length;
                using (var stream = request.GetRequestStream()) stream.Write(data, 0, data.Length);
            }
            HttpWebResponse response;
            try { response = (HttpWebResponse)request.GetResponse(); }
            catch (WebException ex) { if (ex.Response == null) throw; response = (HttpWebResponse)ex.Response; }
            using (response)
            using (var reader = new StreamReader(response.GetResponseStream()))
                return new ApiReply((int)response.StatusCode, reader.ReadToEnd());
        }
    }
}
