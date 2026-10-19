# -*- coding: utf-8 -*-
"""初始化数据库:执行 schema_sqlite.sql 建表,并写入种子账号与示例藏品

用法:  py init_db.py
说明:  SQLite 零安装零密码,数据库文件为 museum_app/museum.db
       重复执行安全:已有数据时自动跳过种子数据。
"""
import os
from urllib.parse import quote

import sqlite3
from werkzeug.security import generate_password_hash

from config import Config

ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(ROOT, 'schema_sqlite.sql')


def gen_img(prompt):
    """示例藏品图片(文生图 API),URL 需编码"""
    return ('https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image'
            f'?prompt={quote(prompt)}&image_size=square')


def main():
    print(f"初始化 SQLite 数据库: {Config.DB_PATH}")
    conn = sqlite3.connect(Config.DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')  # 提升并发读写能力

    with open(SCHEMA_PATH, encoding='utf-8') as fp:
        conn.executescript(fp.read())
    conn.commit()
    print('建表完成(5 张表 + 触发器 + 索引)')

    # 旧库迁移:transfer_history 补借用字段( borrower/due_at )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transfer_history)")}
    if 'borrower' not in cols:
        conn.execute("ALTER TABLE transfer_history ADD COLUMN borrower TEXT")
        conn.execute("ALTER TABLE transfer_history ADD COLUMN due_at TEXT")
        conn.commit()
        print('已迁移: transfer_history 增加 borrower/due_at')

    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        print('数据库已有数据,跳过种子数据。')
        conn.close()
        return

    cur = conn.cursor()
    users = [
        ('admin',     'Admin@123', '系统管理员', '管理员',     None),
        ('keeper01',  '123456',    '王馆管',     '馆藏管理员', '13800000001'),
        ('curator01', '123456',    '李策展',     '查看者',     '13800000002'),
    ]
    for emp, pwd, name, role, phone in users:
        cur.execute(
            "INSERT INTO users(employee_no, password_hash, name, role, phone) "
            "VALUES (?,?,?,?,?)",
            (emp, generate_password_hash(pwd), name, role, phone))

    samples = [
        ('MUS-0001', '唐三彩骆驼俑', '陶瓷', '唐', '陕西西安出土', 'EPC-0001', 'A1',
         'Tang dynasty sancai tri-color glazed pottery camel figurine, Chinese museum artifact photography, dark studio background'),
        ('MUS-0002', '青花缠枝莲纹梅瓶', '陶瓷', '明永乐', '江西景德镇窑', 'EPC-0002', 'A1',
         'Ming dynasty blue and white porcelain meiping vase with lotus scroll pattern, museum artifact photography, dark background'),
        ('MUS-0003', '兽面纹青铜鼎', '青铜器', '商', '河南安阳殷墟', 'EPC-0003', 'A2',
         'ancient Chinese bronze ding tripod cauldron with taotie mask pattern, Shang dynasty, museum artifact photography, dark background'),
        ('MUS-0004', '溪山行旅图(摹本)', '书画', '北宋', '清宫旧藏', 'EPC-0004', 'A2',
         'classical Chinese landscape hanging scroll painting, mountains and travelers, Song dynasty style, museum display, dark background'),
        ('MUS-0005', '谷纹玉璧', '玉器', '汉', '江苏徐州汉墓', 'EPC-0005', 'B1',
         'Han dynasty carved green jade bi disc with grain pattern, museum artifact photography, dark background'),
        ('MUS-0006', '鎏金铜佛像', '金银器', '唐', '甘肃敦煌', 'EPC-0006', 'B1',
         'Tang dynasty gilt bronze seated Buddha statue, museum artifact photography, dark background'),
        ('MUS-0007', '粉彩花鸟纹盘', '陶瓷', '清乾隆', '江西景德镇窑', 'EPC-0007', 'B2',
         'Qing dynasty famille rose porcelain plate painted with flowers and birds, museum artifact photography, dark background'),
        ('MUS-0008', '青铜编钟', '青铜器', '战国', '湖北随州曾侯乙墓', 'EPC-0008', 'B2',
         'ancient Chinese bronze bianzhong chime bell, Warring States period, museum artifact photography, dark background'),
    ]
    for no, name, cat, era, origin, epc, area, prompt in samples:
        cur.execute(
            "INSERT INTO collections(collection_no, name, category, era, origin, photo_url) "
            "VALUES (?,?,?,?,?,?)",
            (no, name, cat, era, origin, gen_img(prompt)))
        cur.execute(
            "INSERT INTO inventory_status(collection_id, epc, status, shelf_area) "
            "VALUES (?,?,?,?)",
            (cur.lastrowid, epc, '在库', area))

    cur.execute(
        "INSERT INTO rfid_devices(device_no, location, connected) VALUES (?,?,?)",
        ('READER-01', 'A/B 区货架通道', '在线'))

    conn.commit()
    conn.close()
    print(f'种子数据写入完成({len(users)} 个账号,{len(samples)} 件藏品,1 台阅读器)。登录账号:')
    print('  admin     / Admin@123  (管理员)')
    print('  keeper01  / 123456     (馆藏管理员)')
    print('  curator01 / 123456     (查看者)')
    print('启动: py app.py 后访问 http://127.0.0.1:5000')
    print('查看数据: 用 SQLite 工具(如 VS Code 插件/DB Browser)打开 museum.db 即可')


if __name__ == '__main__':
    main()
