-- ============================================================
-- 博物馆智能货架系统 - SQLite 建表脚本
-- 由 init_db.py 自动执行,也可用任何 SQLite 工具手动打开 museum.db 查看
-- 说明: 与 MySQL 版 schema.sql 结构一致,差异点:
--   ENUM -> TEXT + CHECK 约束; JSON -> TEXT; 时间为 TEXT('YYYY-MM-DD HH:MM:SS')
--   updated_at 自动更新用触发器实现; 外键需连接内开启 PRAGMA foreign_keys=ON
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- D1 藏品档案库 (collections)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_no TEXT NOT NULL UNIQUE,                -- 藏品编号(对外标识,不可修改)
  name          TEXT NOT NULL,                       -- 藏品名称
  category      TEXT NOT NULL,                       -- 分类(书画/陶瓷/青铜器等)
  era           TEXT,                                -- 年代
  origin        TEXT,                                -- 出土地点/来源
  photo_url     TEXT,                                -- 藏品照片路径
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TRIGGER IF NOT EXISTS trg_collections_updated
AFTER UPDATE ON collections
BEGIN
  UPDATE collections SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
END;

-- ------------------------------------------------------------
-- D5 人员权限库 (users)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_no      TEXT NOT NULL UNIQUE,             -- 工号(登录账号)
  password_hash    TEXT NOT NULL,                    -- 密码哈希(pbkdf2)
  name             TEXT NOT NULL,                    -- 姓名
  role             TEXT NOT NULL DEFAULT '馆藏管理员'
                     CHECK (role IN ('馆藏管理员','查看者','管理员')),
  phone            TEXT,                             -- 联系电话
  face_template_id TEXT,                             -- 人脸模板ID(人脸认证模块暂缓)
  permissions      TEXT,                             -- 允许的操作类型(JSON数组文本,空按角色默认)
  status           TEXT NOT NULL DEFAULT '启用'
                     CHECK (status IN ('启用','停用')),
  last_login_at    TEXT,                             -- 最后登录时间
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TRIGGER IF NOT EXISTS trg_users_updated
AFTER UPDATE ON users
BEGIN
  UPDATE users SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
END;

-- ------------------------------------------------------------
-- D2 库存状态库 (inventory_status)  与 collections 一对一
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_status (
  collection_id INTEGER PRIMARY KEY
                REFERENCES collections(id) ON DELETE RESTRICT,
  epc           TEXT NOT NULL UNIQUE,                -- RFID标签EPC编码
  status        TEXT NOT NULL DEFAULT '在库'
                  CHECK (status IN ('在库','展出中','借调中','修复中','外展中')),
  shelf_area    TEXT,                                -- 所在货架区域编号
  version       INTEGER NOT NULL DEFAULT 0,          -- 乐观锁版本号(并发控制用)
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TRIGGER IF NOT EXISTS trg_inv_updated
AFTER UPDATE ON inventory_status
BEGIN
  UPDATE inventory_status SET updated_at = datetime('now','localtime')
  WHERE collection_id = NEW.collection_id;
END;

CREATE INDEX IF NOT EXISTS idx_inv_status     ON inventory_status (status);
CREATE INDEX IF NOT EXISTS idx_inv_shelf_area ON inventory_status (shelf_area);

-- ------------------------------------------------------------
-- D3 流转历史库 (transfer_history)  每笔出入库追加一条,审计用
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfer_history (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_id      INTEGER NOT NULL
                     REFERENCES collections(id) ON DELETE RESTRICT,
  action             TEXT NOT NULL
                     CHECK (action IN ('出库','归还','借调','归还借调',
                                       '送修','修复归','外展','外展归还')),
  before_status      TEXT,                           -- 操作前状态
  after_status       TEXT,                           -- 操作后状态
  borrower           TEXT,                           -- 借用人/去向(出库/借调/外展时填写)
  due_at             TEXT,                           -- 预计归还时间
  operator_id        INTEGER NOT NULL
                     REFERENCES users(id) ON DELETE RESTRICT,
  auth_credential_id TEXT,                           -- 人脸认证凭据ID(暂缓,预留)
  remark             TEXT,                           -- 备注
  created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_hist_collection ON transfer_history (collection_id);
CREATE INDEX IF NOT EXISTS idx_hist_created    ON transfer_history (created_at);
CREATE INDEX IF NOT EXISTS idx_hist_operator   ON transfer_history (operator_id);

-- ------------------------------------------------------------
-- D4 盘点记录库 (inventory_check_tasks)  盘点不改库存,只出差异报告
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_check_tasks (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  area_range         TEXT NOT NULL,                  -- 盘点区域(如A1-A5或FULL)
  snapshot_epc_list  TEXT,                           -- 库存快照(JSON数组)
  observed_epc_list  TEXT,                           -- RFID观测批次(JSON数组)
  missing_count      INTEGER NOT NULL DEFAULT 0,     -- 未读到数量
  unknown_count      INTEGER NOT NULL DEFAULT 0,     -- 未知标签数量
  incomplete_areas   TEXT,                           -- 盘点不完整区域(JSON数组)
  result_report      TEXT,                           -- 差异报告(JSON对象)
  status             TEXT NOT NULL DEFAULT '待盘点'
                       CHECK (status IN ('待盘点','盘点中','已完成','不完整')),
  operator_id        INTEGER NOT NULL
                     REFERENCES users(id) ON DELETE RESTRICT,
  started_at         TEXT,
  finished_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_check_status   ON inventory_check_tasks (status);
CREATE INDEX IF NOT EXISTS idx_check_area     ON inventory_check_tasks (area_range);
CREATE INDEX IF NOT EXISTS idx_check_operator ON inventory_check_tasks (operator_id);
CREATE INDEX IF NOT EXISTS idx_check_started  ON inventory_check_tasks (started_at);

-- ------------------------------------------------------------
-- D6 RFID 设备库 (rfid_devices)  设备状态页数据源
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rfid_devices (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  device_no     TEXT NOT NULL UNIQUE,              -- 阅读器编号
  location      TEXT,                              -- 安装位置
  connected     TEXT NOT NULL DEFAULT '离线'
                  CHECK (connected IN ('在线','离线','故障')),
  collecting    INTEGER NOT NULL DEFAULT 0,        -- 1=自动采集中 0=暂停
  last_seen_at  TEXT,                              -- 最近采集时间
  last_error    TEXT,                              -- 设备故障信息
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TRIGGER IF NOT EXISTS trg_dev_updated
AFTER UPDATE ON rfid_devices
BEGIN
  UPDATE rfid_devices SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
END;

-- ------------------------------------------------------------
-- D7 标签观测库 (rfid_observations)  采集流水,供状态展示/盘点
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rfid_observations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id  INTEGER NOT NULL REFERENCES rfid_devices(id) ON DELETE CASCADE,
  epc        TEXT NOT NULL,                        -- 读到的标签 EPC
  read_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_obs_read_at ON rfid_observations (read_at);
CREATE INDEX IF NOT EXISTS idx_obs_epc     ON rfid_observations (epc);
CREATE INDEX IF NOT EXISTS idx_obs_device  ON rfid_observations (device_id);

-- ------------------------------------------------------------
-- D8 异常告警库 (alerts)  疑似离架/未知标签/设备故障 → 处置闭环
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  type          TEXT NOT NULL
                  CHECK (type IN ('疑似离架','未知标签','设备故障')),
  epc           TEXT,                              -- 关联标签 EPC(离架/未知)
  collection_id INTEGER
                  REFERENCES collections(id) ON DELETE SET NULL,
  device_id     INTEGER
                  REFERENCES rfid_devices(id) ON DELETE SET NULL,
  reason        TEXT NOT NULL,                     -- 触发原因
  status        TEXT NOT NULL DEFAULT '未处理'
                  CHECK (status IN ('未处理','已处理')),
  handle_note   TEXT,                              -- 处置意见
  handled_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  handled_at    TEXT,                              -- 确认处理时间
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_alert_status  ON alerts (status);
CREATE INDEX IF NOT EXISTS idx_alert_type    ON alerts (type);
CREATE INDEX IF NOT EXISTS idx_alert_created ON alerts (created_at);
CREATE INDEX IF NOT EXISTS idx_alert_epc     ON alerts (epc);
