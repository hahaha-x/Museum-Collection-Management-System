# -*- coding: utf-8 -*-
"""应用配置 —— SQLite 版:零安装、零密码,数据库就是 museum.db 一个文件"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get('MUSEUM_SECRET_KEY', 'museum-shelf-dev-key-change-me')

    # ---- SQLite 数据库文件路径 ----
    DB_PATH = os.environ.get('MUSEUM_DB_PATH', os.path.join(BASE_DIR, 'museum.db'))

    # ---- 藏品照片上传 ----
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 单文件最大 8MB
    ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

    # 仅后端访问 C# 服务；密钥不写进模板，不提交到代码仓库。
    RFID_API_URL = os.environ.get('RFID_API_URL', 'http://127.0.0.1:5080')
    RFID_API_TOKEN = os.environ.get('RFID_API_TOKEN', '')
    RFID_HTTP_TIMEOUT = 3
    RFID_MAX_AGE_SECONDS = 10
    RFID_WORKER_ENABLED = True
