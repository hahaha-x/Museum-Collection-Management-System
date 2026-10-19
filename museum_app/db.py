# -*- coding: utf-8 -*-
"""数据库连接管理:SQLite,每请求一条连接,请求结束自动关闭"""
import sqlite3

from flask import current_app, g


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DB_PATH'])
        g.db.row_factory = sqlite3.Row  # 结果按列名访问: row['status']
        g.db.execute('PRAGMA foreign_keys = ON')  # SQLite 默认关闭外键,须每连接开启
        from rfid_source import ensure_schema
        ensure_schema(g.db)
    return g.db


def close_db(e=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def query_all(sql, args=None):
    cur = get_db().execute(sql, args or ())
    rows = cur.fetchall()
    cur.close()
    return rows


def query_one(sql, args=None):
    cur = get_db().execute(sql, args or ())
    row = cur.fetchone()
    cur.close()
    return row
