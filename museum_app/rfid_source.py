"""RFID 数据源适配：保留 mock，device 只接受 C# 服务的真实、新鲜观测。

模式和代次保存在 SQLite 中。切换递增代次，让切换前的在途 HTTP 响应失效。
使用独立批次表，不覆盖旧 EPC 明细，也能正确表达空读和采集失败。
"""
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError


class RfidError(Exception):
    pass


def ensure_schema(conn):
    """增量建表，无 DROP/DELETE，不要求重建用户数据库。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rfid_source_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            mode TEXT NOT NULL CHECK(mode IN ('mock','device')),
            generation INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO rfid_source_state(id,mode,generation) VALUES(1,'mock',0);
        CREATE TABLE IF NOT EXISTS rfid_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL REFERENCES rfid_devices(id),
            source TEXT NOT NULL CHECK(source IN ('mock','device')),
            generation INTEGER NOT NULL,
            observation_id TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            valid INTEGER NOT NULL,
            epc_list TEXT NOT NULL,
            error_message TEXT,
            UNIQUE(device_id, source, generation, observation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rfid_batch_current
        ON rfid_batches(device_id,source,generation,id);
    """)


def source_state(conn):
    return dict(conn.execute('SELECT mode,generation FROM rfid_source_state WHERE id=1').fetchone())


def normalize_epc(epc):
    return ''.join(epc.split()).upper()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp_age(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('时间缺少时区')
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except (ValueError, TypeError, AttributeError) as exc:
        raise RfidError('采集时间格式错误') from exc


def read_device(config):
    """只取真实观测；任何错误均抛出，调用者不能静默调用模拟器兜底。"""
    token = config.get('RFID_API_TOKEN')
    if not token:
        raise RfidError('未配置 RFID_API_TOKEN，请与 C# 采集服务使用相同密钥后重启网页服务')
    request = Request(config['RFID_API_URL'].rstrip('/') + '/observations/latest',
                      headers={'X-RFID-Token': token})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=config['RFID_HTTP_TIMEOUT']) as response:
            data = json.load(response)
    except HTTPError as exc:
        # 不回显未经筛选的响应正文，避免泄漏密钥或远端 HTML。
        raise RfidError(f'RFID 服务返回 HTTP {exc.code}，请检查密钥、设备连接和观测是否过期') from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RfidError('RFID 服务无法访问或响应格式错误，请启动 RFID_reader') from exc
    return validate_device(data, config['RFID_MAX_AGE_SECONDS'])


def validate_device(data, max_age):
    if not isinstance(data, dict) or data.get('stale') is not False:
        raise RfidError('真实观测缺失或已经过期')
    obs = data.get('observation')
    if not isinstance(obs, dict) or obs.get('source') != 'device' or obs.get('valid') is not True:
        raise RfidError('没有有效的真实设备观测，未回退到模拟数据')
    if not isinstance(obs.get('observation_id'), str) or not obs['observation_id']:
        raise RfidError('真实观测缺少唯一编号')
    age = timestamp_age(obs.get('collected_at'))
    if age < -2 or age > max_age:
        raise RfidError('真实观测过期或设备服务电脑时间异常')
    epcs = obs.get('epc_list')
    if not isinstance(epcs, list):
        raise RfidError('真实 EPC 列表格式错误')
    normalized = []
    for epc in epcs:
        if not isinstance(epc, str):
            raise RfidError('真实 EPC 必须为字符串')
        epc = normalize_epc(epc)
        if not re.fullmatch(r'(?:[0-9A-F]{2})+', epc):
            raise RfidError('真实 EPC 不是有效十六进制编码')
        normalized.append(epc)
    return dict(obs, epc_list=sorted(set(normalized)))


def store_batch(conn, dev_id, state, epcs, valid=True, error=None, observation=None):
    obs_id = observation['observation_id'] if observation else 'obs_' + uuid.uuid4().hex
    stamp = observation['collected_at'] if observation else utc_now()
    conn.execute("""INSERT OR IGNORE INTO rfid_batches
        (device_id,source,generation,observation_id,collected_at,valid,epc_list,error_message)
        VALUES(?,?,?,?,?,?,?,?)""",
        (dev_id, state['mode'], state['generation'], obs_id, stamp, int(valid),
         json.dumps(sorted(set(epcs)) if valid else []), error))


def latest_batch(conn, dev_id, state):
    return conn.execute("""SELECT * FROM rfid_batches WHERE device_id=? AND source=?
        AND generation=? ORDER BY id DESC LIMIT 1""",
        (dev_id, state['mode'], state['generation'])).fetchone()
