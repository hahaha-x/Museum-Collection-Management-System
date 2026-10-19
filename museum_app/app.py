# -*- coding: utf-8 -*-
"""博物馆智能货架管理系统 - Web 应用(组员A模块) — SQLite 版

功能:
  1. 登录认证(密码哈希 + 会话 + 角色权限;人脸识别模块暂缓,以登录会话代替凭据校验)
  2. 藏品档案管理(增删改查、搜索、图片展示/上传、EPC 绑定)
  3. 出入库登记(角色权限 → 状态预校验 → 乐观锁更新 + 写流转历史,单事务)
  4. 流水记录查询与 Excel 导出
  5. 操作人员管理(仅管理员)
"""
import functools
import io
import json
import os
import sqlite3
import uuid
import secrets
import threading
from datetime import datetime

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from openpyxl import Workbook
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config
from db import close_db, get_db, query_all, query_one
from rfid_source import (RfidError, source_state, store_batch, latest_batch,
                         read_device, timestamp_age, normalize_epc)

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
app.teardown_appcontext(close_db)


class BizError(Exception):
    """业务校验失败(状态不允许/并发冲突等)"""


# ---------------- 业务常量 ----------------
STATUS_LIST = ['在库', '展出中', '借调中', '修复中', '外展中']

# 出入库动作:允许的原状态 → 目标状态
ACTION_FLOW = {
    '出库':     {'from': {'在库'},   'to': '展出中'},
    '借调':     {'from': {'在库'},   'to': '借调中'},
    '送修':     {'from': {'在库'},   'to': '修复中'},
    '外展':     {'from': {'在库'},   'to': '外展中'},
    '归还':     {'from': {'展出中'}, 'to': '在库'},
    '归还借调': {'from': {'借调中'}, 'to': '在库'},
    '修复归':   {'from': {'修复中'}, 'to': '在库'},
    '外展归还': {'from': {'外展中'}, 'to': '在库'},
}

# 角色默认权限(三角色:管理员全部/馆藏管理员管出入库/查看者仅查询;users.permissions 非空时以个人权限为准)
ROLE_PERMISSIONS = {
    '馆藏管理员': {'出库', '归还', '借调', '归还借调', '外展', '外展归还', '送修', '修复归'},
    '查看者':     set(),  # 仅查询数据与记录
    '管理员':     set(ACTION_FLOW.keys()),
}

STATUS_CLASS = {'在库': 'in', '展出中': 'out', '借调中': 'loan', '修复中': 'repair', '外展中': 'away'}


# ---------------- 模板辅助 ----------------
@app.template_filter('photo')
def photo_src(path):
    """藏品图片:无图用占位图;http 开头的直接用;否则视为 static 下的相对路径"""
    if not path:
        return url_for('static', filename='img/placeholder.svg')
    if path.startswith(('http://', 'https://')):
        return path
    return url_for('static', filename=path)


@app.template_filter('dt')
def fmt_dt(v):
    """时间显示:SQLite 存的是 'YYYY-MM-DD HH:MM:SS' 文本,截取到分钟即可"""
    if not v:
        return '-'
    return str(v)[:16]


app.jinja_env.filters['stclass'] = lambda s: STATUS_CLASS.get(s, 'in')
app.jinja_env.filters['from_json'] = json.loads


# ---------------- 认证与权限 ----------------
def current_user():
    if 'user_id' not in session:
        return None
    if '_cached_user' not in g:
        g._cached_user = query_one(
            "SELECT id, employee_no, name, role, permissions, status "
            "FROM users WHERE id = ?", (session['user_id'],))
    return g._cached_user


@app.context_processor
def inject_globals():
    session.setdefault('rfid_csrf', secrets.token_hex(24))
    return {'me': current_user(), 'rfid_mode': source_state(get_db())['mode'],
            'rfid_csrf': session['rfid_csrf']}


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            session.clear()
            return redirect(url_for('login', next=request.path))
        # 会话期内账号被停用/删除时即时失效
        if user['status'] != '启用':
            session.clear()
            flash('账号已被停用,请联系管理员', 'error')
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user()['role'] != '管理员':
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def keeper_required(view):
    """仅馆藏管理员/管理员可执行(档案增删改)"""
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user()['role'] not in ('馆藏管理员', '管理员'):
            flash('仅馆藏管理员/管理员可执行该操作', 'error')
            return redirect(url_for('collections_list'))
        return view(*args, **kwargs)
    return wrapped


def allowed_actions(user):
    """用户可执行的出入库动作:个人 permissions(JSON) 优先,否则按角色默认"""
    if user and user['permissions']:
        try:
            return set(json.loads(user['permissions']))
        except (ValueError, TypeError):
            pass
    return ROLE_PERMISSIONS.get(user['role'], set()) if user else set()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        emp_no = request.form.get('employee_no', '').strip()
        password = request.form.get('password', '')
        user = query_one("SELECT * FROM users WHERE employee_no = ?", (emp_no,))
        if user is None or not check_password_hash(user['password_hash'], password):
            flash('工号或密码错误', 'error')
        elif user['status'] != '启用':
            flash('账号已停用,请联系管理员', 'error')
        else:
            session.clear()
            session['user_id'] = user['id']
            get_db().execute(
                "UPDATE users SET last_login_at = datetime('now','localtime') WHERE id = ?",
                (user['id'],))
            get_db().commit()
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') else url_for('dashboard'))
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ---------------- 首页 / 仪表盘 ----------------
@app.route('/')
@login_required
def dashboard():
    stats = query_one("""
        SELECT COUNT(*) AS total,
               IFNULL(SUM(status = '在库'),0)   AS in_stock,
               IFNULL(SUM(status = '展出中'),0) AS on_display,
               IFNULL(SUM(status = '借调中'),0) AS on_loan,
               IFNULL(SUM(status = '修复中'),0) AS in_repair,
               IFNULL(SUM(status = '外展中'),0) AS on_out
        FROM inventory_status""")
    recent = query_all("""
        SELECT h.*, c.name AS collection_name, c.collection_no, u.name AS operator_name
        FROM transfer_history h
        JOIN collections c ON c.id = h.collection_id
        LEFT JOIN users u ON u.id = h.operator_id
        ORDER BY h.id DESC LIMIT 8""")
    dev = get_device()
    return render_template('dashboard.html', stats=stats, recent=recent,
                           alert_open=query_one(
                               "SELECT COUNT(*) AS n FROM alerts WHERE status = '未处理'")['n'],
                           dev=dev)


# ---------------- 藏品档案 ----------------
@app.route('/collections')
@login_required
def collections_list():
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    status = request.args.get('status', '').strip()
    shelf = request.args.get('shelf', '').strip()
    view = request.args.get('view', 'card')

    sql = """
        SELECT c.*, s.status, s.shelf_area, s.epc
        FROM collections c
        LEFT JOIN inventory_status s ON s.collection_id = c.id
        WHERE 1=1"""
    args = []
    if q:
        sql += " AND (c.collection_no LIKE ? OR c.name LIKE ? OR s.epc LIKE ?)"
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    if shelf:
        sql += " AND s.shelf_area LIKE ?"
        args.append(f'%{shelf}%')
    if category:
        sql += " AND c.category = ?"
        args.append(category)
    if status:
        sql += " AND s.status = ?"
        args.append(status)
    sql += " ORDER BY c.id DESC"

    items = query_all(sql, args)
    categories = [r['category'] for r in query_all(
        "SELECT DISTINCT category FROM collections ORDER BY category")]
    return render_template('collections/list.html', items=items, q=q, category=category,
                           status=status, view=view, categories=categories,
                           status_list=STATUS_LIST)


def save_photo(file_storage):
    """保存上传图片,返回 static 下的相对路径;未上传返回 None"""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in app.config['ALLOWED_IMAGE_EXT']:
        raise BizError('仅支持 png/jpg/jpeg/gif/webp 图片')
    filename = f'{uuid.uuid4().hex}.{ext}'
    file_storage.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return f'uploads/{filename}'


@app.route('/collections/new', methods=['GET', 'POST'])
@keeper_required
def collection_new():
    if request.method == 'POST':
        f = request.form
        try:
            photo_url = save_photo(request.files.get('photo')) or (f.get('photo_url', '').strip() or None)
            conn = get_db()
            conn.execute("""
                INSERT INTO collections(collection_no, name, category, era, origin, photo_url)
                VALUES (?,?,?,?,?,?)""",
                (f['collection_no'].strip(), f['name'].strip(), f['category'].strip(),
                 f.get('era', '').strip() or None, f.get('origin', '').strip() or None, photo_url))
            cid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()['id']
            conn.execute("""
                INSERT INTO inventory_status(collection_id, epc, status, shelf_area)
                VALUES (?,?,?,?)""",
                (cid, f['epc'].strip(), '在库', f.get('shelf_area', '').strip() or None))
            conn.commit()
            flash('藏品建档成功', 'success')
            return redirect(url_for('collections_list'))
        except sqlite3.IntegrityError:
            conn.rollback()
            flash('藏品编号或 EPC 已存在,请检查后重试', 'error')
        except BizError as e:
            flash(str(e), 'error')
    return render_template('collections/form.html', item=None)


@app.route('/collections/<int:cid>')
@login_required
def collection_detail(cid):
    item = query_one("""
        SELECT c.*, s.status, s.shelf_area, s.epc, s.version, s.updated_at AS status_at
        FROM collections c
        LEFT JOIN inventory_status s ON s.collection_id = c.id
        WHERE c.id = ?""", (cid,))
    if item is None:
        abort(404)
    history = query_all("""
        SELECT h.*, u.name AS operator_name
        FROM transfer_history h LEFT JOIN users u ON u.id = h.operator_id
        WHERE h.collection_id = ? ORDER BY h.id DESC""", (cid,))
    return render_template('collections/detail.html', item=item, history=history)


@app.route('/collections/<int:cid>/edit', methods=['GET', 'POST'])
@keeper_required
def collection_edit(cid):
    item = query_one("""
        SELECT c.*, s.status, s.shelf_area, s.epc
        FROM collections c LEFT JOIN inventory_status s ON s.collection_id = c.id
        WHERE c.id = ?""", (cid,))
    if item is None:
        abort(404)
    if request.method == 'POST':
        f = request.form
        try:
            photo_url = (save_photo(request.files.get('photo'))
                         or f.get('photo_url', '').strip()
                         or item['photo_url'])
            new_epc = f.get('epc', '').strip()
            area = f.get('shelf_area', '').strip() or None
            conn = get_db()
            # 编号不可修改;状态只允许经出入库流程变更
            conn.execute("""
                UPDATE collections SET name=?, category=?, era=?, origin=?, photo_url=?
                WHERE id=?""",
                (f['name'].strip(), f['category'].strip(),
                 f.get('era', '').strip() or None, f.get('origin', '').strip() or None,
                 photo_url, cid))
            if item['epc'] is None:
                conn.execute("""
                    INSERT INTO inventory_status(collection_id, epc, status, shelf_area)
                    VALUES (?,?,?,?)""", (cid, new_epc, '在库', area))
            else:
                conn.execute("""
                    UPDATE inventory_status SET epc=?, shelf_area=?
                    WHERE collection_id=?""", (new_epc, area, cid))
            conn.commit()
            flash('保存成功', 'success')
            return redirect(url_for('collection_detail', cid=cid))
        except sqlite3.IntegrityError:
            conn.rollback()
            flash('EPC 已被其他藏品绑定', 'error')
        except BizError as e:
            flash(str(e), 'error')
    return render_template('collections/form.html', item=item)


@app.route('/collections/<int:cid>/delete', methods=['POST'])
@keeper_required
def collection_delete(cid):
    item = query_one("""
        SELECT c.id, s.status FROM collections c
        LEFT JOIN inventory_status s ON s.collection_id = c.id WHERE c.id = ?""", (cid,))
    if item is None:
        abort(404)
    if item['status'] and item['status'] != '在库':
        flash(f'当前状态为「{item["status"]}」,仅"在库"藏品可删除', 'error')
        return redirect(url_for('collection_detail', cid=cid))
    try:
        conn = get_db()
        conn.execute("DELETE FROM inventory_status WHERE collection_id = ?", (cid,))
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
        conn.commit()
        flash('藏品档案已删除', 'success')
        return redirect(url_for('collections_list'))
    except sqlite3.IntegrityError:
        conn.rollback()
        flash('该藏品存在流转记录,不可删除(审计保护)', 'error')
        return redirect(url_for('collection_detail', cid=cid))


# ---------------- 出入库登记 ----------------
@app.route('/inventory')
@login_required
def inventory_page():
    user = current_user()
    actions = [a for a in ACTION_FLOW if a in allowed_actions(user)]
    items = query_all("""
        SELECT c.id, c.collection_no, c.name, s.epc, s.status, s.shelf_area
        FROM collections c JOIN inventory_status s ON s.collection_id = c.id
        ORDER BY s.status, c.id""")
    flow_json = json.dumps(
        {k: {'from': sorted(v['from']), 'to': v['to']} for k, v in ACTION_FLOW.items()},
        ensure_ascii=False)
    return render_template('inventory/form.html', items=items, actions=actions,
                           flow=ACTION_FLOW, flow_json=flow_json)


@app.route('/inventory/submit', methods=['POST'])
@login_required
def inventory_submit():
    user = current_user()
    action = request.form.get('action', '').strip()
    ids = request.form.getlist('collection_ids', type=int)
    remark = request.form.get('remark', '').strip()
    borrower = request.form.get('borrower', '').strip()
    due_at = request.form.get('due_at', '').strip().replace('T', ' ') or None

    # 1. 校验链:操作类型 → 角色权限 → 非空 → 借用信息
    LOAN_ACTIONS = {'出库', '借调', '外展'}  # 需要登记借用人/预计归还时间的动作
    if action not in ACTION_FLOW:
        flash('无效的操作类型', 'error')
        return redirect(url_for('inventory_page'))
    if action not in allowed_actions(user):
        flash(f'当前角色({user["role"]})无权执行「{action}」', 'error')
        return redirect(url_for('inventory_page'))
    if not ids:
        flash('请至少选择一件藏品', 'error')
        return redirect(url_for('inventory_page'))
    if action in LOAN_ACTIONS and not borrower:
        flash(f'「{action}」必须填写借用人/去向', 'error')
        return redirect(url_for('inventory_page'))
    if not action in LOAN_ACTIONS:
        borrower = None  # 归还类操作不记录借用信息
        due_at = None

    flow = ACTION_FLOW[action]
    conn = get_db()
    try:
        # 2. 状态预校验
        marks = ','.join('?' * len(ids))
        rows = {r['collection_id']: r for r in conn.execute(
            f"SELECT collection_id, status, version FROM inventory_status "
            f"WHERE collection_id IN ({marks})", ids).fetchall()}
        missing = [i for i in ids if i not in rows]
        if missing:
            raise BizError(f'藏品 ID {missing} 不存在库存记录')
        bad = [(i, rows[i]['status']) for i in ids if rows[i]['status'] not in flow['from']]
        if bad:
            detail = '、'.join(f'ID{cid}(当前「{st}」)' for cid, st in bad)
            raise BizError(f'「{action}」要求原状态为 {"/".join(flow["from"])}:{detail}')

        # 3. 乐观锁更新库存 + 追加流转历史(同一事务)
        for cid in ids:
            cur = conn.execute("""
                UPDATE inventory_status SET status = ?, version = version + 1
                WHERE collection_id = ? AND version = ?""",
                (flow['to'], cid, rows[cid]['version']))
            if cur.rowcount != 1:
                raise BizError('并发冲突,请刷新后重试')
            conn.execute("""
                INSERT INTO transfer_history
                    (collection_id, action, before_status, after_status, operator_id,
                     borrower, due_at, remark)
                VALUES (?,?,?,?,?,?,?,?)""",
                (cid, action, rows[cid]['status'], flow['to'], user['id'],
                 borrower, due_at, remark or None))
        conn.commit()
        flash(f'「{action}」登记成功,共 {len(ids)} 件', 'success')
    except BizError as e:
        conn.rollback()
        flash(str(e), 'error')
    return redirect(url_for('inventory_page'))


# ---------------- 流水记录 ----------------
def build_history_query(args):
    sql = """
        SELECT h.*, c.collection_no, c.name AS collection_name, u.name AS operator_name
        FROM transfer_history h
        JOIN collections c ON c.id = h.collection_id
        LEFT JOIN users u ON u.id = h.operator_id
        WHERE 1=1"""
    params = []
    q = args.get('q', '').strip()
    if q:
        sql += " AND (c.collection_no LIKE ? OR c.name LIKE ?)"
        params += [f'%{q}%', f'%{q}%']
    if args.get('action', '').strip():
        sql += " AND h.action = ?"
        params.append(args['action'].strip())
    if args.get('operator_id', '').strip():
        sql += " AND h.operator_id = ?"
        params.append(int(args['operator_id']))
    if args.get('date_from', ''):
        sql += " AND h.created_at >= ?"
        params.append(args['date_from'] + ' 00:00:00')
    if args.get('date_to', ''):
        sql += " AND h.created_at <= ?"
        params.append(args['date_to'] + ' 23:59:59')
    return sql, params


@app.route('/history')
@login_required
def history():
    sql, params = build_history_query(request.args)
    rows = query_all(sql + " ORDER BY h.id DESC LIMIT 500", params)
    operators = query_all("""
        SELECT DISTINCT u.id, u.name FROM transfer_history h
        JOIN users u ON u.id = h.operator_id ORDER BY u.id""")
    return render_template('history/list.html', rows=rows, actions=list(ACTION_FLOW),
                           operators=operators)


@app.route('/history/export')
@login_required
def history_export():
    sql, params = build_history_query(request.args)
    rows = query_all(sql + " ORDER BY h.id DESC", params)

    wb = Workbook()
    ws = wb.active
    ws.title = '流转记录'
    ws.append(['流水号', '藏品编号', '藏品名称', '操作类型', '操作前状态', '操作后状态',
               '借用人/去向', '预计归还', '操作人', '备注', '操作时间'])
    for r in rows:
        ws.append([r['id'], r['collection_no'], r['collection_name'], r['action'],
                   r['before_status'], r['after_status'], r['borrower'],
                   str(r['due_at'] or ''),
                   r['operator_name'] or r['operator_id'],
                   r['remark'], str(r['created_at'] or '')])
    for i, w in enumerate([10, 16, 22, 10, 12, 12, 16, 20, 10, 30, 20], 1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"流转记录_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------------- 操作人员管理(仅管理员) ----------------
@app.route('/users')
@admin_required
def users_list():
    users = query_all("""
        SELECT u.*, (SELECT COUNT(*) FROM transfer_history h WHERE h.operator_id = u.id) AS op_count
        FROM users u ORDER BY u.id""")
    return render_template('users/list.html', users=users)


@app.route('/users/new', methods=['GET', 'POST'])
@admin_required
def user_new():
    if request.method == 'POST':
        f = request.form
        if len(f.get('password', '')) < 6:
            flash('密码至少 6 位', 'error')
            return render_template('users/form.html', roles=list(ROLE_PERMISSIONS))
        try:
            get_db().execute("""
                INSERT INTO users(employee_no, password_hash, name, role, phone)
                VALUES (?,?,?,?,?)""",
                (f['employee_no'].strip(), generate_password_hash(f['password']),
                 f['name'].strip(), f['role'], f.get('phone', '').strip() or None))
            get_db().commit()
            flash('账号创建成功', 'success')
            return redirect(url_for('users_list'))
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash('工号已存在', 'error')
    return render_template('users/form.html', roles=list(ROLE_PERMISSIONS))


@app.route('/users/<int:uid>/edit', methods=['GET', 'POST'])
@admin_required
def user_edit(uid):
    user = query_one("SELECT * FROM users WHERE id = ?", (uid,))
    if user is None:
        abort(404)
    if request.method == 'POST':
        f = request.form
        role = f['role']
        if role not in ROLE_PERMISSIONS:
            flash('无效的角色', 'error')
        elif uid == session.get('user_id') and role != '管理员':
            flash('不能修改自己的角色,避免失去管理权限', 'error')
        else:
            get_db().execute(
                "UPDATE users SET name = ?, role = ?, phone = ? WHERE id = ?",
                (f['name'].strip(), role, f.get('phone', '').strip() or None, uid))
            get_db().commit()
            flash('账号信息已更新', 'success')
            return redirect(url_for('users_list'))
    return render_template('users/form.html', item=user, roles=list(ROLE_PERMISSIONS))


@app.route('/users/<int:uid>/toggle', methods=['POST'])
@admin_required
def user_toggle(uid):
    if uid == session.get('user_id'):
        flash('不能停用自己的账号', 'error')
        return redirect(url_for('users_list'))
    get_db().execute("""
        UPDATE users SET status = CASE WHEN status='启用' THEN '停用' ELSE '启用' END
        WHERE id = ?""", (uid,))
    get_db().commit()
    flash('账号状态已切换', 'success')
    return redirect(url_for('users_list'))


@app.route('/users/<int:uid>/reset-password', methods=['POST'])
@admin_required
def user_reset_pwd(uid):
    new_pwd = request.form.get('new_password', '')
    if len(new_pwd) < 6:
        flash('密码至少 6 位', 'error')
    else:
        get_db().execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(new_pwd), uid))
        get_db().commit()
        flash('密码已重置', 'success')
    return redirect(url_for('users_list'))


# ---------------- RFID 设备与采集 ----------------
import random
import time as _time

ALERT_TYPES = ('疑似离架', '未知标签', '设备故障')


def get_device():
    return query_one("SELECT * FROM rfid_devices ORDER BY id LIMIT 1")


def _raise_alert(conn, a_type, epc, device_id, reason):
    """生成异常告警(同类型同EPC的未处理告警不重复),返回是否新建"""
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE type = ? AND IFNULL(epc,'') = IFNULL(?,'') "
        "AND status = '未处理'", (a_type, epc)).fetchone()
    if dup:
        return False
    cid = None
    if epc:
        row = conn.execute("SELECT collection_id FROM inventory_status WHERE epc = ?",
                           (epc,)).fetchone()
        cid = row['collection_id'] if row else None  # 未建档标签 collection_id 为空
    conn.execute("""
        INSERT INTO alerts(type, epc, collection_id, device_id, reason)
        VALUES (?,?,?,?,?)""", (a_type, epc, cid, device_id, reason))
    return True


def _do_read(conn, dev):
    """执行一次模拟读数:写观测流水并生成异常告警,返回读到的 EPC 数"""
    on_shelf = [r['epc'] for r in conn.execute(
        "SELECT s.epc FROM inventory_status s WHERE s.status = '在库'")]
    read = list(on_shelf)
    if len(read) > 1 and random.random() < 0.15:      # 小概率漏读 1 件
        read.remove(random.choice(read))
    now = _time.strftime('%Y-%m-%d %H:%M:%S')
    for epc in read:
        conn.execute("INSERT INTO rfid_observations(device_id, epc, read_at) VALUES (?,?,?)",
                     (dev['id'], epc, now))
    # 8% 概率混入一个未知标签(异常素材)
    unknown = []
    if random.random() < 0.08:
        unknown = [f"UNKN-{uuid.uuid4().hex[:8].upper()}"]
        conn.execute("INSERT INTO rfid_observations(device_id, epc, read_at) VALUES (?,?,?)",
                     (dev['id'], unknown[0], now))
    conn.execute("UPDATE rfid_devices SET last_seen_at = ? WHERE id = ?",
                 (now, dev['id']))
    # 告警:在库但本轮未读到 → 疑似离架;未建档标签 → 未知标签
    for epc in set(on_shelf) - set(read):
        _raise_alert(conn, '疑似离架', epc, dev['id'],
                     f'[模拟数据] 自动采集未读到在库标签 {epc},疑似离架')
    for epc in unknown:
        _raise_alert(conn, '未知标签', epc, dev['id'],
                     f'[模拟数据] 观测到未建档标签 {epc},请核验来源')
    # 清理 10 分钟前的观测,控制表体积
    conn.execute("DELETE FROM rfid_observations "
                 "WHERE read_at < datetime('now','-10 minutes','localtime')")
    store_batch(conn, dev['id'], source_state(conn), read + unknown)
    return len(read)


def rfid_csrf_check():
    if not secrets.compare_digest(request.form.get('csrf_token', ''), session.get('rfid_csrf', '!')):
        abort(400)


@app.route('/rfid/source', methods=['POST'])
@admin_required
def rfid_source_switch():
    """切换全局数据源。新代次隐藏旧结果，不删除任何旧观测或业务记录。"""
    rfid_csrf_check()
    mode = request.form.get('source')
    if mode not in ('mock', 'device'):
        abort(400)
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    old = source_state(conn)
    if old['mode'] != mode:
        conn.execute('UPDATE rfid_source_state SET mode=?,generation=generation+1 WHERE id=1', (mode,))
        conn.execute("UPDATE rfid_devices SET collecting=0,connected='离线',last_seen_at=NULL,last_error=NULL")
    conn.commit()
    flash('数据源已设为' + ('模拟数据' if mode == 'mock' else '真实设备') + '。切换后请点击开始采集。', 'success')
    return redirect(url_for('rfid_page'))


def rfid_snapshot():
    """只显示当前来源、当前代次的最近一批。空读和失败必须清空旧 EPC。"""
    conn = get_db()
    dev = get_device()
    state = source_state(conn)
    if dev is None:
        return {'error': 'no-device', 'source': state['mode'], 'generation': state['generation']}
    batch = latest_batch(conn, dev['id'], state)
    valid = bool(batch and batch['valid'])
    stale = False
    if state['mode'] == 'device' and batch:
        try:
            age = timestamp_age(batch['collected_at'])
            stale = age < -2 or age > app.config['RFID_MAX_AGE_SECONDS']
        except RfidError:
            stale = True
    error = '观测已过期，等待真实设备新数据' if stale else dev['last_error']
    recent = [{'epc': e, 'read_at': batch['collected_at']} for e in json.loads(batch['epc_list'])[:10]] if valid and not stale else []
    return {'source': state['mode'], 'generation': state['generation'],
            'connected': '故障' if stale else dev['connected'], 'collecting': dev['collecting'],
            'last_seen_at': batch['collected_at'] if batch else None, 'last_error': error,
            'valid': valid and not stale, 'stale': stale, 'recent': recent}


@app.route('/rfid')
@login_required
def rfid_page():
    dev = get_device()
    snapshot = rfid_snapshot()
    if dev:
        dev = dict(dev, **{k: snapshot[k] for k in ('connected', 'collecting', 'last_seen_at', 'last_error')})
    return render_template('rfid/status.html', dev=dev, recent=snapshot.get('recent', []),
                           snapshot=snapshot, is_admin=current_user()['role'] == '管理员')


@app.route('/rfid/data')
@login_required
def rfid_data():
    """设备状态 JSON,前端每 5 秒轮询刷新"""
    return rfid_snapshot()


@app.route('/rfid/toggle', methods=['POST'])
@admin_required
def rfid_toggle():
    rfid_csrf_check()
    get_db().execute('BEGIN IMMEDIATE')
    dev = get_device()
    if dev is None:
        flash('未找到 RFID 设备', 'error')
    else:
        get_db().execute("UPDATE rfid_devices SET collecting = 1 - collecting WHERE id = ?",
                         (dev['id'],))
        get_db().commit()
        flash(f'自动采集已{"开始" if 1 - dev["collecting"] else "暂停"}', 'success')
    get_db().commit()
    return redirect(url_for('rfid_page'))


@app.route('/rfid/simulate', methods=['POST'])
@admin_required
def rfid_simulate():
    """模拟采集:normal 正常读数 / empty 空读 / fault 设备故障(无真实设备时的演示)"""
    rfid_csrf_check()
    mode = request.form.get('mode', 'normal')
    conn = get_db()
    conn.execute('BEGIN IMMEDIATE')
    if source_state(conn)['mode'] != 'mock':
        conn.rollback()
        flash('真实模式禁止注入模拟数据，请先切换到模拟模式', 'error')
        return redirect(url_for('rfid_page'))
    dev = get_device()
    if dev is None:
        flash('未找到 RFID 设备', 'error')
        return redirect(url_for('rfid_page'))
    now = _time.strftime('%Y-%m-%d %H:%M:%S')
    if mode == 'fault':
        store_batch(conn, dev['id'], source_state(conn), [], False, '模拟设备故障')
        conn.execute("UPDATE rfid_devices SET connected='故障', collecting=0, "
                     "last_error='模拟故障:RF 功率异常,读距不足' WHERE id = ?", (dev['id'],))
        _raise_alert(conn, '设备故障', None, dev['id'],
                     f'阅读器 {dev["device_no"]} 连接异常:RF 功率异常,读距不足')
        conn.commit()
        flash('已模拟设备故障', 'error')
    elif mode in ('normal', 'empty'):
        conn.execute("UPDATE rfid_devices SET connected='在线', last_error=NULL WHERE id = ?",
                     (dev['id'],))
        if mode == 'normal':
            n = _do_read(conn, dev)
            conn.commit()
            flash(f'模拟正常读数完成,读到 {n} 个标签', 'success')
        else:
            store_batch(conn, dev['id'], source_state(conn), [])
            conn.execute("UPDATE rfid_devices SET last_seen_at = ? WHERE id = ?", (now, dev['id']))
            conn.commit()
            flash('模拟空读完成,本次未读到任何标签', 'error')
    else:
        flash('无效的模拟模式', 'error')
    return redirect(url_for('rfid_page'))


def rfid_poll_once():
    """HTTP 等待在事务外；写入前再次核对来源代次，丢弃切换前的在途响应。"""
    conn = get_db()
    state = source_state(conn)
    dev = get_device()
    if not dev or not dev['collecting']:
        return
    observation, error = None, None
    if state['mode'] == 'device':
        try:
            observation = read_device(app.config)
        except RfidError as exc:
            error = str(exc)
    conn.execute('BEGIN IMMEDIATE')
    if source_state(conn) != state or not get_device()['collecting']:
        conn.rollback()
        return
    if state['mode'] == 'mock':
        conn.execute("UPDATE rfid_devices SET connected='在线',last_error=NULL WHERE id=?", (dev['id'],))
        _do_read(conn, dev)
    elif error:
        store_batch(conn, dev['id'], state, [], False, error)
        conn.execute("UPDATE rfid_devices SET connected='故障',last_error=? WHERE id=?", (error, dev['id']))
    else:
        store_batch(conn, dev['id'], state, observation['epc_list'], observation=observation)
        conn.execute("UPDATE rfid_devices SET connected='在线',last_error=NULL,last_seen_at=? WHERE id=?",
                     (observation['collected_at'], dev['id']))
        # 真机单轮缺读不等于离架；此处不套用模拟器的一次缺读告警规则。
    conn.commit()


def rfid_worker():
    """真实模式即使上次故障仍重试；不会自动回退到 mock。"""
    while True:
        _time.sleep(5)
        try:
            with app.app_context():
                rfid_poll_once()
        except Exception:
            app.logger.exception('RFID 后台轮询失败')


_worker_lock = threading.Lock()
_worker_started = False


@app.before_request
def ensure_rfid_worker():
    # 首个请求时启动，避免 Flask debug 父进程和重载子进程各启动一个线程。
    # 部署约定：本实验使用一个 Flask 服务进程；多 worker 部署需独立调度器。
    global _worker_started
    if app.testing or not app.config['RFID_WORKER_ENABLED']:
        return
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=rfid_worker, daemon=True).start()
            _worker_started = True


# ---------------- 自动盘点 ----------------
@app.route('/check')
@login_required
def check_list():
    tasks = query_all("""
        SELECT t.*, u.name AS operator_name
        FROM inventory_check_tasks t LEFT JOIN users u ON u.id = t.operator_id
        ORDER BY t.id DESC""")
    return render_template('check/list.html', tasks=tasks)


@app.route('/check/new', methods=['POST'])
@keeper_required
def check_new():
    user = current_user()
    area = request.form.get('area_range', '').strip() or 'FULL'
    conn = get_db()
    state = source_state(conn)
    real_observation = None
    if state['mode'] == 'device':
        try:
            real_observation = read_device(app.config)
        except RfidError as exc:
            flash('真实观测比对失败：' + str(exc) + '。没有使用模拟结果。', 'error')
            return redirect(url_for('check_list'))
    # 数据源在网络请求期间可能切换；持短事务核对代次后才创建盘点记录。
    conn.execute('BEGIN IMMEDIATE')
    if source_state(conn) != state:
        conn.rollback()
        flash('数据源已切换，请重新发起盘点', 'error')
        return redirect(url_for('check_list'))
    # 1. 快照:发起时刻该区域的应在架清单(仅"在库"藏品)
    sql = ("SELECT c.id, c.collection_no, c.name, s.epc FROM collections c "
           "JOIN inventory_status s ON s.collection_id = c.id WHERE s.status = '在库'")
    args = []
    if area != 'FULL':
        sql += " AND s.shelf_area = ?"
        args.append(area)
    snapshot = [dict(r) for r in conn.execute(sql + " ORDER BY c.id", args)]
    if not snapshot:
        flash('所选区域没有在库藏品,无法盘点', 'error')
        return redirect(url_for('check_list'))
    if state['mode'] == 'device':
        for row in snapshot:
            row['epc'] = normalize_epc(row['epc'])
    snapshot_epcs = [r['epc'] for r in snapshot]

    # 2. 实际读取:模拟一次采集(小概率漏读/未知标签,演示差异)
    if state['mode'] == 'device':
        observed = real_observation['epc_list']
    else:
        observed = list(snapshot_epcs)
        if len(observed) > 1 and random.random() < 0.6:
            observed.remove(random.choice(observed))          # 保留原模拟逻辑
        unknown_epcs = []
        if random.random() < 0.25:
            unknown_epcs = [f"UNKN-{uuid.uuid4().hex[:8].upper()}"]
        observed += unknown_epcs

    # 3. 比对:缺失 = 快照-实测;未知 = 实测-快照;正常 = 交集
    snap_set, obs_set = set(snapshot_epcs), set(observed)
    missing_epcs = sorted(snap_set - obs_set)
    unknown_epcs = sorted(obs_set - snap_set)
    normal_epcs = sorted(snap_set & obs_set)
    name_map = {r['epc']: r for r in snapshot}
    report = {
        'source': state['mode'],
        'observation_id': real_observation['observation_id'] if real_observation else None,
        'collected_at': real_observation['collected_at'] if real_observation else None,
        'coverage_verified': False,
        'normal': [{'epc': e, 'name': name_map[e]['name'], 'no': name_map[e]['collection_no']} for e in normal_epcs],
        'missing': [{'epc': e, 'name': name_map[e]['name'], 'no': name_map[e]['collection_no']} for e in missing_epcs],
        'unknown': [{'epc': e} for e in unknown_epcs],
    }
    conn.execute("""
        INSERT INTO inventory_check_tasks
            (area_range, snapshot_epc_list, observed_epc_list, missing_count,
             unknown_count, result_report, status, operator_id, started_at, finished_at)
        VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))""",
        (area, json.dumps(snapshot_epcs), json.dumps(observed), len(missing_epcs),
         len(unknown_epcs), json.dumps(report, ensure_ascii=False),
         '已完成' if state['mode'] == 'mock' and not missing_epcs else '不完整', user['id']))
    conn.commit()
    flash('真实观测比对完成；尚未验证全区域覆盖，结果标为不完整' if state['mode'] == 'device' else '模拟盘点完成', 'success')
    return redirect(url_for('check_detail', tid=query_one("SELECT last_insert_rowid() AS id")['id']))


@app.route('/check/<int:tid>')
@login_required
def check_detail(tid):
    task = query_one("""
        SELECT t.*, u.name AS operator_name
        FROM inventory_check_tasks t LEFT JOIN users u ON u.id = t.operator_id
        WHERE t.id = ?""", (tid,))
    if task is None:
        abort(404)
    report = json.loads(task['result_report']) if task['result_report'] else None
    return render_template('check/detail.html', task=task, report=report)


# ---------------- 异常告警与处理 ----------------
@app.route('/alerts')
@login_required
def alerts_list():
    q_type = request.args.get('type', '').strip()
    q_status = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    sql = ("SELECT a.*, c.name AS collection_name, c.collection_no, "
           "d.device_no, u.name AS handler_name "
           "FROM alerts a "
           "LEFT JOIN collections c ON c.id = a.collection_id "
           "LEFT JOIN rfid_devices d ON d.id = a.device_id "
           "LEFT JOIN users u ON u.id = a.handled_by WHERE 1=1")
    params = []
    if q_type:
        sql += " AND a.type = ?"
        params.append(q_type)
    if q_status:
        sql += " AND a.status = ?"
        params.append(q_status)
    if date_from:
        sql += " AND a.created_at >= ?"
        params.append(date_from + ' 00:00:00')
    if date_to:
        sql += " AND a.created_at <= ?"
        params.append(date_to + ' 23:59:59')
    alerts = query_all(sql + " ORDER BY a.id DESC", params)
    return render_template('alerts/list.html', alerts=alerts, types=sorted(ALERT_TYPES),
                           q_type=q_type, q_status=q_status,
                           date_from=date_from, date_to=date_to)


@app.route('/alerts/<int:aid>')
@login_required
def alert_detail(aid):
    alert = query_one("""
        SELECT a.*, c.name AS collection_name, c.collection_no,
               d.device_no, u.name AS handler_name
        FROM alerts a
        LEFT JOIN collections c ON c.id = a.collection_id
        LEFT JOIN rfid_devices d ON d.id = a.device_id
        LEFT JOIN users u ON u.id = a.handled_by
        WHERE a.id = ?""", (aid,))
    if alert is None:
        abort(404)
    return render_template('alerts/detail.html', alert=alert,
                           can_handle=current_user()['role'] in ('馆藏管理员', '管理员'))


@app.route('/alerts/<int:aid>/handle', methods=['POST'])
@keeper_required
def alert_handle(aid):
    note = request.form.get('handle_note', '').strip()
    if not note:
        flash('请填写处置意见', 'error')
        return redirect(url_for('alert_detail', aid=aid))
    user = current_user()
    conn = get_db()
    cur = conn.execute("""
        UPDATE alerts SET status = '已处理', handle_note = ?, handled_by = ?,
                           handled_at = datetime('now','localtime')
        WHERE id = ? AND status = '未处理'""", (note, user['id'], aid))
    conn.commit()
    flash('告警已确认处理' if cur.rowcount else '该告警已被处理', 'success')
    return redirect(url_for('alert_detail', aid=aid))


@app.route('/alerts/pending-count')
@login_required
def alerts_pending_count():
    n = query_one("SELECT COUNT(*) AS n FROM alerts WHERE status = '未处理'")['n']
    return {'count': n}


# ---------------- 错误处理 ----------------
@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', message='页面不存在'), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', message='无权限访问该页面'), 403


@app.errorhandler(sqlite3.Error)
def db_error(e):
    return render_template('error.html',
                           message='数据库操作失败:请先运行 init_db.py 完成初始化。'
                                   f'({e.__class__.__name__})'), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
