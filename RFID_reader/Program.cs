using System;
using System.Collections.Generic;
using System.Configuration;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using nsAlienRFID2;

namespace Museum.Rfid
{
    internal static class Program
    {
        // Ctrl+C 只发出停止请求，不在控制台事件线程上操作 SDK，避免跨线程竞争。
        private static readonly ManualResetEvent Stop = new ManualResetEvent(false);
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        private static int Main(string[] args)
        {
            Console.OutputEncoding = Encoding.UTF8;
            Console.CancelKeyPress += (sender, e) => { e.Cancel = true; Stop.Set(); };
            try
            {
                // 自测完全离线，不创建读写器连接、不发射射频信号。
                if (args.Length == 1 && args[0] == "--self-test") { SelfTest(); return 0; }
                if (args.Length == 1 && args[0] == "--http-self-test") { ApiTests.Http(); return 0; }
                if (args.Length != 0) throw new ArgumentException("仅支持 --self-test 或 --http-self-test，连接参数请修改 App.config。");
                var settings = new Settings();
                Directory.CreateDirectory(settings.OutputDirectory);
                Console.WriteLine("连接 {0}:{1}，天线 {2}。请先断开 Gateway；Ctrl+C 停止。", settings.Ip, settings.Port, settings.Antennas);
                Console.WriteLine("输出目录：" + settings.OutputDirectory);
                // HTTP 请求线程不直接操作 SDK；唯一采集循环负责设备连接和所有读写。
                // 先启动监听，若端口占用或权限不足则退出，不继续占用硬件。
                using (var api = new RfidApi(settings))
                {
                    api.Start();
                    Run(settings, api.State);
                }
                return 0;
            }
            catch (Exception ex)
            {
                // 配置或磁盘写入失败必须退出，不能继续读数却让用户误以为已保存。
                Console.Error.WriteLine("程序停止：" + ex.Message);
                return 1;
            }
        }

        private static void Run(Settings s, ReaderState state)
        {
            clsReader reader = null;
            try
            {
                while (!Stop.WaitOne(0))
                {
                    // 只在新一轮开始时领取任务。请求到达前已经开始的读数不能用于该任务。
                    string scanId = state.BeginRound();
                    string raw = null;
                    List<TagRecord> tags = new List<TagRecord>();
                    string error = null;
                    try
                    {
                        if (reader == null)
                        {
                            reader = new clsReader();
                            reader.InitOnNetwork(s.Ip, s.Port);
                            reader.Connect();
                            if (!reader.Login(s.Username, s.Password))
                                throw new InvalidOperationException("登录失败，请核对读写器账户和密码。");

                            // 使用主机轮询模式。下列参数会修改设备当前工作状态，但不执行 Save。
                            // 不改 IP、功率、频段，也不写入/锁定/销毁标签。
                            reader.AutoMode = "OFF";
                            reader.AntennaSequence = s.Antennas;
                            reader.TagListFormat = "Text";
                            reader.TagListAntennaCombine = "OFF";
                            state.SetConnected(true);
                            Console.WriteLine("已连接并登录，开始采集。");
                        }

                        // 每轮先清掉历史列表，再执行 Get TagList（手动模式下触发采集）。
                        // 因此这一轮的空结果不会被上一次的缓存标签掩盖。
                        reader.ClearTagList();
                        raw = reader.TagList;
                        tags = ParseTags(raw);
                    }
                    catch (Exception ex)
                    {
                        // 网络、登录、设备命令或解析失败均是无效观测，绝不能当作正常“零标签”。
                        error = ex.Message;
                        state.SetConnected(false);
                        Close(ref reader);
                        Console.Error.WriteLine("采集失败，将重试：" + error);
                    }

                    var observation = Observation(tags, error);
                    // 业务观测文件严格保持根 README 里的统一契约；详情放在另一份文件。
                    // 写入在网络异常处理之外：磁盘错误直接终止，避免无限重连掩盖磁盘问题。
                    Append(s.OutputDirectory, "observations", observation);
                    Append(s.OutputDirectory, "details", new {
                        scan_id = scanId,
                        observation_id = observation.observation_id,
                        collected_at = observation.collected_at,
                        reader_ip = s.Ip,
                        antenna_sequence = s.Antennas,
                        tags = tags,
                        raw_response = raw,
                        error_message = error
                    });
                    // 先完成日志写入再发布；保存失败不会把未保存的扫描当作成功提交。
                    state.FinishRound(scanId, observation, tags);
                    if (error == null)
                    {
                        Console.WriteLine("[{0:HH:mm:ss}] 本轮 {1} 个不同 EPC", DateTime.Now, observation.epc_list.Length);
                        foreach (var tag in tags)
                            Console.WriteLine("  EPC={0}  天线={1}  次数={2}  RSSI={3}", tag.epc, tag.antenna, tag.read_count, tag.rssi);
                    }
                    // 可中断等待；正在执行的同步 SDK 命令仍须等其完成/超时才会退出。
                    if (Stop.WaitOne(error == null ? s.PollMs : s.RetryMs)) break;
                }
            }
            finally { state.Shutdown(); Close(ref reader); }
        }

        private static void Close(ref clsReader reader)
        {
            if (reader == null) return;
            try { reader.Disconnect(); }
            catch (Exception ex) { Console.Error.WriteLine("断开连接时：" + ex.Message); }
            finally { reader = null; }
        }

        internal static List<TagRecord> ParseTags(string raw)
        {
            // 只有设备明确返回 No Tags 才认定为空；空字符串/错误消息均不可误报成功。
            if (raw == null) throw new FormatException("读写器返回 null。");
            string text = raw.Trim('\0', '\r', '\n', ' ', '\t');
            if (string.Equals(text, "(No Tags)", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(text, "No Tags", StringComparison.OrdinalIgnoreCase))
                return new List<TagRecord>();
            if (text.Length == 0) throw new FormatException("读写器返回空响应。");

            // 老 SDK 的解析器未能识别本机截图中的 EPC 格式，因此按字段名解析 Text。
            // Gateway 显示 EPC:，实机 SDK 回复可能是 Tag:，两者均表示 EPC。
            // D1 等额外字段可忽略，原文仍保存在日志；缺少必要字段不能当作空读。
            var result = new List<TagRecord>();
            foreach (string line in text.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var fields = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                foreach (string part in line.Split(','))
                {
                    int colon = part.IndexOf(':');
                    if (colon < 1) throw new FormatException("无法解析标签字段：" + part);
                    string key = part.Substring(0, colon).Trim();
                    if (key.Length == 0 || fields.ContainsKey(key))
                        throw new FormatException("标签字段名为空或重复：" + key);
                    fields.Add(key, part.Substring(colon + 1).Trim());
                }
                string epcText, tagText;
                bool hasEpc = fields.TryGetValue("EPC", out epcText);
                bool hasTag = fields.TryGetValue("Tag", out tagText);
                if (!hasEpc && !hasTag)
                    throw new FormatException("标签记录缺少 EPC 或 Tag 字段；实际字段：" + string.Join(", ", fields.Keys));
                string epc = NormalizeEpc(hasEpc ? epcText : tagText);
                if (hasEpc && hasTag && epc != NormalizeEpc(tagText))
                    throw new FormatException("同一标签记录的 EPC 与 Tag 字段不一致。");
                // EPC 是十六进制文本，必须保留开头的 0；不可转换成整数再保存。
                if (epc.Length == 0 || epc.Length % 2 != 0 || !Regex.IsMatch(epc, "^[0-9A-F]+$"))
                    throw new FormatException("收到无效 EPC。");
                double? rssi = null;
                if (fields.ContainsKey("RSSI"))
                {
                    double value;
                    if (!double.TryParse(RequiredField(fields, "RSSI"), NumberStyles.Float,
                        CultureInfo.InvariantCulture, out value) || double.IsNaN(value) || double.IsInfinity(value))
                        throw new FormatException("RSSI 字段不是有效数值。");
                    rssi = value;
                }
                result.Add(new TagRecord {
                    epc = epc, antenna = IntegerField(fields, "Ant", 0, 3),
                    read_count = IntegerField(fields, "Count", 0, int.MaxValue),
                    rssi = rssi,
                    reader_first_seen = RequiredField(fields, "Disc"), reader_last_seen = RequiredField(fields, "Last")
                });
            }
            // 详情保留不同天线记录；业务 epc_list 在 Observation 内按 EPC 去重。
            return result;
        }

        private static string NormalizeEpc(string value)
        {
            return Regex.Replace(value ?? "", @"\s+", "").ToUpperInvariant();
        }

        private static string RequiredField(Dictionary<string, string> fields, string name)
        {
            string value;
            if (!fields.TryGetValue(name, out value) || string.IsNullOrWhiteSpace(value))
                throw new FormatException("标签记录缺少必要字段或值为空：" + name);
            return value;
        }

        private static int IntegerField(Dictionary<string, string> fields, string name, int min, int max)
        {
            int value;
            if (!int.TryParse(RequiredField(fields, name), NumberStyles.Integer, CultureInfo.InvariantCulture, out value)
                || value < min || value > max)
                throw new FormatException("标签字段 " + name + " 不是允许范围内的整数。");
            return value;
        }

        private static ObservationRecord Observation(List<TagRecord> tags, string error)
        {
            return new ObservationRecord {
                observation_id = "obs_" + Guid.NewGuid().ToString("N"),
                // 截图中设备时间为 1970 年，不能用于业务时间；统一采用 PC 的 UTC 时间。
                collected_at = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                valid = error == null,
                epc_list = error == null ? tags.Select(t => t.epc).Distinct().OrderBy(x => x).ToArray() : new string[0],
                source = "device", error_message = error
            };
        }

        private static void Append(string directory, string prefix, object record)
        {
            // JSONL 每行一个完整 JSON 对象，每天一个文件；重启追加，不覆盖已有数据。
            string path = Path.Combine(directory, prefix + "-" + DateTime.UtcNow.ToString("yyyyMMdd") + ".jsonl");
            File.AppendAllText(path, Json.Serialize(record) + Environment.NewLine, new UTF8Encoding(false));
        }

        private static void SelfTest()
        {
            const string first = "EPC:5348 0000 0001 0000 0000 0001, Disc:1970/01/01 10:05:40.020, Last:1970/01/01 10:05:40.020, Count:1, Ant:3, Proto:2, RSSI:4183.4";
            const string second = "EPC:0011 2233 4455 6691, Disc:1970/01/01 10:05:51.284, Last:1970/01/01 10:05:51.284, Count:1, Ant:3, Proto:2, RSSI:5816.1";
            var tags = ParseTags(first + "\r\n" + second + "\r\n" + first);
            Require(tags.Count == 3 && tags[1].epc == "0011223344556691" && tags[0].antenna == 3, "解析 EPC、前导零、天线");
            Require(Observation(tags, null).epc_list.Length == 2, "批次 EPC 去重");
            // 2026-10-19 实机日志回归：Tag 前缀、没有 RSSI、额外 D1 字段。
            const string actual = "Tag:E200 0000 0000 0000 0970 7110, Disc:1970/01/06 07:31:57.347, Last:1970/01/06 07:31:57.347, Count:1, Ant:3, Proto:2";
            var live = ParseTags(actual + ", D1:012E 0300 03B7 3DFC");
            Require(live.Count == 1 && live[0].epc == "E20000000000000009707110" && live[0].rssi == null,
                "实机 Tag 格式和 D1 扩展字段");
            Require(ParseTags(actual)[0].read_count == 1, "实机无 RSSI 格式");
            Require(ParseTags(actual.Replace("Tag:E200", "tag:e200"))[0].epc == live[0].epc, "字段和 EPC 大小写");
            Require(ParseTags(second.Replace("EPC:", "Tag:"))[0].epc == "0011223344556691", "Tag 前导零");
            Require(ParseTags(first + "\r\n" + second + "\r\n" + actual).Count == 3, "混合三标签格式");
            foreach (string badRecord in new[] {
                actual.Replace("Tag:", "Other:"), actual.Replace("Ant:3", "Other:3"),
                actual.Replace("Count:1", "Count:bad"), actual.Replace("Ant:3", "Ant:4"),
                actual.Replace("Disc:", "Other:"), actual.Replace("Last:", "Other:"),
                actual + ", Tag:0011", actual + ", EPC:0011", actual + ", RSSI:NaN" })
            {
                bool rejected = false;
                try { ParseTags(badRecord); }
                catch (FormatException) { rejected = true; }
                Require(rejected, "字段错误给出明确格式异常而非字典异常");
            }
            Require(Observation(ParseTags("(No Tags)"), null).valid, "正常空读");
            var failed = Observation(tags, "测试错误");
            Require(!failed.valid && failed.epc_list.Length == 0, "失败不能带成功 EPC");
            foreach (var bad in new[] { "", "Unknown command", "EPC:ZZZZ, Count:1, Ant:3" })
            {
                bool rejected = false;
                try { ParseTags(bad); } catch { rejected = true; }
                Require(rejected, "拒绝无效响应");
            }
            Require(Observation(tags, null).observation_id != Observation(tags, null).observation_id, "观测 ID 唯一");
            ApiTests.Run();
            Console.WriteLine("离线自测通过（未连接设备）。");
        }

        private static void Require(bool ok, string name)
        {
            if (!ok) throw new Exception("自测失败：" + name);
        }
    }

    // 小写字段名刻意与 museum 观测接口一致，便于后续对接。
    public sealed class ObservationRecord
    {
        public string observation_id;
        public string collected_at;
        public bool valid;
        public string[] epc_list;
        public string source;
        public string error_message;
    }

    public sealed class TagRecord
    {
        public string epc;
        public int antenna;
        public int read_count;
        public double? rssi; // 厂商原始无量纲值；缺失时为 null，不冒充 dBm。
        public string reader_first_seen;
        public string reader_last_seen;
    }

    internal sealed class Settings
    {
        public readonly string Ip = Value("ReaderIp");
        public readonly int Port = Number("ReaderPort", 1, 65535);
        public readonly string Username = Value("Username");
        public readonly string Password = Environment.GetEnvironmentVariable("RFID_PASSWORD") ?? Value("Password");
        public readonly string Antennas = Value("Antennas");
        public readonly int PollMs = Number("PollIntervalMs", 100, 3600000);
        public readonly int RetryMs = Number("ReconnectIntervalMs", 1000, 3600000);
        public readonly string OutputDirectory = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, Value("OutputDirectory")));
        public readonly int ApiPort = Number("ApiPort", 1024, 65535);
        public readonly int FreshnessMs = Number("FreshnessMs", 1000, 60000);
        public readonly int ScanTimeoutMs = Number("ScanTimeoutMs", 1000, 300000);
        public readonly int ScanRetentionMs = Number("ScanRetentionMs", 60000, 3600000);
        // 不写死访问密钥。可通过环境变量固定，否则每次启动生成新的密钥。
        public readonly string ApiToken = Environment.GetEnvironmentVariable("RFID_API_TOKEN") ?? Guid.NewGuid().ToString("N");

        public Settings()
        {
            if (ApiToken.Length < 16) throw new ConfigurationErrorsException("RFID_API_TOKEN 至少需要 16 个字符。");
            System.Net.IPAddress address;
            if (!System.Net.IPAddress.TryParse(Ip, out address) || address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
                throw new ConfigurationErrorsException("ReaderIp 必须是 IPv4 地址。");
            if (!Regex.IsMatch(Antennas, "^[0-3](,[0-3])*$"))
                throw new ConfigurationErrorsException("Antennas 应为 0～3 的逗号分隔编号，例如 3 或 0,1,2,3。");
        }
        private static string Value(string key)
        {
            string value = ConfigurationManager.AppSettings[key];
            if (string.IsNullOrWhiteSpace(value)) throw new ConfigurationErrorsException("配置缺少 " + key);
            return value.Trim();
        }
        private static int Number(string key, int min, int max)
        {
            int value;
            if (!int.TryParse(Value(key), out value) || value < min || value > max)
                throw new ConfigurationErrorsException(key + " 超出允许范围。");
            return value;
        }
    }
}
