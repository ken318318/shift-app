from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from flask_cors import CORS
import psycopg2
from psycopg2 import pool
import os
import re
import datetime
import json
import time
import pytz

app = Flask(__name__)
app.json.sort_keys = False          # 保留字典原有次序（否則 jsonify 會按字母重排）
app.secret_key = "Ken_Shift_App_Secret_123" 
CORS(app)

STAFF_PASSWORD = "BS2026"       
ADMIN_PASSWORD = "ADMIN888"     
DB_URL = os.environ.get("DB_URL")

DB_POOL = None
def init_pool():
    global DB_POOL
    try:
        DB_POOL = psycopg2.pool.SimpleConnectionPool(1, 10, DB_URL)
        print("連線池建立成功！")
    except Exception as e:
        print("連線池建立失敗:", e)

init_pool()

def get_db_connection():
    return DB_POOL.getconn()

def release_db_connection(conn):
    DB_POOL.putconn(conn)


# ---------- 系統設定 / 稽核（自動建表，無須手動修改資料庫） ----------
def ensure_tables():
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("ALTER TABLE venue_configs ADD COLUMN IF NOT EXISTS cat_order INTEGER DEFAULT 0")
        c.execute("ALTER TABLE venue_configs ADD COLUMN IF NOT EXISTS item_order INTEGER DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS guest_feedback TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS dlr_feedback TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS layout_side TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS on_duty TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed BOOLEAN DEFAULT FALSE")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed_by TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
                        id BIGSERIAL PRIMARY KEY,
                        actor TEXT,
                        action TEXT,
                        detail TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("""CREATE TABLE IF NOT EXISTS members (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        mtype TEXT NOT NULL DEFAULT 'staff',
                        gender TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("SELECT COUNT(*) FROM members")
        if (c.fetchone()[0] or 0) == 0:
            seed = [
                ('龍ken (8000285)', 'staff', '男'),
                ('陳ken (80571)', 'staff', '男'),
                ('Fish (8047495)', 'staff', '女'),
                ('阿能 (8055532)', 'staff', '男'),
                ('阿威 (8055475)', 'staff', '男'),
                ('阿倫 (8017308)', 'staff', '男'),
                ('世外高喵 (298)', 'staff', '女'),
                ('Paco (8047393)', 'staff', '男'),
                ('小信 (8056027)', 'staff', '女'),
                ('kiko (1227)', 'staff', '女'),
                ('Wawa (1145)', 'staff', '男'),
                ('Ivy (8001161)', 'staff', '女'),
                ('秋龍 (67162)', 'staff', '女'),
                ('炳 (6234)', 'staff', '男'),
                ('Carman', 'follower', ''),
                ('David', 'follower', ''),
                ('Nam', 'follower', ''),
                ('Stella', 'follower', ''),
            ]
            c.executemany("""INSERT INTO members (name, mtype, gender) VALUES (%s, %s, %s)
                            ON CONFLICT (name) DO NOTHING""", seed)

        # 每人帳號（帳號＝員工ID，密碼＝BS＋員工ID）
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS account TEXT")
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS password TEXT")
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'staff'")
        c.execute("SELECT id, name, COALESCE(mtype, 'staff') FROM members WHERE COALESCE(account, '') = ''")
        for mid, nm, mt in c.fetchall():
            mm = re.search(r"[(（](\d+)[)）]", nm or '')
            if mm:
                acc = mm.group(1)
                c.execute("""UPDATE members SET account = %s, password = %s,
                                          role = COALESCE(NULLIF(role, ''), 'staff')
                             WHERE id = %s""", (acc, 'BS' + acc, mid))
            elif mt == 'follower':
                c.execute("""UPDATE members SET account = %s, password = %s,
                                          role = COALESCE(NULLIF(role, ''), 'staff')
                             WHERE id = %s""", (nm, nm + '888', mid))
        conn.commit()
        print("系統資料表檢查完成（app_settings / audit_logs）")
    except Exception as e:
        print("系統資料表檢查失敗:", e)
        if conn:
            try: conn.rollback()
            except Exception: pass
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


def get_setting(key, default=None):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = c.fetchone()
        return row[0] if row else default
    except Exception as e:
        print("讀取設定失敗:", e)
        return default
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


def set_setting(key, value):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW())
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                  (key, value))
        conn.commit()
        return True
    except Exception as e:
        print("寫入設定失敗:", e)
        if conn:
            try: conn.rollback()
            except Exception: pass
        return False
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


def log_action(action, detail=""):
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO audit_logs (actor, action, detail) VALUES (%s, %s, %s)",
                  (session.get('role', 'unknown'), action, detail))
        conn.commit()
    except Exception as e:
        print("寫入稽核紀錄失敗:", e)
        if conn:
            try: conn.rollback()
            except Exception: pass
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


# ---------- 強制登出（session 版本控制） ----------
_EPOCH_CACHE = {'staff': (0.0, None), 'admin': (0.0, None)}
EPOCH_TTL = 10  # 秒；即修改密碼後最多 10 秒內所有舊 session 失效


def get_epoch(role, force=False):
    role = 'admin' if role == 'admin' else 'staff'
    ts, val = _EPOCH_CACHE.get(role, (0.0, None))
    now = time.time()
    if not force and val is not None and (now - ts) < EPOCH_TTL:
        return val
    val = get_setting(f"{role}_epoch", '') or ''
    _EPOCH_CACHE[role] = (now, val)
    return val


def bump_epoch(role):
    """更新版本號 → 該角色所有已登入 session 於下次請求即失效。"""
    role = 'admin' if role == 'admin' else 'staff'
    val = str(int(time.time() * 1000))
    ok = set_setting(f"{role}_epoch", val)
    if ok:
        _EPOCH_CACHE[role] = (time.time(), val)
    return ok


# ---------- 簡單記憶體快取（單一 worker 有效，減少重複查詢） ----------
_RESULT_CACHE = {}


def cache_get(key):
    item = _RESULT_CACHE.get(key)
    if not item:
        return None
    expiry, val = item
    if time.time() > expiry:
        _RESULT_CACHE.pop(key, None)
        return None
    return val


def cache_set(key, val, ttl=60):
    _RESULT_CACHE[key] = (time.time() + ttl, val)


def cache_clear(*keys):
    for k in keys:
        _RESULT_CACHE.pop(k, None)


def init_config_order():
    """第一次啟動時，按舊有顯示次序寫入 cat_order / item_order（之後不會再改動）。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM venue_configs WHERE COALESCE(cat_order,0) <> 0 OR COALESCE(item_order,0) <> 0")
        if c.fetchone()[0] > 0:
            return
        c.execute("""SELECT category, problem, id FROM venue_configs
                     ORDER BY CASE
                        WHEN category = '賭檯 - Display(大Mon)' THEN 1
                        WHEN category = '賭檯 - Display(細Mon)' THEN 2
                        WHEN category = '賭檯 - Layout' THEN 3
                        WHEN category = '賭檯 - Sound Bar' THEN 4
                        ELSE 5 END, category ASC, id ASC""")
        rows = c.fetchall()
        cat_order = {}
        item_counter = {}
        for cat, prob, rid in rows:
            if cat not in cat_order:
                cat_order[cat] = (len(cat_order) + 1) * 10
                item_counter[cat] = 0
            item_counter[cat] += 1
            c.execute("UPDATE venue_configs SET cat_order = %s, item_order = %s WHERE id = %s",
                      (cat_order[cat], item_counter[cat], rid))
        conn.commit()
        print("選項排序初始化完成，共", len(cat_order), "個類別")
    except Exception as e:
        print("選項排序初始化失敗:", e)
        if conn: conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)


ensure_tables()
init_config_order()

# ---------- 舊域名自動跳轉新域名（令舊微信名片／舊連結仍然可用） ----------
PRIMARY_HOST = (os.environ.get('PRIMARY_HOST') or 'bs-shift.xyz').strip().lower()


@app.before_request
def force_primary_domain():
    """
    shift-app-wvgf.onrender.com 之類的舊網址 → 301 跳轉至新域名。
    保留：/healthz、任何 .txt（微信域名驗證檔）、/static/ 資源。
    """
    host = (request.host or '').split(':')[0].strip().lower()
    if not PRIMARY_HOST or not host:
        return
    if host == PRIMARY_HOST or host.endswith('.' + PRIMARY_HOST):
        return
    if not host.endswith('.onrender.com'):
        return
    if request.path.startswith('/static/') or request.path.endswith('.txt') or request.path == '/healthz':
        return

    qs = request.query_string.decode('utf-8', 'ignore')
    target = 'https://' + PRIMARY_HOST + request.path
    if qs:
        target += '?' + qs
    return redirect(target, code=301)


@app.route('/healthz')
def healthz():
    return "ok", 200, {'Content-Type': 'text/plain'}


@app.before_request
def require_login():
    allowed_routes = ['login', 'static', 'verify_wechat', 'catch_all_txt', 'healthz']
    endpoint = request.endpoint

    if 'logged_in' not in session:
        if endpoint in allowed_routes:
            return
        return redirect(url_for('login'))

    # 密碼被更改 / 被管理員強制登出 → 舊 session 一律失效
    role = session.get('role', 'staff')
    if session.get('epoch', '') != get_epoch(role):
        session.clear()
        if endpoint in allowed_routes:
            return
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "kick": True,
                            "message": "⏰ 你的登入已失效（密碼已被更改），請重新登入。"}), 401
        return redirect(url_for('login'))

@app.route('/bf392e0c1abdaeb5d994c628262a24a2.txt')
def verify_wechat():
    return "3d858c8569ab9afadbbc62d5f31c30007c34f14b", 200, {'Content-Type': 'text/plain'}

@app.route('/<path:filename>')
def catch_all_txt(filename):
    if 'bf392e0c1abdaeb5d994c628262a24a2' in filename:
        return "3d858c8569ab9afadbbc62d5f31c30007c34f14b", 200, {'Content-Type': 'text/plain'}
    return "Not Found", 404

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pwd = (request.form.get('password') or '').strip()
        staff_pwd = get_setting('staff_password', STAFF_PASSWORD) or STAFF_PASSWORD
        admin_pwd = get_setting('admin_password', ADMIN_PASSWORD) or ADMIN_PASSWORD

        if pwd and pwd == admin_pwd:
            session['logged_in'] = True
            session['role'] = 'admin'
            session['user_name'] = '管理員'
            session['epoch'] = get_epoch('admin', force=True)
            log_action('登入', '管理員登入')
            return redirect(url_for('index'))
        elif pwd and pwd == staff_pwd:
            session['logged_in'] = True
            session['role'] = 'staff'
            session['user_name'] = '同事'
            session['epoch'] = get_epoch('staff', force=True)
            log_action('登入', '同事登入')
            return redirect(url_for('index'))
        else:
            # 個人帳號（帳號＝員工ID，密碼＝BS＋員工ID）
            member = None
            if pwd:
                conn = None
                try:
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute("""SELECT name, COALESCE(role, 'staff')
                                 FROM members
                                 WHERE password = %s AND COALESCE(account, '') <> ''
                                 LIMIT 1""", (pwd,))
                    member = c.fetchone()
                except Exception as e:
                    print('個人帳號登入檢查失敗:', e)
                finally:
                    if conn:
                        release_db_connection(conn)

            if member:
                role = 'admin' if member[1] == 'admin' else 'staff'
                session['logged_in'] = True
                session['role'] = role
                session['user_name'] = member[0]
                session['epoch'] = get_epoch(role, force=True)
                log_action('登入', '個人帳號：%s' % member[0])
                return redirect(url_for('index'))
            error = "密碼錯誤！"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        # 普通帳號：直接返回首頁（前端亦已隱藏「設定」按鈕）
        return redirect(url_for('index'))
    return render_template('admin.html')

@app.route('/api/diag', methods=['GET'])
def diag():
    """診斷（純文字，方便手機查看）。"""
    cache_clear('configs')
    conn = None
    lines = []
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM venue_configs")
        lines.append('total_rows=' + str(c.fetchone()[0]))

        c.execute("SELECT COUNT(DISTINCT category) FROM venue_configs")
        lines.append('total_categories=' + str(c.fetchone()[0]))

        c.execute("""SELECT column_name FROM information_schema.columns
                     WHERE table_name = 'venue_configs' ORDER BY column_name""")
        lines.append('columns=' + ','.join(r[0] for r in c.fetchall()))

        c.execute("""SELECT COALESCE(cat_order,-1), COUNT(*) FROM venue_configs
                     GROUP BY COALESCE(cat_order,-1) ORDER BY 1 LIMIT 8""")
        lines.append('cat_order_values=' + ' | '.join(str(r[0]) + 'x' + str(r[1]) for r in c.fetchall()))

        lines.append('--- SQL 排序頭 5 個（repr 顯示隱藏字元）---')
        c.execute("""SELECT category, COALESCE(cat_order,0), COALESCE(item_order,0), id
                     FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC
                     LIMIT 5""")
        for r in c.fetchall():
            lines.append('  cat=' + str(r[1]) + ' item=' + str(r[2]) + ' id=' + str(r[3]) + ' name=' + repr(r[0]))

        lines.append('--- get_configs 頭 3 個 key ---')
        c.execute("""SELECT category FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        keys = []
        for r in c.fetchall():
            if r[0] not in keys:
                keys.append(r[0])
        for k in keys[:3]:
            lines.append('  ' + repr(k))

        lines.append('--- 可能有重複／相似類別（前 6）---')
        for k in keys[:6]:
            lines.append('  ' + repr(k) + ' 長度=' + str(len(k)))

        return Response('\n'.join(lines), mimetype='text/plain; charset=utf-8',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return Response('ERROR: ' + str(e), mimetype='text/plain; charset=utf-8')
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/get_configs', methods=['GET'])
def get_configs():
    cached = cache_get('configs')
    if cached is not None:
        resp = Response(json.dumps(cached, ensure_ascii=False), mimetype='application/json; charset=utf-8')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT category, problem FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        rows = c.fetchall()
        configs = {}
        for row in rows:
            if row[0] not in configs:
                configs[row[0]] = []
            configs[row[0]].append(row[1])
        cache_set('configs', configs, 300)
        resp = Response(json.dumps(configs, ensure_ascii=False), mimetype='application/json; charset=utf-8')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Get Configs Error:", e)
        return jsonify({})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/configs_detail', methods=['GET'])
def configs_detail():
    """回傳帶 id 的選項清單（admin 排序用）。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, category, problem FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        rows = c.fetchall()
        resp = jsonify([{"id": r[0], "category": r[1], "problem": r[2]} for r in rows])
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Configs Detail Error:", e)
        return jsonify([])
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/reorder_config', methods=['POST'])
def reorder_config():
    """上下移動類別或問題選項。"""
    data = request.json or {}
    rtype = (data.get('type') or '').strip()
    direction = (data.get('direction') or '').strip()
    if direction not in ('up', 'down'):
        return jsonify({"status": "error", "message": "❌ 移動方向不正確。"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if rtype == 'category':
            category = (data.get('category') or '').strip()
            if not category:
                return jsonify({"status": "error", "message": "❌ 缺少類別資料。"})
            c.execute("""SELECT category FROM venue_configs
                         GROUP BY category
                         ORDER BY MIN(COALESCE(cat_order,0)) ASC, category ASC""")
            names = [r[0] for r in c.fetchall()]
            if category not in names:
                return jsonify({"status": "error", "message": "❌ 找不到該類別。"})
            i = names.index(category)
            j = i - 1 if direction == 'up' else i + 1
            if j < 0 or j >= len(names):
                return jsonify({"status": "success", "message": "已移至最" + ('前' if direction == 'up' else '後') + "面。"})
            names[i], names[j] = names[j], names[i]
            for k, name in enumerate(names, start=1):
                c.execute("UPDATE venue_configs SET cat_order = %s WHERE category = %s", (k * 10, name))
            conn.commit()
            cache_clear('configs')
            return jsonify({"status": "success", "message": "✅ 已調整類別次序。"})

        if rtype == 'problem':
            rid = data.get('id')
            if not rid:
                return jsonify({"status": "error", "message": "❌ 缺少選項資料。"})
            c.execute("SELECT category FROM venue_configs WHERE id = %s", (rid,))
            row = c.fetchone()
            if not row:
                return jsonify({"status": "error", "message": "❌ 找不到該選項。"})
            c.execute("""SELECT id FROM venue_configs WHERE category = %s
                         ORDER BY COALESCE(item_order,0) ASC, id ASC""", (row[0],))
            ids = [r[0] for r in c.fetchall()]
            i = ids.index(rid)
            j = i - 1 if direction == 'up' else i + 1
            if j < 0 or j >= len(ids):
                return jsonify({"status": "success", "message": "已移至最" + ('前' if direction == 'up' else '後') + "面。"})
            ids[i], ids[j] = ids[j], ids[i]
            for k, oid in enumerate(ids, start=1):
                c.execute("UPDATE venue_configs SET item_order = %s WHERE id = %s", (k, oid))
            conn.commit()
            cache_clear('configs')
            return jsonify({"status": "success", "message": "✅ 已調整問題次序。"})

        return jsonify({"status": "error", "message": "❌ 未知的排序類型。"})
    except Exception as e:
        print("Reorder Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 排序失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)


def _cfg_prefix(name):
    """用於判斷同一個群組（例：『賭檯 - xxx』、『燈-xxx』）。"""
    n = (name or '').strip()
    for sep in (' - ', '-'):
        if sep in n:
            return n.split(sep, 1)[0].strip()
    return n


@app.route('/api/add_config', methods=['POST'])
def add_config():
    data = request.json or {}
    category = (data.get('category') or '').strip()
    problem = (data.get('problem') or '').strip()

    if not category or not problem:
        return jsonify({"status": "error", "message": "❌ 類別同問題內容都必須填寫。"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1 FROM venue_configs WHERE category = %s AND problem = %s", (category, problem))
        if c.fetchone():
            return jsonify({"status": "error", "message": "⚠️ 此選項已經存在。"})

        c.execute("""SELECT COALESCE(MAX(cat_order),0), COALESCE(MAX(item_order),0)
                     FROM venue_configs WHERE category = %s""", (category,))
        row = c.fetchone()
        cat_order = row[0] or 0
        item_order = (row[1] or 0) + 1
        is_new_cat = (cat_order == 0)
        if is_new_cat:
            c.execute("SELECT COALESCE(MAX(cat_order),0) FROM venue_configs")
            cat_order = (c.fetchone()[0] or 0) + 10
        c.execute("INSERT INTO venue_configs (category, problem, cat_order, item_order) VALUES (%s, %s, %s, %s)",
                  (category, problem, cat_order, item_order))

        if is_new_cat:
            # 新類別自動排入同一個群組後面（例：賭檯 - 新項目 會排在其他「賭檯」類別之後）
            c.execute("""SELECT category, MIN(COALESCE(cat_order,0)) AS o
                         FROM venue_configs GROUP BY category ORDER BY o ASC, category ASC""")
            order = [r[0] for r in c.fetchall() if r[0] != category]
            pos = len(order)
            prefix = _cfg_prefix(category)
            if prefix:
                for i, name in enumerate(order):
                    if _cfg_prefix(name) == prefix:
                        pos = i + 1
            order.insert(pos, category)
            for idx, name in enumerate(order):
                c.execute("UPDATE venue_configs SET cat_order = %s WHERE category = %s",
                          ((idx + 1) * 10, name))

        conn.commit()
        cache_clear('configs')
        log_action('新增選項', f"{category} / {problem}")
        return jsonify({"status": "success", "message": "✅ 新增成功！"})
    except Exception as e:
        print("Add Config Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 新增失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/delete_config', methods=['POST'])
def delete_config():
    """
    刪除場面問題選項（任何已登入的同事均可使用）。
    problem 有值 → 只刪該類別下的該個選項；
    problem 留空 → 刪除整個類別（連同該類別底下所有選項）。
    已提交的舊紀錄不受影響（紀錄儲存文字，不是儲存 id）。
    """
    data = request.json or {}
    category = (data.get('category') or '').strip()
    problem = (data.get('problem') or '').strip()

    if not category:
        return jsonify({"status": "error", "message": "❌ 缺少類別資料。"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if problem:
            c.execute("DELETE FROM venue_configs WHERE category = %s AND problem = %s", (category, problem))
        else:
            c.execute("DELETE FROM venue_configs WHERE category = %s", (category,))

        deleted = c.rowcount
        conn.commit()

        if deleted == 0:
            return jsonify({"status": "error", "message": "⚠️ 找不到要刪除的選項（可能已被其他人刪除）。"})

        cache_clear('configs')
        log_action('刪除選項', f"{category} / {problem or '整個類別'}（共 {deleted}）")
        return jsonify({"status": "success", "message": f"✅ 已刪除 {deleted} 個選項。", "deleted": deleted})
    except Exception as e:
        print("Delete Config Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 刪除失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/rename_config', methods=['POST'])
def rename_config():
    """修改類別名稱或問題名稱（任何已登入同事都可用）。"""
    data = request.json or {}
    rtype = (data.get('type') or '').strip()

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if rtype == 'category':
            old_cat = (data.get('old_category') or '').strip()
            new_cat = (data.get('new_category') or '').strip()
            if not old_cat or not new_cat:
                return jsonify({"status": "error", "message": "❌ 類別名稱不可空白。"})
            if old_cat == new_cat:
                return jsonify({"status": "success", "message": "✅ 名稱相同，無需修改。", "updated": 0})
            c.execute("SELECT 1 FROM venue_configs WHERE category = %s LIMIT 1", (new_cat,))
            if c.fetchone():
                return jsonify({"status": "error", "message": "⚠️ 已存在同名類別，請用其他名稱。"})
            c.execute("UPDATE venue_configs SET category = %s WHERE category = %s", (new_cat, old_cat))

        elif rtype == 'problem':
            category = (data.get('category') or '').strip()
            old_prob = (data.get('old_problem') or '').strip()
            new_prob = (data.get('new_problem') or '').strip()
            if not category or not old_prob or not new_prob:
                return jsonify({"status": "error", "message": "❌ 資料不完整。"})
            if old_prob == new_prob:
                return jsonify({"status": "success", "message": "✅ 名稱相同，無需修改。", "updated": 0})
            c.execute("SELECT 1 FROM venue_configs WHERE category = %s AND problem = %s", (category, new_prob))
            if c.fetchone():
                return jsonify({"status": "error", "message": "⚠️ 該類別已有同名問題，請用其他名稱。"})
            c.execute("UPDATE venue_configs SET problem = %s WHERE category = %s AND problem = %s",
                      (new_prob, category, old_prob))

        else:
            return jsonify({"status": "error", "message": "❌ 未知的修改類型。"})

        updated = c.rowcount
        conn.commit()

        if updated == 0:
            return jsonify({"status": "error", "message": "⚠️ 找不到要修改的資料。"})

        cache_clear('configs')
        log_action('修改選項名稱', f"{rtype} / 更新 {updated} 筆")
        return jsonify({"status": "success", "message": f"✅ 已更新 {updated} 筆。", "updated": updated})
    except Exception as e:
        print("Rename Config Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 修改失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now_macau = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        c.execute("""INSERT INTO records 
                     (name, shift, lux, temp, bgm, checklist, status, follower, on_duty, note, lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, layout_side, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('on_duty'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'),
                   data.get('layout_side'), now_macau))
        conn.commit()
        cache_clear('pending')
        return jsonify({"message": "交更成功！", "status": "success"})
    except Exception as e:
        print("Submit Error:", e)
        return jsonify({"status": "error", "message": "❌ 數據庫連線超時，請重新整理後再試！"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/overwrite_record', methods=['POST'])
def overwrite_record():
    data = request.json
    old_id = data.get('old_id')
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        if not old_id:
            return jsonify({"status": "error", "message": "❌ 系統錯誤：找不到要覆蓋的舊紀錄。"})

        c.execute("""SELECT created_at, COALESCE(reviewed, FALSE), reviewed_at, COALESCE(reviewed_by, '')
                     FROM records WHERE id = %s""", (old_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "❌ 找不到該紀錄！"})
        old_reviewed, old_reviewed_at, old_reviewed_by = row[1], row[2], row[3]

        if session.get('role') != 'admin':
            c.execute("SELECT NOW() - created_at < interval '5 minutes' FROM records WHERE id = %s", (old_id,))
            is_within_5_mins = c.fetchone()[0]
            if not is_within_5_mins:
                return jsonify({"status": "error", "message": "⏰ 已超過 5 分鐘修改時限，無法修改！"})

        c.execute("DELETE FROM records WHERE id = %s", (old_id,))

        now_macau = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        c.execute("""INSERT INTO records 
                     (name, shift, lux, temp, bgm, checklist, status, follower, on_duty, note, lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, layout_side, reviewed, reviewed_at, reviewed_by, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('on_duty'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'),
                   data.get('layout_side'), old_reviewed, old_reviewed_at, old_reviewed_by, now_macau))
        
        conn.commit()
        cache_clear('pending')
        log_action('修改紀錄', f"舊紀錄 ID {old_id}")
        return jsonify({"status": "success", "message": "✅ 紀錄已成功修改並覆蓋！"})
    except Exception as e:
        print("Overwrite Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": f"❌ 修改失敗：{str(e)}"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/records_home', methods=['GET'])
def get_records_home():
    """首頁用：分頁 + 伺服器端篩選（避免一次載入全部紀錄）。"""
    try:
        limit = min(max(int(request.args.get('limit', 60)), 1), 300)
    except Exception:
        limit = 60
    try:
        offset = max(int(request.args.get('offset', 0)), 0)
    except Exception:
        offset = 0

    date_from = (request.args.get('from') or '').strip()
    date_to = (request.args.get('to') or '').strip()
    q = (request.args.get('q') or '').strip()

    where = []
    params = []
    if date_from:
        where.append("(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date >= %s")
        params.append(date_from)
    if date_to:
        where.append("(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s")
        params.append(date_to)
    if q:
        like = '%' + q + '%'
        where.append("(name ILIKE %s OR checklist ILIKE %s OR note ILIKE %s"
                     " OR COALESCE(lux_issue,'') ILIKE %s OR COALESCE(temp_issue,'') ILIKE %s"
                     " OR COALESCE(db_issue,'') ILIKE %s OR COALESCE(guest_feedback,'') ILIKE %s"
                     " OR COALESCE(dlr_feedback,'') ILIKE %s"
                     " OR TO_CHAR(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau', 'YYYY-MM-DD HH24:MI:SS') ILIKE %s)")
        params += [like] * 9

    sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM records" + sql_where, params)
        total = c.fetchone()[0]

        c.execute("""SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note,
                            lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, layout_side, on_duty,
                            created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed, FALSE),
                            reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed_by, '')
                     FROM records""" + sql_where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                  params + [limit, offset])
        rows = c.fetchall()
        records = [{
            "id": r[0], "name": r[1], "shift": r[2], "lux": r[3], "temp": r[4], "bgm": r[5],
            "checklist": r[6], "status": r[7], "follower": r[8], "note": r[9],
            "lux_issue": r[10], "temp_issue": r[11], "db_issue": r[12],
            "guest_feedback": r[13], "dlr_feedback": r[14], "layout_side": r[15], "on_duty": r[16],
            "created_at": str(r[17]) if r[17] else None,
            "reviewed": bool(r[18]),
            "reviewed_at": str(r[19]) if r[19] else '',
            "reviewed_by": r[20] or ''
        } for r in rows]
        resp = jsonify({"records": records, "total": total, "limit": limit, "offset": offset})
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Records Home Error:", e)
        return jsonify({"records": [], "total": 0, "error": str(e)})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue,
                 guest_feedback, dlr_feedback, layout_side, on_duty,
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                 COALESCE(reviewed, FALSE),
                 reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                 COALESCE(reviewed_by, '')
                 FROM records ORDER BY id DESC""")
    rows = c.fetchall()
    release_db_connection(conn)
    records = []
    for row in rows:
        records.append({
            "id": row[0], "name": row[1], "shift": row[2], "lux": row[3], "temp": row[4], "bgm": row[5],
            "checklist": row[6], "status": row[7], "follower": row[8], "note": row[9],
            "lux_issue": row[10], "temp_issue": row[11], "db_issue": row[12],
            "guest_feedback": row[13], "dlr_feedback": row[14], "layout_side": row[15], "on_duty": row[16],
            "created_at": str(row[17]) if row[17] else None,
            "reviewed": bool(row[18]),
            "reviewed_at": str(row[19]) if row[19] else '',
            "reviewed_by": row[20] or ''
        })
    return jsonify(records)

# ---------- 匯出（伺服器端產生檔案，手機／微信都可下載） ----------
def _csv_bytes(rows):
    def esc(v):
        s = '' if v is None else str(v)
        return '"' + s.replace('"', '""') + '"'
    body = '\r\n'.join(','.join(esc(c) for c in row) for row in rows)
    return ('\ufeff' + body).encode('utf-8')


def _xls_bytes(rows, sheet_name):
    def x(v):
        s = '' if v is None else str(v)
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    body = ''.join('<Row>' + ''.join('<Cell><Data ss:Type="String">' + x(c) + '</Data></Cell>' for c in row) + '</Row>' for row in rows)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
           'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           '<Worksheet ss:Name="' + x(sheet_name) + '"><Table>' + body + '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


def _feedback_text(v):
    if not v:
        return ''
    try:
        d = json.loads(v)
        parts = []
        if d.get('items'):
            parts.append('內容：' + '、'.join(str(i) for i in d['items']))
        parts.append(f"好評{d.get('good', 0)}／中評{d.get('normal', 0)}／差評{d.get('bad', 0)}")
        if d.get('note'):
            parts.append('備註：' + str(d['note']))
        return '；'.join(parts)
    except Exception:
        return str(v)


def _file_response(rows, sheet_name, fmt):
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d_%H%M')
    if fmt == 'xls':
        data, mimetype, ext = _xls_bytes(rows, sheet_name), 'application/vnd.ms-excel', 'xls'
    else:
        data, mimetype, ext = _csv_bytes(rows), 'text/csv; charset=utf-8', 'csv'
    filename = f"BS_{sheet_name}_{stamp}.{ext}"
    return Response(data, mimetype=mimetype, headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


# ---------- 交更報表模板（圖1 格式） ----------
_REPORT_ROWS = [
    ('Hall1', ['Pit 1', 'Pit 2', 'Pit 30', 'Pit 32', 'Pit 35', 'Pit 31 LT']),
    ('Hall2', ['Pit 3', 'Pit 5', 'Pit 8', 'Pit 26', 'Pit 27', 'Pit 28', 'Pit 29']),
    ('Hall3', ['Pit 33', 'Pit 9', 'Pit 11', 'Pit 12']),
    ('Hall4', ['Pit 25', 'Pit 16']),
    ('Hall5', ['Pit 13', 'Pit 15', 'Pit 17', 'Pit 18', 'Pit 19', 'Pit 20', 'Pit 63 LT']),
    ('Hall6', ['Pit 103', 'Pit 105', 'Pit 106', 'Pit 107']),
]

_REPORT_LOC_DETAIL = {
    'Pit 9': 'Pit 9\nDS x3, BA x23',
    'Pit 11': 'Pit 11\nDS x6, BA x23',
}

_REPORT_ACTIVE_DETAIL = {
    'Pit 1': 'BJ x8,CS x5,RL x4\nFP x6,Craps x2',
}


def _xml_esc(v):
    s = '' if v is None else str(v)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
             .replace('"', '&quot;').replace('\n', '&#10;'))


def build_report_template():
    def cell(text='', style=None, across=0, down=0, formula=None):
        attrs = ''
        if across:
            attrs += ' ss:MergeAcross="' + str(across) + '"'
        if down:
            attrs += ' ss:MergeDown="' + str(down) + '"'
        if style:
            attrs += ' ss:StyleID="' + style + '"'
        if formula:
            attrs += ' ss:Formula="' + _xml_esc(formula) + '"'
            return '<Cell' + attrs + '/>'
        return ('<Cell' + attrs + '><Data ss:Type="String">' + _xml_esc(text) + '</Data></Cell>')

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')
    rows = []

    # 標題
    rows.append('<Row ss:Height="34">' + cell('Update ' + stamp, 'Title', across=6) + '</Row>')

    # 表頭（兩行）
    rows.append('<Row ss:Height="26">' +
                cell('Mass', 'Head', down=1) +
                cell('Location', 'Head', down=1) +
                cell('ALL', 'Head', across=1) +
                cell('Dealer Chair', 'Head', across=1) +
                cell('Total', 'Head', down=1) +
                '</Row>')
    rows.append('<Row ss:Height="22">' +
                cell('Active', 'HeadPink') +
                cell('Deactive', 'HeadYellow') +
                cell('New', 'Head') +
                cell('Old', 'Head') +
                '</Row>')

    first_data_row = len(rows) + 1
    for hall, pits in _REPORT_ROWS:
        for idx, pit in enumerate(pits):
            r = '<Row ss:Height="20">'
            if idx == 0:
                r += cell(hall, 'Mass', down=len(pits) - 1)
            loc_text = _REPORT_LOC_DETAIL.get(pit, pit)
            active_text = _REPORT_ACTIVE_DETAIL.get(pit, '')
            r += cell(loc_text, 'Loc')
            r += cell(active_text, 'Pink')
            r += cell('', 'Yellow')
            r += cell('', 'Plain')
            r += cell('', 'Plain')
            r += cell('', 'Green')
            r += '</Row>'
            rows.append(r)
    last_data_row = len(rows)

    # 合計行
    total_formula_tpl = '=SUM(R[-{}]C:R[-1]C)'.format(last_data_row - first_data_row + 1)
    rows.append('<Row ss:Height="24">' +
                cell('Total DLR Chair New/Old', 'TotalLabel', across=3) +
                cell(formula=total_formula_tpl, style='TotalCell') +
                cell(formula=total_formula_tpl, style='TotalCell') +
                cell(formula=total_formula_tpl, style='TotalCell') +
                '</Row>')

    border = ('<Borders>'
              '<Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Left" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Right" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Top" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '</Borders>')

    styles = (
        '<Styles>'
        '<Style ss:ID="Default" ss:Name="Normal">'
        '<Alignment ss:Vertical="Center" ss:Horizontal="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Title">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="20" ss:Bold="1" ss:Color="#2E74B5"/></Style>'
        '<Style ss:ID="Head">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadPink">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFD9D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadYellow">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFFF99" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Mass">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="Loc">'
        '<Alignment ss:Horizontal="Left" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Pink">'
        '<Interior ss:Color="#FFECEC" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Yellow">'
        '<Interior ss:Color="#FFFFE0" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Plain">' + border + '</Style>'
        '<Style ss:ID="Green">'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="TotalLabel">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="TotalCell">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="11" ss:Bold="1"/>'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '</Styles>'
    )

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
           ' xmlns:o="urn:schemas-microsoft-com:office:office"'
           ' xmlns:x="urn:schemas-microsoft-com:office:excel"'
           ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           + styles +
           '<Worksheet ss:Name="Update"><Table ss:DefaultRowHeight="18">'
           '<Column ss:Width="52"/><Column ss:Width="110"/>'
           '<Column ss:Width="150"/><Column ss:Width="80"/>'
           '<Column ss:Width="66"/><Column ss:Width="66"/><Column ss:Width="62"/>'
           + ''.join(rows) +
           '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


# ---------- 交更報表：由 hall_tables 自動填數 ----------
def build_update_report(data_rows):
    """data_rows: [(hall, pit, 營運數, 停運數), ...]"""

    def cell(text='', style=None, across=0, down=0, num=None):
        attrs = ''
        if across:
            attrs += ' ss:MergeAcross="' + str(across) + '"'
        if down:
            attrs += ' ss:MergeDown="' + str(down) + '"'
        if style:
            attrs += ' ss:StyleID="' + style + '"'
        if num is not None:
            return ('<Cell' + attrs + '><Data ss:Type="Number">' + str(num) + '</Data></Cell>')
        return ('<Cell' + attrs + '><Data ss:Type="String">' + _xml_esc(text) + '</Data></Cell>')

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')
    rows_xml = []
    rows_xml.append('<Row ss:Height="34">' + cell('Update ' + stamp, 'Title', across=5) + '</Row>')
    rows_xml.append('<Row ss:Height="26">' +
                    cell('Mass', 'Head', down=1) +
                    cell('Location', 'Head', down=1) +
                    cell('ALL', 'Head', across=1) +
                    cell('Total', 'Head', down=1) +
                    '</Row>')
    rows_xml.append('<Row ss:Height="22">' +
                    cell('Active', 'HeadPink') +
                    cell('Deactive', 'HeadYellow') +
                    '</Row>')

    grouped = {}
    order = []
    for hall, pit, act, dea in data_rows:
        if hall not in grouped:
            grouped[hall] = []
            order.append(hall)
        grouped[hall].append((pit, act, dea))

    sum_act = sum_dea = 0
    for hall in order:
        items = grouped[hall]
        for idx, (pit, act, dea) in enumerate(items):
            r = '<Row ss:Height="20">'
            if idx == 0:
                r += cell(hall, 'Mass', down=len(items) - 1)
            r += cell('Pit ' + str(pit), 'Loc')
            r += cell(num=act, style='Pink')
            r += cell(num=dea, style='Yellow')
            r += cell(num=act + dea, style='Green')
            r += '</Row>'
            rows_xml.append(r)
            sum_act += act
            sum_dea += dea

    rows_xml.append('<Row ss:Height="24">' +
                    cell('Total', 'TotalLabel', across=2) +
                    cell(num=sum_act, style='TotalCell') +
                    cell(num=sum_dea, style='TotalCell') +
                    cell(num=sum_act + sum_dea, style='TotalCell') +
                    '</Row>')

    return _xls_from_rows(rows_xml)


def _xls_from_rows(rows_xml):
    border = ('<Borders>'
              '<Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Left" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Right" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Top" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '</Borders>')

    styles = (
        '<Styles>'
        '<Style ss:ID="Default" ss:Name="Normal">'
        '<Alignment ss:Vertical="Center" ss:Horizontal="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Title">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="20" ss:Bold="1" ss:Color="#2E74B5"/></Style>'
        '<Style ss:ID="Head">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadPink">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFD9D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadYellow">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFFF99" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Mass">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="Loc">'
        '<Alignment ss:Horizontal="Left" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Pink">'
        '<Interior ss:Color="#FFECEC" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Yellow">'
        '<Interior ss:Color="#FFFFE0" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Green">'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="TotalLabel">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="TotalCell">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="11" ss:Bold="1"/>'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '</Styles>'
    )

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
           ' xmlns:o="urn:schemas-microsoft-com:office:office"'
           ' xmlns:x="urn:schemas-microsoft-com:office:excel"'
           ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           + styles +
           '<Worksheet ss:Name="Update"><Table ss:DefaultRowHeight="18">'
           '<Column ss:Width="56"/><Column ss:Width="90"/>'
           '<Column ss:Width="90"/><Column ss:Width="90"/><Column ss:Width="70"/>'
           + ''.join(rows_xml) +
           '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


# ---------- 真 .xlsx 產生器（顯示最穩定） ----------
def _xlsx_col(i):
    """1 -> A, 2 -> B ..."""
    out = ''
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def build_update_report_xlsx(data_rows):
    import io
    import zipfile
    from xml.sax.saxutils import escape as xe

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')

    grouped = {}
    order = []
    for hall, pit, act, dea in data_rows:
        if hall not in grouped:
            grouped[hall] = []
            order.append(hall)
        grouped[hall].append((pit, act, dea))

    rows_xml = []
    merges = []

    def row(idx, cells):
        parts = []
        for col, (style, val, is_num) in cells:
            ref = _xlsx_col(col) + str(idx)
            if is_num:
                parts.append(f'<c r="{ref}" s="{style}"><v>{val}</v></c>')
            else:
                parts.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{xe(str(val))}</t></is></c>')
        rows_xml.append(f'<row r="{idx}" ht="20" customHeight="1">' + ''.join(parts) + '</row>')

    # 標題
    rows_xml.append('<row r="1" ht="34" customHeight="1">'
                    f'<c r="A1" s="1" t="inlineStr"><is><t>Update {stamp}</t></is></c></row>')
    merges.append('A1:E1')

    # 表頭
    rows_xml.append('<row r="2" ht="26" customHeight="1">'
                    '<c r="A2" s="2" t="inlineStr"><is><t>Mass</t></is></c>'
                    '<c r="B2" s="2" t="inlineStr"><is><t>Location</t></is></c>'
                    '<c r="C2" s="2" t="inlineStr"><is><t>ALL</t></is></c>'
                    '<c r="E2" s="2" t="inlineStr"><is><t>Total</t></is></c></row>')
    merges += ['A2:A3', 'B2:B3', 'C2:D2', 'E2:E3']

    rows_xml.append('<row r="3" ht="22" customHeight="1">'
                    '<c r="C3" s="3" t="inlineStr"><is><t>Active</t></is></c>'
                    '<c r="D3" s="4" t="inlineStr"><is><t>Deactive</t></is></c></row>')

    idx = 4
    sum_act = sum_dea = 0
    for hall in order:
        items = grouped[hall]
        start = idx
        for j, (pit, act, dea) in enumerate(items):
            cells = []
            if j == 0:
                cells.append((1, (5, hall, False)))
            cells.append((2, (6, 'Pit ' + str(pit), False)))
            cells.append((3, (7, act, True)))
            cells.append((4, (8, dea, True)))
            cells.append((5, (9, act + dea, True)))
            row(idx, cells)
            sum_act += act
            sum_dea += dea
            idx += 1
        if len(items) > 1:
            merges.append('A' + str(start) + ':A' + str(idx - 1))

    # 合計行
    rows_xml.append(f'<row r="{idx}" ht="24" customHeight="1">'
                    f'<c r="A{idx}" s="10" t="inlineStr"><is><t>Total</t></is></c>'
                    f'<c r="C{idx}" s="11"><v>{sum_act}</v></c>'
                    f'<c r="D{idx}" s="11"><v>{sum_dea}</v></c>'
                    f'<c r="E{idx}" s="11"><v>{sum_act + sum_dea}</v></c></row>')
    merges += [f'A{idx}:B{idx}']

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<cols>'
             '<col min="1" max="1" width="11" customWidth="1"/>'
             '<col min="2" max="2" width="12" customWidth="1"/>'
             '<col min="3" max="4" width="12" customWidth="1"/>'
             '<col min="5" max="5" width="10" customWidth="1"/>'
             '</cols>'
             '<sheetData>' + ''.join(rows_xml) + '</sheetData>'
             '<mergeCells count="' + str(len(merges)) + '">' +
             ''.join(f'<mergeCell ref="{m}"/>' for m in merges) +
             '</mergeCells></worksheet>')

    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="4">'
              '<font><sz val="10"/><name val="Arial"/></font>'
              '<font><b/><sz val="10"/><name val="Arial"/></font>'
              '<font><b/><sz val="20"/><color rgb="FF2E74B5"/><name val="Arial"/></font>'
              '<font><b/><sz val="11"/><name val="Arial"/></font>'
              '</fonts>'
              '<fills count="8">'
              '<fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFCCFFFF"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFD9D9"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF99"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFECEC"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFE0"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFD9F2D9"/><bgColor indexed="64"/></patternFill></fill>'
              '</fills>'
              '<borders count="2">'
              '<border><left/><right/><top/><bottom/><diagonal/></border>'
              '<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border>'
              '</borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="12">'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="3" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '</cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              '</styleSheet>')

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '</Types>')

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Update" sheetId="1" r:id="rId1"/></sheets></workbook>')

    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


# ---------- 完整備份 / 還原 ----------
_BACKUP_COLUMNS = ['id', 'name', 'shift', 'lux', 'temp', 'bgm', 'checklist', 'status', 'follower',
                   'on_duty', 'note', 'lux_issue', 'temp_issue', 'db_issue', 'guest_feedback',
                   'dlr_feedback', 'layout_side', 'reviewed', 'reviewed_at', 'reviewed_by', 'created_at']


def _rows_as_dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.route('/api/backup/export', methods=['GET'])
def backup_export():
    """下載完整備份（JSON）：紀錄、選項、台號總表、系統設定、操作紀錄。"""
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以備份。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM records ORDER BY id")
        records = _rows_as_dicts(c)
        c.execute("SELECT * FROM venue_configs ORDER BY id")
        configs = _rows_as_dicts(c)
        c.execute("SELECT * FROM hall_tables ORDER BY id")
        halls = _rows_as_dicts(c)
        c.execute("SELECT * FROM app_settings")
        settings = _rows_as_dicts(c)
        c.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 2000")
        logs = _rows_as_dicts(c)

        now = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        payload = {
            'format': 'bs-shift-backup',
            'version': 1,
            'exported_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'counts': {'records': len(records), 'configs': len(configs),
                       'hall_tables': len(halls), 'settings': len(settings), 'audit_logs': len(logs)},
            'records': records,
            'venue_configs': configs,
            'hall_tables': halls,
            'app_settings': settings,
            'audit_logs': logs,
        }
        body = json.dumps(payload, ensure_ascii=False, default=str, indent=1)
        fname = 'bs_backup_' + now.strftime('%Y%m%d_%H%M') + '.json'
        log_action('下載備份', '紀錄 %d 筆' % len(records))
        return Response(body, mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename="%s"' % fname})
    except Exception as e:
        print('Backup Error:', e)
        return jsonify({'status': 'error', 'message': '❌ 備份失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/backup/import_records', methods=['POST'])
def backup_import_records():
    """由備份還原交更紀錄（只新增，不覆蓋／不刪除）。"""
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以還原。'})

    data = request.json or {}
    rows = data.get('records')
    if not isinstance(rows, list) or not rows:
        return jsonify({'status': 'error', 'message': '❌ 備份檔內沒有紀錄。'})

    allowed = set(_BACKUP_COLUMNS)
    added = skipped = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        for r in rows[:20000]:
            if not isinstance(r, dict):
                skipped += 1
                continue
            cols = [k for k in _BACKUP_COLUMNS if k in r]
            if 'id' not in cols:
                skipped += 1
                continue
            vals = [r[k] for k in cols]
            sql = 'INSERT INTO records (%s) VALUES (%s) ON CONFLICT (id) DO NOTHING RETURNING id' % (
                ','.join(cols), ','.join(['%s'] * len(cols)))
            c.execute('SAVEPOINT sp')
            try:
                c.execute(sql, vals)
                inserted = c.fetchone()
                c.execute('RELEASE SAVEPOINT sp')
                if inserted:
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                print('還原單筆失敗:', e)
                c.execute('ROLLBACK TO SAVEPOINT sp')
                skipped += 1

        c.execute("SELECT setval(pg_get_serial_sequence('records','id'), "
                  "COALESCE((SELECT MAX(id) FROM records), 1))")
        conn.commit()
        log_action('還原紀錄', '新增 %d 筆，略過 %d 筆' % (added, skipped))
        return jsonify({'status': 'success',
                        'message': '✅ 還原完成：新增 %d 筆，略過 %d 筆（已存在或格式不符）。' % (added, skipped),
                        'added': added, 'skipped': skipped})
    except Exception as e:
        print('Restore Error:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 還原失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/export/report_update', methods=['GET'])
def export_report_update():
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""select hall, pit,
                            count(*) filter (where status = '營運') as act,
                            count(*) filter (where status = '停運') as dea
                     from hall_tables
                     group by hall, pit
                     order by hall, pit""")
        rows = c.fetchall()
    except Exception as e:
        print("Report Update Error:", e)
        return jsonify({"status": "error",
                        "message": "❌ 讀取不通。請先執行「台號總表_seed.sql」建立 hall_tables。"})
    finally:
        if conn:
            release_db_connection(conn)

    if not rows:
        return jsonify({"status": "error", "message": "❌ hall_tables 沒有任何資料。"})

    data = [(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in rows]
    payload = build_update_report_xlsx(data)
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d')
    filename = 'BS_update_' + stamp + '.xlsx'
    return Response(payload, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


@app.route('/api/export/report_template', methods=['GET'])
def export_report_template():
    data = build_report_template()
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d')
    filename = 'BS_report_template_' + stamp + '.xls'
    return Response(data, mimetype='application/vnd.ms-excel', headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


def _unit_temp_text(v):
    s = str(v or '').strip()
    if not s:
        return ''
    if '°' in s or '℃' in s:
        return s
    if '偏低' in s:
        return '偏低（< 22.5 °C）'
    if '偏高' in s:
        return '偏高（> 24.5 °C）'
    if '正常' in s:
        return '正常（22.5–24.5 °C）'
    return s


def _unit_bgm_text(v):
    s = str(v or '').strip()
    if not s:
        return ''
    if 'db' in s.lower():
        return s
    if '偏低' in s:
        return '偏低（< 65 dB）'
    if '偏高' in s:
        return '偏高（> 75 dB）'
    if '正常' in s:
        return '正常（65–75 dB）'
    return s


@app.route('/api/export/records', methods=['GET'])
def export_records_file():
    date_from = (request.args.get('from') or '').strip()
    date_to = (request.args.get('to') or '').strip()
    fmt = (request.args.get('format') or 'csv').lower()

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sql = """SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note,
                        lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback,
                        created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau'
                 FROM records WHERE 1=1"""
        params = []
        if date_from:
            sql += " AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date >= %s"
            params.append(date_from)
        if date_to:
            sql += " AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s"
            params.append(date_to)
        sql += " ORDER BY id DESC"
        c.execute(sql, params)
        rows = c.fetchall()
    except Exception as e:
        print("Export Records Error:", e)
        return jsonify({"status": "error", "message": "❌ 匯出失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

    out = [['日期時間', '交更人', '更份', '全場Lux', '全場溫度', '全場分貝', '狀態', 'NFC檢測Hall',
            'Checklist', '詳細事項', 'Lux異常', '溫度異常', '分貝異常', '客人意見', 'DLR意見']]
    for r in rows:
        checklist = r[6] or ''
        hall = ''
        for seg in checklist.split(' | '):
            s = seg.strip()
            if s.startswith('NFC正常'):
                hall = s.replace('NFC正常:', '').replace('NFC正常：', '').strip()
                break
        out.append([
            str(r[15]) if r[15] else '', r[1], r[2], r[3], _unit_temp_text(r[4]), _unit_bgm_text(r[5]), r[7], hall,
            checklist, r[9] or '', r[10] or '', r[11] or '', r[12] or '',
            _feedback_text(r[13]), _feedback_text(r[14])
        ])
    return _file_response(out, 'records', fmt)


@app.route('/api/export/configs', methods=['GET'])
def export_configs_file():
    fmt = (request.args.get('format') or 'csv').lower()
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT category, problem FROM venue_configs ORDER BY category ASC, id ASC")
        rows = c.fetchall()
    except Exception as e:
        print("Export Configs Error:", e)
        return jsonify({"status": "error", "message": "❌ 匯出失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

    out = [['類別', '問題']]
    for r in rows:
        out.append([r[0] or '', r[1] or ''])
    return _file_response(out, 'options', fmt)


@app.route('/api/pending_issues', methods=['GET'])
def get_pending_issues():
    cached = cache_get('pending')
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT note FROM records WHERE note LIKE '%【早前已處理】%'")
    done_rows = c.fetchall()
    done_items = set()
    for r in done_rows:
        if r[0] and '【早前已處理】' in r[0]:
            part = r[0].split('【早前已處理】')[1]
            if '【早前未處理】' in part:
                part = part.split('【早前未處理】')[0]
            for line in part.split('\n'):
                if line.strip(): 
                    clean_line = re.sub(r'\[狀態:.*?\]', '', line).strip().upper()
                    if '位置:' in clean_line and (',' in clean_line or '，' in clean_line):
                        parts = clean_line.split('位置:')
                        if len(parts) == 2:
                            before = parts[0].strip()
                            after = parts[1].strip()
                            tables = re.split(r'[,，]', after)
                            for t in tables:
                                t = t.strip()
                                if t: done_items.add(f"{before} 位置: {t}")
                    else:
                        done_items.add(clean_line)

    c.execute("""SELECT id, name, lux_issue, temp_issue, db_issue, note, status, checklist,
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records 
                 WHERE status NOT IN ('已處理') 
                 AND (lux_issue != '' OR temp_issue != '' OR db_issue != '' 
                      OR note LIKE '%【場面問題】%' OR note LIKE '%【早前未處理】%'
                      OR checklist LIKE '%NFC異常%')
                 ORDER BY id DESC""")
    rows = c.fetchall()
    release_db_connection(conn)
    pending = []
    for row in rows:
        pending.append({
            "id": row[0], "name": row[1], "lux_issue": row[2], "temp_issue": row[3], 
            "db_issue": row[4], "note": row[5], "overall_status": row[6],
            "checklist": row[7],
            "created_at": str(row[8]) if row[8] else None
        })
    payload = {"issues": pending, "done_items": list(done_items)}
    cache_set('pending', payload, 45)
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp

@app.route('/api/delete_record', methods=['POST'])
def delete_record():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 權限不足！只有管理員可以刪除紀錄。"})
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM records WHERE id = %s", (data.get('id'),))
    conn.commit()
    release_db_connection(conn)
    cache_clear('pending')
    log_action('刪除紀錄', f"紀錄 ID {data.get('id')}")
    return jsonify({"status": "success", "message": "✅ 紀錄已成功刪除！"})

@app.route('/api/record_review', methods=['POST'])
def record_review():
    """已檢閱（👍）：可多人累加／取消。
    action: add（加自己名）／remove（移除自己名）／clear（全部取消）
    """
    data = request.json or {}
    rid = data.get('id')
    by_name = (data.get('by') or '').strip()[:40]
    action = (data.get('action') or ('add' if data.get('reviewed') else 'clear')).strip()
    if action not in ('add', 'remove', 'clear'):
        action = 'add' if data.get('reviewed') else 'clear'
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到紀錄。'})
    if action in ('add', 'remove') and not by_name:
        return jsonify({'status': 'error', 'message': '❌ 請選擇檢閱人。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COALESCE(reviewed_by, '') FROM records WHERE id = %s", (rid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該紀錄！'})

        names = [n.strip() for n in (row[0] or '').split(',') if n.strip()]
        if action == 'add':
            if by_name and by_name not in names:
                names.append(by_name)
        elif action == 'remove':
            names = [n for n in names if n != by_name]
        else:
            names = []

        if names:
            c.execute("""UPDATE records SET reviewed = TRUE, reviewed_at = NOW(), reviewed_by = %s
                         WHERE id = %s""", (', '.join(names), rid))
        else:
            c.execute("""UPDATE records SET reviewed = FALSE, reviewed_at = NULL, reviewed_by = NULL
                         WHERE id = %s""", (rid,))

        c.execute("""SELECT COALESCE(reviewed, FALSE),
                            reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed_by, '')
                     FROM records WHERE id = %s""", (rid,))
        row = c.fetchone()
        conn.commit()
        if action == 'add':
            log_action('已檢閱', '紀錄 ID %s（%s）' % (rid, by_name))
        elif action == 'remove':
            log_action('取消檢閱', '紀錄 ID %s（%s）' % (rid, by_name))
        else:
            log_action('取消檢閱', '紀錄 ID %s（全部）' % rid)
        return jsonify({'status': 'success',
                        'reviewed': bool(row[0]) if row else False,
                        'reviewed_at': str(row[1]) if row and row[1] else '',
                        'reviewed_by': (row[2] or '') if row else ''})
    except Exception as e:
        print('Review Error:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 操作失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/update_record', methods=['POST'])
def update_record():
    data = request.json
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT created_at FROM records WHERE id = %s", (data.get('id'),))
        row = c.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "❌ 找不到該紀錄！"})
            
        if session.get('role') != 'admin':
            c.execute("SELECT NOW() - created_at < interval '5 minutes' FROM records WHERE id = %s", (data.get('id'),))
            is_within_5_mins = c.fetchone()[0]
            if not is_within_5_mins: 
                return jsonify({"status": "error", "message": "⏰ 已超過 5 分鐘修改時限，無法修改！"})
                
        c.execute("UPDATE records SET note = %s, status = %s WHERE id = %s", (data.get('note'), data.get('status'), data.get('id')))
        conn.commit()
        cache_clear('pending')
        return jsonify({"status": "success", "message": "✅ 紀錄已成功更新！"})
    except Exception as e:
        print("Update Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 更新失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/get_auth_status', methods=['GET'])
def get_auth_status():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 權限不足！"})
    staff_pwd = get_setting('staff_password', STAFF_PASSWORD) or STAFF_PASSWORD
    admin_pwd = get_setting('admin_password', ADMIN_PASSWORD) or ADMIN_PASSWORD
    return jsonify({
        "status": "success",
        "role": session.get('role'),
        "staff_custom": get_setting('staff_password') is not None,
        "admin_custom": get_setting('admin_password') is not None,
        "staff_len": len(staff_pwd),
        "admin_len": len(admin_pwd)
    })


@app.route('/api/change_password', methods=['POST'])
def change_password():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 只有管理員可以改密碼。"})

    data = request.json or {}
    target = data.get('target')
    new_pwd = (data.get('new_password') or '').strip()
    cur_admin = get_setting('admin_password', ADMIN_PASSWORD) or ADMIN_PASSWORD

    if (data.get('current_admin_password') or '') != cur_admin:
        return jsonify({"status": "error", "message": "❌ 目前管理員密碼不正確。"})
    if target not in ('staff', 'admin'):
        return jsonify({"status": "error", "message": "❌ 目標帳號不正確。"})
    if len(new_pwd) < 4:
        return jsonify({"status": "error", "message": "❌ 新密碼最少 4 個字元。"})

    key = 'staff_password' if target == 'staff' else 'admin_password'
    if not set_setting(key, new_pwd):
        return jsonify({"status": "error", "message": "❌ 儲存失敗，請稍後再試。"})

    # 令舊 session 全部失效
    bump_epoch('staff' if target == 'staff' else 'admin')

    if target == 'admin':
        # 保持目前這個管理員 session 有效，不會將自己登出
        session['epoch'] = get_epoch('admin', force=True)
        msg = "✅ 管理員密碼已更新（其他已登入的管理員會被強制登出）。"
    else:
        msg = "✅ 同事密碼已更新，所有已登入的同事已被強制登出（最多 10 秒內生效）。"

    log_action('更改密碼', '帳號：' + ('同事' if target == 'staff' else '管理員'))
    return jsonify({"status": "success", "message": msg})


@app.route('/api/kick_sessions', methods=['POST'])
def kick_sessions():
    """不修改密碼，直接登出所有已登入的同事（適合有人離職）。"""
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 只有管理員可以強制登出。"})

    data = request.json or {}
    target = data.get('target') or 'staff'
    if target not in ('staff', 'admin'):
        return jsonify({"status": "error", "message": "❌ 目標帳號不正確。"})

    if not bump_epoch(target):
        return jsonify({"status": "error", "message": "❌ 執行失敗，請稍後再試。"})

    if target == 'admin':
        session['epoch'] = get_epoch('admin', force=True)
        label = '管理員（你本人不受影響）'
    else:
        label = '同事'

    log_action('強制登出', '目標：' + label)
    return jsonify({"status": "success",
                    "message": f"✅ 已強制登出所有{label} session（最多 10 秒內生效）。"})


@app.route('/api/audit_logs', methods=['GET'])
def audit_logs():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 權限不足！", "logs": []})

    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
    except Exception:
        limit = 100

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, actor, action, detail,
                            created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau'
                     FROM audit_logs ORDER BY id DESC LIMIT %s""", (limit,))
        rows = c.fetchall()
        logs = [{"id": r[0], "actor": r[1], "action": r[2], "detail": r[3],
                 "created_at": str(r[4]) if r[4] else None} for r in rows]
        return jsonify({"status": "success", "logs": logs})
    except Exception as e:
        print("讀取稽核紀錄失敗:", e)
        return jsonify({"status": "error", "message": "❌ 讀取失敗。", "logs": []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/clear_audit_logs', methods=['POST'])
def clear_audit_logs():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 只有管理員可以清理。"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM audit_logs WHERE created_at < NOW() - interval '90 days'")
        deleted = c.rowcount
        conn.commit()
        return jsonify({"status": "success", "message": f"✅ 已清理 {deleted} 條（90 日前）紀錄。", "deleted": deleted})
    except Exception as e:
        print("清理稽核紀錄失敗:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 清理失敗。"})
    finally:
        if conn:
            release_db_connection(conn)


# ---------- 更新表（任何類別，一鍵生成） ----------
# 交更表單記錄格式：  [賭檯 - Layout - 污跡] 位置: NB0201 [狀態:已 Send mail]
# 本功能把「已 Send mail」的台號抽出，按類別／問題分欄，產生主管查看的更新表。
_UP_KEY = 'update_reports'
_UP_MAPKEY = 'update_map'
_UP_EXCLKEY = 'update_excluded'
_UP_LAYOUT_COLS = [
    ('Layout', 'Stain Nearly Betting Area'),
    ('Layout', 'Stain Nearly Dealer Area'),
    ('Thin Layout', 'Thin Layout Affects Dealing'),
]
_UP_CODE_RE = re.compile(r'^([A-Z]{1,4})?(\d{3,6})$')
_UP_MAX_COLS = 12


def _up_today():
    return datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y-%m-%d')


def _up_now_text():
    return datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%m/%d %H:%M:%S')


def _up_norm(v):
    s = '' if v is None else str(v)
    s = s.replace('\u3000', ' ').strip().upper()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[（(].*?[)）]', '', s)
    return s


def _up_is_code(s):
    return bool(_UP_CODE_RE.match(s or ''))


def _up_short_cat(cat):
    c = (cat or '').strip()
    if ' - ' in c:
        return c.split(' - ', 1)[1].strip()
    return c or '（未分類）'


def _up_mailish(status):
    s = (status or '').lower()
    return ('send' in s) and ('mail' in s)


def _up_is_layout(cat, problem):
    t = ((cat or '') + ' ' + (problem or '')).lower()
    return 'layout' in t


def _up_layout_guess(problem, side=''):
    p = (problem or '').lower()
    if '薄' in p or 'thin' in p:
        return 2
    if side == '客位':
        return 0
    if side == 'DLR位':
        return 1
    return None


def _up_take_note(line):
    """抽出行尾的「備註：…」內容。"""
    m = re.search(r'備註\s*[:：]\s*(.+)$', str(line or ''))
    return m.group(1).strip() if m else ''


def _up_parse_metric(raw):
    """解析 Lux／溫度／分貝 異常行：NB0201 (Box 1: 380) [狀態:已 Send mail]"""
    s = str(raw or '').strip()
    if not s:
        return None
    m = re.search(r'([A-Za-z]{1,4})\s*[-–]?\s*(\d{3,6})', s)
    if not m:
        return None
    code = m.group(1).upper() + m.group(2)
    if not _up_is_code(code):
        return None
    st = re.search(r'\[狀態\s*[:：]\s*([^\]]*)\]', s)
    return {'code': code, 'status': st.group(1).strip() if st else '',
            'note': _up_take_note(s)}


def _up_parse_line(line):
    """解析單行： [類別 - 問題] 位置: 台號 [狀態:xxx]"""
    if not line or '位置' not in line:
        return []
    m = re.search(r'\[([^\[\]]+?)\]\s*位置\s*[:：]\s*(.+)', line)
    if not m:
        return []
    parts = [p.strip() for p in m.group(1).split(' - ')]
    problem = parts[-1] if parts else ''
    category = ' - '.join(parts[:-1]).strip() if len(parts) > 1 else ''
    loc_text = m.group(2)
    st = re.search(r'\[狀態\s*[:：]\s*([^\]]*)\]', loc_text)
    status = st.group(1).strip() if st else ''
    if st:
        loc_text = loc_text[:st.start()]
    out = []
    note_text = _up_take_note(line)
    for raw in re.split(r'[,，、;；]', loc_text):
        code = _up_norm(raw)
        if not _up_is_code(code):
            # 台號後面可能跟住描述（例：NB1713 #1位內面、DS1106 A證…），抽出前後字母＋數字
            mm = re.search(r'([A-Za-z]{1,4})\s*[-–]?\s*(\d{3,6})', raw)
            code = (mm.group(1).upper() + mm.group(2)) if mm else ''
        if code and _up_is_code(code):
            out.append({'code': code, 'category': category,
                        'problem': problem, 'status': status, 'note': note_text})
    return out


def _up_split_sections(note):
    """把備註按 【標題】 分段落。"""
    text = str(note or '')
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'【([^】]+)】', text)]
    if not marks:
        return {'': text}
    out = {}
    if marks[0][0] > 0:
        out[''] = text[:marks[0][0]]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        seg = re.sub(r'^【[^】]+】', '', text[pos:end])
        out[name] = (out.get(name, '') + '\n' + seg)
    return out


_NFC_RE = re.compile(r'NFC\s*異常\s*[:：]\s*(.*?)\s*[（(]\s*Box\s*[:：]\s*(.*?)\s*[)）]\s*(?:\[狀態\s*[:：]\s*([^\]]*)\])?')
_NFC_DONE_RE = re.compile(r'NFC\s*[-－]\s*(.*?)\s*[（(]\s*Box\s*[:：]\s*(.*?)\s*[)）]')


def _up_parse_nfc(text, done=False):
    """解析 NFC 異常： NFC異常: NB0201 (Box: Box 3) [狀態:...] / NFC - NB0201 (Box: Box 3)"""
    out = []
    if not text:
        return out
    rx = _NFC_DONE_RE if done else _NFC_RE
    for m in rx.finditer(str(text)):
        code = _up_norm(m.group(1))
        boxes = (m.group(2) or '').strip()
        status = ''
        if not done and m.lastindex and m.lastindex >= 3:
            status = (m.group(3) or '').strip()
        out.append({'code': code, 'boxes': boxes, 'status': status})
    return out


def _up_nfc_problem(boxes):
    b = (boxes or '').strip()
    if not b:
        return 'Box 未填'
    return re.sub(r'\s*[,，]\s*', ' + ', b)


def _up_parse_note(note):
    out = []
    if not note:
        return out
    for line in str(note).split('\n'):
        out.extend(_up_parse_line(line))
    return out


def _up_detect(date_str):
    """截至指定日期，每個「類別＋台號」最新一次的資料。
    回傳 (out, done_at, nfc_nocode)
    """
    rows = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT COALESCE(note, ''),
                            (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date,
                            COALESCE(layout_side, ''),
                            COALESCE(checklist, ''),
                            COALESCE(lux_issue, ''),
                            COALESCE(temp_issue, ''),
                            COALESCE(db_issue, '')
                     FROM records
                     WHERE (COALESCE(note, '') <> '' OR COALESCE(checklist, '') <> '')
                       AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s
                     ORDER BY id ASC""", (date_str,))
        rows = c.fetchall()
    except Exception as e:
        print('更新表讀取紀錄失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    out = {}
    sides = {}
    done_at = {}
    nfc_nocode = []

    def upsert(cat, code, problem, status, ds, note=''):
        key = (cat, code)
        rec = out.get(key)
        if rec is None:
            out[key] = {'category': cat, 'code': code, 'problem': problem, 'status': status,
                        'note': note or '',
                        'first_seen': ds, 'last_seen': ds}
        else:
            rec['problem'] = problem
            rec['status'] = status
            if note:
                rec['note'] = note
            if ds > rec['last_seen']:
                rec['last_seen'] = ds

    for row in rows:
        note = row[0]
        d = row[1]
        side_json = row[2] if len(row) > 2 else ''
        checklist = row[3] if len(row) > 3 else ''
        lux_issue = row[4] if len(row) > 4 else ''
        temp_issue = row[5] if len(row) > 5 else ''
        db_issue = row[6] if len(row) > 6 else ''
        ds = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]
        if side_json:
            try:
                sd = json.loads(side_json)
                if isinstance(sd, dict):
                    for k, v in sd.items():
                        ck = _up_norm(k)
                        if ck and v in ('客位', 'DLR位'):
                            sides[ck] = v
            except Exception:
                pass

        # NFC 異常（存在 checklist 欄位）
        for it in _up_parse_nfc(checklist):
            if it['code']:
                upsert('NFC', it['code'], _up_nfc_problem(it['boxes']), it['status'], ds)
            else:
                nfc_nocode.append(it['status'])

        # Lux／溫度／分貝 異常（存在獨立欄位，也是異常類型）
        for raw in str(lux_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('Lux 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')
        for raw in str(temp_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('溫度 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')
        for raw in str(db_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('分貝 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')

        secs = _up_split_sections(note)
        # 【早前已處理】＝已經完成，不應再出現在更新表
        # 以「類別＋台號」判斷（因為有時同事沒有填寫問題，只填台號）
        for line in secs.get('早前已處理', '').split('\n'):
            for it in _up_parse_line(line):
                done_at[(it['category'], it['code'])] = (ds, it['problem'])
            for it in _up_parse_nfc(line, done=True):
                if it['code']:
                    done_at[('NFC', it['code'])] = (ds, _up_nfc_problem(it['boxes']))
            for label, catname in (('Lux', 'Lux 異常'), ('溫度', '溫度 異常'), ('分貝', '分貝 異常')):
                if line.strip().startswith(label + ' -'):
                    it = _up_parse_metric(line)
                    if it:
                        done_at[(catname, it['code'])] = (ds, '數值異常')

        for name, seg in secs.items():
            # 「早前已處理」＝完成標記；「早前未處理」只是上一張紀錄的文字副本，
            # 原始台號仍會從原本那張紀錄抽出，所以兩段都不作新項目，避免舊台號殘留。
            if name in ('早前已處理', '早前未處理'):
                continue
            for line in seg.split('\n'):
                for it in _up_parse_line(line):
                    if not it['category']:
                        continue
                    upsert(it['category'], it['code'], it['problem'], it['status'], ds, it.get('note') or '')

    for key in list(out.keys()):
        rec = out[key]
        info = done_at.get(key)
        if info:
            dd, dprob = info
            if dd >= rec['last_seen']:
                # 若「已完成」的問題與最新上報的問題不同，不當作已完成（避免新問題被誤刪）
                if (not dprob) or (not rec['problem']) or dprob == rec['problem']:
                    out.pop(key, None)
                    continue
        rec['side'] = sides.get(rec['code'], '')

    return out, done_at, nfc_nocode


def _up_table_lookup():
    lut = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code, hall, pit FROM hall_tables")
        for code, hall, pit in c.fetchall():
            key = _up_norm(code)
            if key and key not in lut:
                h = str(hall or '')
                h = re.sub(r'(?i)^hall\s*', '', h).strip()
                lut[key] = {'hall': h, 'pit': str(pit)}
    except Exception as e:
        print('更新表讀取台號總表失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass
    return lut


def _up_fallback_hp(code):
    m = re.match(r'^([A-Z]{1,4})?(\d{3,6})$', code or '')
    if not m:
        return '', ''
    d = m.group(2)
    pit = d[:-2] if len(d) >= 5 else (d[:2] if len(d) == 4 else d)
    try:
        return '', str(int(pit))
    except Exception:
        return '', ''


def _up_load_json(key, default):
    raw = get_setting(key, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else default
    except Exception:
        data = default
    return data if isinstance(data, type(default)) else default


def _up_load_reports():
    return _up_load_json(_UP_KEY, {})


def _up_store_reports(data):
    try:
        keys = sorted(data.keys())
        for k in keys[:-400]:
            data.pop(k, None)
    except Exception:
        pass
    return set_setting(_UP_KEY, json.dumps(data, ensure_ascii=False))


def _up_load_map():
    return _up_load_json(_UP_MAPKEY, {})


def _up_load_excl():
    return _up_load_json(_UP_EXCLKEY, {})


def _up_key(date_str, rtype):
    return date_str + '|' + rtype


@app.route('/api/update_report_cats', methods=['GET'])
def update_report_cats():
    date_str = (request.args.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        date_str = _up_today()

    det, _done_map, _nfc_nocode = _up_detect(date_str)
    agg = {}
    layout_mail = layout_all = 0
    for (cat_key, code), e in det.items():
        cat = e['category'] or ''
        rec = agg.setdefault(cat, {'mail': 0, 'all': 0})
        rec['all'] += 1
        if _up_mailish(e['status']):
            rec['mail'] += 1
        if _up_is_layout(cat, e['problem']):
            layout_all += 1
            if _up_mailish(e['status']):
                layout_mail += 1

    cats = [{'cat': k, 'short': _up_short_cat(k), 'mail': v['mail'], 'all': v['all']}
            for k, v in agg.items() if k and not _up_is_layout(k, '')]
    cats.sort(key=lambda x: (-x['mail'], -x['all'], x['short']))
    resp = jsonify({'status': 'success', 'date': date_str, 'cats': cats,
                    'layout_mail': layout_mail, 'layout_all': layout_all})
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@app.route('/api/update_report', methods=['GET'])
def update_report_get():
    date_str = (request.args.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        date_str = _up_today()
    rtype = (request.args.get('type') or 'layout').strip()
    if not (rtype == 'layout' or rtype.startswith('cat:')):
        rtype = 'layout'
    mail_only = (request.args.get('mail_only', '1') != '0')
    is_layout = (rtype == 'layout')
    cat_name = None if is_layout else rtype[4:]

    reports = _up_load_reports()
    saved = reports.get(_up_key(date_str, rtype)) or {}

    # 已移除台號：記住「移除日期」；日後有新紀錄就自動重新顯示（不會永久隱藏）
    excl_at = {}
    for k, rep in reports.items():
        if '|' not in k:
            continue
        d, t = k.split('|', 1)
        if t != rtype or d > date_str:
            continue
        for x in (rep.get('excluded') or []):
            c = _up_norm(x)
            if c:
                excl_at[c] = max(excl_at.get(c, ''), d)

    # 上一份同類報告 → 用來判斷「新增」（紅字）
    prev_dates = []
    for k in reports.keys():
        if '|' in k:
            d, t = k.split('|', 1)
            if t == rtype and d < date_str:
                prev_dates.append(d)
    baseline = set()
    if prev_dates:
        prev = reports.get(_up_key(max(prev_dates), rtype)) or {}
        for col in (prev.get('columns') or []):
            for x in (col.get('codes') or []):
                c = _up_norm(x)
                if c:
                    baseline.add(c)

    det_all, done_map, nfc_nocode = _up_detect(date_str)
    lut = _up_table_lookup()

    def resolve_hp(code):
        """台號對應 Hall／Pit。若只輸入數字（例：0307），嘗試用結尾比對。"""
        hp = lut.get(code)
        if hp:
            return hp.get('hall', ''), hp.get('pit', '')
        if re.match(r'^\d{3,6}$', code or ''):
            hits = [lut[k] for k in lut if k.endswith(code)]
            if len(hits) == 1:
                return hits[0].get('hall', ''), hits[0].get('pit', '')
        return _up_fallback_hp(code)

    def eligible(e):
        if is_layout:
            if not _up_is_layout(e['category'], e['problem']):
                return False
        else:
            if (e['category'] or '') != cat_name:
                return False
        if mail_only and not _up_mailish(e['status']):
            return False
        return True

    det = {}
    for (cat_key, code), e in det_all.items():
        if not eligible(e):
            continue
        ed = excl_at.get(code)
        if ed and e['last_seen'] <= ed:
            continue
        det[code] = e

    # 仍然存在於紀錄內的台號（不分狀態、不分完成），用於清理舊儲存報表
    present_all = set()
    for (cat_key, code), e in det_all.items():
        if is_layout:
            if _up_is_layout(e['category'], e['problem']):
                present_all.add(code)
        elif e['category'] == cat_name:
            present_all.add(code)
    manual = set()
    for x in (saved.get('manual') or []):
        c = _up_norm(x)
        if c:
            manual.add(c)

    # 已標記完成、而且之後沒有再上報的台號（只限當前類別）
    done_only = set()
    for (c, code), _info in done_map.items():
        if is_layout:
            if not _up_is_layout(c, ''):
                continue
        else:
            if c != cat_name:
                continue
        if code not in det:
            done_only.add(code)

    nfc_missing = 0
    if (not is_layout) and cat_name == 'NFC':
        nfc_missing = sum(1 for s in nfc_nocode if (not mail_only or _up_mailish(s)))

    columns = []
    for col in (saved.get('columns') or [])[:_UP_MAX_COLS]:
        codes = []
        for x in (col.get('codes') or []):
            c = _up_norm(x)
            if not c or c in done_only:
                continue
            ed = excl_at.get(c)
            if ed and c not in det:
                continue
            # 台號已不在任何紀錄內（例如後來修正了台號）→ 自動移除；手動加入者保留
            if c not in present_all and c not in manual:
                continue
            codes.append(c)
        columns.append({'main': col.get('main') or '',
                        'sub': (col.get('sub') or '').replace('Dearly', 'Dealer'),
                        'codes': codes})
    if is_layout and not columns:
        columns = [{'main': m, 'sub': s, 'codes': []} for m, s in _UP_LAYOUT_COLS]

    code_to_col = {}
    for idx, col in enumerate(columns):
        for c in col['codes']:
            code_to_col[c] = idx

    cmap = _up_load_map().get(rtype) or {}
    pool = []
    for code, e in det.items():
        if code in code_to_col:
            continue
        target = None
        saved_key = cmap.get(code)
        if saved_key:
            saved_key = saved_key.replace('Dearly', 'Dealer')
            for idx, col in enumerate(columns):
                if (col['main'] + '||' + col['sub']) == saved_key:
                    target = idx
                    break
        if target is None and is_layout:
            target = _up_layout_guess(e['problem'], e.get('side') or '')
        if target is None and not is_layout:
            main = _up_short_cat(e['category'])
            sub = e['problem'] or '（未填問題）'
            for idx, col in enumerate(columns):
                if col['main'] == main and col['sub'] == sub:
                    target = idx
                    break
            if target is None and len(columns) < _UP_MAX_COLS:
                columns.append({'main': main, 'sub': sub, 'codes': []})
                target = len(columns) - 1
        if target is None:
            pool.append(code)
        else:
            columns[target]['codes'].append(code)

    def make_item(code):
        e = det.get(code) or {}
        hall, pit = resolve_hp(code)
        return {
            'table': code,
            'hall': hall,
            'pit': pit,
            'problem': e.get('problem') or '',
            'status': e.get('status') or '',
            'side': e.get('side') or '',
            'note': e.get('note') or '',
            'is_new': code not in baseline,
        }

    def sort_items(items):
        def k(it):
            try: h = int(it['hall'])
            except Exception: h = 9999
            try: p = int(it['pit'])
            except Exception: p = 9999
            return (h, p, it['table'])
        items.sort(key=k)

    out_cols = []
    counts = []
    total = 0
    for col in columns:
        items = [make_item(c) for c in col['codes']]
        sort_items(items)
        out_cols.append({'main': col['main'], 'sub': col['sub'], 'items': items})
        counts.append(len(items))
        total += len(items)
    pool_items = [make_item(c) for c in pool]
    sort_items(pool_items)

    resp = jsonify({
        'status': 'success',
        'date': date_str,
        'today': _up_today(),
        'type': rtype,
        'mail_only': mail_only,
        'sent': bool(saved.get('sent')),
        'has_saved': bool(saved),
        'saved_at': saved.get('updated_at') or '',
        'columns': out_cols,
        'pool': pool_items,
        'counts': counts,
        'total': total,
        'baseline': sorted(baseline),
        'excluded': sorted(excl_at),
        'nfc_missing': nfc_missing,
        'detected_count': len(det),
    })
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@app.route('/api/update_report/save', methods=['POST'])
def update_report_save():
    data = request.json or {}
    date_str = (data.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'status': 'error', 'message': '❌ 日期格式不正確。'})
    rtype = (data.get('type') or 'layout').strip()
    if not (rtype == 'layout' or rtype.startswith('cat:')):
        return jsonify({'status': 'error', 'message': '❌ 報表類型不正確。'})

    cols_in = data.get('columns')
    if not isinstance(cols_in, list):
        cols_in = []
    excl_in = data.get('excluded')
    if not isinstance(excl_in, list):
        excl_in = []

    clean_cols = []
    seen = set()
    for col in cols_in[:_UP_MAX_COLS]:
        if not isinstance(col, dict):
            continue
        main = (col.get('main') or '').strip()[:40]
        sub = (col.get('sub') or '').strip()[:60]
        codes = []
        src = col.get('codes')
        if not isinstance(src, list):
            src = []
        for x in src[:400]:
            c = _up_norm(x)
            if c and _up_is_code(c) and c not in seen:
                seen.add(c)
                codes.append(c)
        clean_cols.append({'main': main, 'sub': sub, 'codes': codes})

    excl = []
    for x in excl_in[:400]:
        c = _up_norm(x)
        if c and c not in seen and c not in excl:
            excl.append(c)

    # 記錄「手動加入」的台號（不在紀錄內），以免下次載入時被自動清除
    manual = []
    try:
        det_save, _dm, _nc = _up_detect(date_str)
        present = set()
        for (cat_key, code), e in det_save.items():
            if rtype == 'layout':
                if _up_is_layout(e['category'], e['problem']):
                    present.add(code)
            elif e['category'] == rtype[4:]:
                present.add(code)
        for col in clean_cols:
            for c in col['codes']:
                if c not in present and c not in manual:
                    manual.append(c)
    except Exception as e:
        print('更新表手動台號檢查失敗:', e)

    reports = _up_load_reports()
    reports[_up_key(date_str, rtype)] = {
        'date': date_str,
        'type': rtype,
        'sent': bool(data.get('sent')),
        'columns': clean_cols,
        'excluded': excl,
        'manual': manual,
        'updated_at': _up_now_text(),
    }
    if not _up_store_reports(reports):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})

    mp = _up_load_map()
    tmap = mp.get(rtype) or {}
    for col in clean_cols:
        ck = col['main'] + '||' + col['sub']
        for c in col['codes']:
            tmap[c] = ck
    for c in excl:
        tmap.pop(c, None)
    mp[rtype] = tmap
    set_setting(_UP_MAPKEY, json.dumps(mp, ensure_ascii=False))

    ex = _up_load_excl()
    elist = set(ex.get(rtype) or [])
    for c in excl:
        elist.add(c)
    for c in seen:
        elist.discard(c)
    ex[rtype] = sorted(elist)
    set_setting(_UP_EXCLKEY, json.dumps(ex, ensure_ascii=False))

    label = 'Layout' if rtype == 'layout' else rtype[4:]
    log_action('更新表', '%s｜%s（%s）共 %d 台' % (date_str, label,
                                                  '已 Send Email' if data.get('sent') else '未 Send Email',
                                                  len(seen)))
    return jsonify({'status': 'success',
                    'message': '✅ 已儲存（%s）' % ('已 Send Email' if data.get('sent') else '未 Send Email')})


# ---------- 成員管理（交更人 / 跟進人） ----------
@app.route('/api/members', methods=['GET'])
def get_members():
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, name, mtype, COALESCE(gender, ''),
                            COALESCE(account, ''), COALESCE(role, 'staff')
                     FROM members ORDER BY mtype DESC, id ASC""")
        rows = c.fetchall()
        return jsonify({'status': 'success', 'members': [
            {'id': r[0], 'name': r[1], 'mtype': r[2], 'gender': r[3],
             'account': r[4], 'role': r[5]} for r in rows]})
    except Exception as e:
        print('讀取成員失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取成員失敗。', 'members': []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/add', methods=['POST'])
def add_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以新增成員。'})
    data = request.json or {}
    name = (data.get('name') or '').strip()[:60]
    mtype = (data.get('mtype') or 'staff').strip()
    gender = (data.get('gender') or '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': '❌ 請輸入姓名。'})
    if mtype not in ('staff', 'follower'):
        mtype = 'staff'
    if gender not in ('男', '女'):
        gender = ''
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1 FROM members WHERE name = %s", (name,))
        if c.fetchone():
            return jsonify({'status': 'error', 'message': '⚠️ 該成員已存在。'})
        mm = re.search(r"[(（](\d+)[)）]", name)
        if mtype == 'staff' and mm:
            acc = mm.group(1)
            c.execute("""INSERT INTO members (name, mtype, gender, account, password, role)
                         VALUES (%s, %s, %s, %s, %s, 'staff')""",
                      (name, mtype, gender, acc, 'BS' + acc))
        elif mtype == 'follower':
            c.execute("""INSERT INTO members (name, mtype, gender, account, password, role)
                         VALUES (%s, %s, %s, %s, %s, 'staff')""",
                      (name, mtype, gender, name, name + '888'))
        else:
            c.execute("INSERT INTO members (name, mtype, gender) VALUES (%s, %s, %s)", (name, mtype, gender))
        conn.commit()
        log_action('新增成員', '%s（%s）' % (name, '交更人' if mtype == 'staff' else '跟進人'))
        return jsonify({'status': 'success', 'message': '✅ 已新增成員。'})
    except Exception as e:
        print('新增成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 新增失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/update', methods=['POST'])
def update_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改成員。'})
    data = request.json or {}
    mid = data.get('id')
    name = (data.get('name') or '').strip()[:60]
    mtype = (data.get('mtype') or 'staff').strip()
    gender = (data.get('gender') or '').strip()
    if not mid or not name:
        return jsonify({'status': 'error', 'message': '❌ 請輸入姓名。'})
    if mtype not in ('staff', 'follower'):
        mtype = 'staff'
    if gender not in ('男', '女'):
        gender = ''
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        old = c.fetchone()
        if not old:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        c.execute("SELECT 1 FROM members WHERE name = %s AND id <> %s", (name, mid))
        if c.fetchone():
            return jsonify({'status': 'error', 'message': '⚠️ 已有同名成員。'})
        c.execute("UPDATE members SET name = %s, mtype = %s, gender = %s WHERE id = %s",
                  (name, mtype, gender, mid))
        conn.commit()
        log_action('修改成員', '%s → %s' % (old[0], name))
        return jsonify({'status': 'success', 'message': '✅ 已更新成員（只影響新交更，舊紀錄不變）。'})
    except Exception as e:
        print('修改成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 修改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/set_role', methods=['POST'])
def set_member_role():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以更改權限。'})
    data = request.json or {}
    mid = data.get('id')
    role = (data.get('role') or '').strip()
    if role not in ('admin', 'staff'):
        return jsonify({'status': 'error', 'message': '❌ 權限類型不正確。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""UPDATE members SET role = %s WHERE id = %s AND COALESCE(account, '') <> ''""",
                  (role, mid))
        if c.rowcount == 0:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '❌ 該成員尚未設定帳號。'})
        conn.commit()
        log_action('更改權限', '成員 ID %s → %s' % (mid, '管理員' if role == 'admin' else '普通'))
        return jsonify({'status': 'success', 'message': '✅ 已更改為「%s」。' % ('管理員' if role == 'admin' else '普通')})
    except Exception as e:
        print('更改權限失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 更改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/reset_password', methods=['POST'])
def reset_member_password():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以重設密碼。'})
    data = request.json or {}
    mid = data.get('id')
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        mm = re.search(r"[(（](\d+)[)）]", row[0] or '')
        if mm:
            acc = mm.group(1)
            pwd = 'BS' + acc
        else:
            # 負責人：帳號＝姓名，密碼＝姓名＋888
            acc = row[0]
            pwd = row[0] + '888'
        c.execute("UPDATE members SET account = %s, password = %s WHERE id = %s", (acc, pwd, mid))
        conn.commit()
        log_action('重設密碼', row[0])
        return jsonify({'status': 'success', 'message': '✅ 密碼已重設為 %s（帳號 %s）。' % (pwd, acc),
                        'account': acc, 'password': pwd})
    except Exception as e:
        print('重設密碼失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 重設失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/delete', methods=['POST'])
def delete_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以刪除成員。'})
    data = request.json or {}
    mid = data.get('id')
    if not mid:
        return jsonify({'status': 'error', 'message': '❌ 找不到成員。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        c.execute("DELETE FROM members WHERE id = %s", (mid,))
        conn.commit()
        log_action('刪除成員', row[0])
        return jsonify({'status': 'success', 'message': '✅ 已刪除成員（舊紀錄不受影響）。'})
    except Exception as e:
        print('刪除成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 刪除失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


# ---------- 台號狀態管理 ----------
@app.route('/api/hall_tables', methods=['GET'])
def list_hall_tables():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以使用。'})
    q = (request.args.get('q') or '').strip().upper()
    hall = (request.args.get('hall') or '').strip()
    pit = (request.args.get('pit') or '').strip()

    where = []
    params = []
    if q:
        where.append("UPPER(table_code) LIKE %s")
        params.append('%' + q + '%')
    if hall:
        where.append("hall = %s")
        params.append(hall)
    if pit:
        where.append("pit = %s")
        params.append(pit)
    sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM hall_tables" + sql_where, params)
        total = c.fetchone()[0]
        c.execute("""SELECT id, hall, pit, table_code, status
                     FROM hall_tables""" + sql_where + " ORDER BY hall, pit, table_code LIMIT 300", params)
        rows = c.fetchall()
        return jsonify({'status': 'success', 'total': total, 'rows': [
            {'id': r[0], 'hall': r[1], 'pit': r[2], 'table_code': r[3], 'status': r[4]} for r in rows]})
    except Exception as e:
        print('讀取台號總表失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取失敗。', 'rows': []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/summary', methods=['GET'])
def hall_tables_summary():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以使用。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT hall, pit, COUNT(*),
                            COUNT(*) FILTER (WHERE status = '營運'),
                            COUNT(*) FILTER (WHERE status = '停運')
                     FROM hall_tables
                     GROUP BY hall, pit
                     ORDER BY hall, pit""")
        rows = c.fetchall()
        c.execute("""SELECT UPPER(REGEXP_REPLACE(table_code, '[0-9]+$', '')) AS prefix,
                            COUNT(*),
                            COUNT(*) FILTER (WHERE status = '營運'),
                            COUNT(*) FILTER (WHERE status = '停運')
                     FROM hall_tables
                     GROUP BY 1
                     ORDER BY 2 DESC""")
        type_rows = c.fetchall()
        types = [{'type': (t or '其他'), 'total': int(n), 'active': int(a or 0), 'inactive': int(d or 0)}
                 for t, n, a, d in type_rows]
        halls = []
        by_hall = {}
        total = {'total': 0, 'active': 0, 'inactive': 0}
        for hall, pit, n, act, dea in rows:
            n, act, dea = int(n), int(act or 0), int(dea or 0)
            if hall not in by_hall:
                by_hall[hall] = {'hall': hall, 'total': 0, 'active': 0, 'inactive': 0, 'pits': []}
                halls.append(by_hall[hall])
            h = by_hall[hall]
            h['total'] += n
            h['active'] += act
            h['inactive'] += dea
            h['pits'].append({'pit': pit, 'total': n, 'active': act, 'inactive': dea})
            total['total'] += n
            total['active'] += act
            total['inactive'] += dea
        return jsonify({'status': 'success', 'halls': halls, 'types': types, 'total': total})
    except Exception as e:
        print('台號統計失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取統計失敗。', 'halls': [], 'total': {}})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/add', methods=['POST'])
def add_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以新增台號。'})
    data = request.json or {}
    hall = (data.get('hall') or '').strip()
    code = (data.get('table_code') or '').strip().upper()
    status = (data.get('status') or '營運').strip()
    try:
        pit = int(data.get('pit'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ Pit 必須是數字。'})
    if not hall or not code:
        return jsonify({'status': 'error', 'message': '❌ Hall 及台號必須填寫。'})
    if status not in ('營運', '停運'):
        status = '營運'

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO hall_tables (hall, pit, table_code, status)
                     VALUES (%s, %s, %s, %s) ON CONFLICT (hall, pit, table_code) DO NOTHING RETURNING id""",
                  (hall, pit, code, status))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '⚠️ 該 Hall／Pit／台號 已存在。'})
        conn.commit()
        log_action('新增台號', '%s / Pit %s / %s' % (hall, pit, code))
        return jsonify({'status': 'success', 'message': '✅ 已新增台號 %s。' % code})
    except Exception as e:
        print('新增台號失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 新增失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/update', methods=['POST'])
def update_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改台號。'})
    data = request.json or {}
    rid = data.get('id')
    hall = (data.get('hall') or '').strip()
    code = (data.get('table_code') or '').strip().upper()
    status = (data.get('status') or '').strip()
    try:
        pit = int(data.get('pit'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ Pit 必須是數字。'})
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到台號。'})
    if not hall or not code:
        return jsonify({'status': 'error', 'message': '❌ Hall 及台號必須填寫。'})
    if status not in ('營運', '停運'):
        status = '營運'

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""UPDATE hall_tables SET hall = %s, pit = %s, table_code = %s, status = %s, updated_at = NOW()
                     WHERE id = %s""", (hall, pit, code, status, rid))
        if c.rowcount == 0:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '❌ 找不到該台號。'})
        conn.commit()
        log_action('修改台號', '%s / Pit %s / %s（%s）' % (hall, pit, code, status))
        return jsonify({'status': 'success', 'message': '✅ 已更新台號。'})
    except Exception as e:
        print('修改台號失敗:', e)
        if conn: conn.rollback()
        if 'duplicate key' in str(e):
            return jsonify({'status': 'error', 'message': '⚠️ 該 Hall／Pit／台號 組合已存在。'})
        return jsonify({'status': 'error', 'message': '❌ 修改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/delete', methods=['POST'])
def delete_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以刪除台號。'})
    data = request.json or {}
    rid = data.get('id')
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到台號。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code FROM hall_tables WHERE id = %s", (rid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該台號。'})
        c.execute("DELETE FROM hall_tables WHERE id = %s", (rid,))
        conn.commit()
        log_action('刪除台號', row[0])
        return jsonify({'status': 'success', 'message': '✅ 已刪除台號 %s。' % row[0]})
    except Exception as e:
        print('刪除台號失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 刪除失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/status', methods=['POST'])
def set_hall_status():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改。'})
    data = request.json or {}
    status = (data.get('status') or '').strip()
    ids = data.get('ids')
    if not isinstance(ids, list):
        ids = [data.get('id')] if data.get('id') else []
    ids = [i for i in ids if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
    if status not in ('營運', '停運'):
        return jsonify({'status': 'error', 'message': '❌ 狀態不正確。'})
    if not ids:
        return jsonify({'status': 'error', 'message': '❌ 沒有選取台號。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE hall_tables SET status = %s, updated_at = NOW() WHERE id = ANY(%s)",
                  (status, [int(i) for i in ids]))
        updated = c.rowcount
        conn.commit()
        log_action('修改台號狀態', '%d 個台號 → %s' % (updated, status))
        return jsonify({'status': 'success', 'message': '✅ 已更新 %d 個台號為「%s」。' % (updated, status),
                        'updated': updated})
    except Exception as e:
        print('修改台號狀態失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 更新失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


_CHAIR_TYPE_KEY = 'chair_types'
_CHAIR_LAYOUT_CACHE = None


def _chair_layout():
    global _CHAIR_LAYOUT_CACHE
    if _CHAIR_LAYOUT_CACHE is None:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'chair_map.json')
            with open(path, 'r', encoding='utf-8') as f:
                _CHAIR_LAYOUT_CACHE = json.load(f)
        except Exception as e:
            print('椅況圖版面讀取失敗:', e)
            _CHAIR_LAYOUT_CACHE = {'maxR': 0, 'maxC': 0, 'pits': [], 'cells': []}
    return _CHAIR_LAYOUT_CACHE


def _chair_types():
    raw = get_setting(_CHAIR_TYPE_KEY, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _chair_issues():
    """由交更紀錄推算每張台的最新椅況（只計未處理項目）。"""
    out = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT note FROM records WHERE note LIKE '%【場面問題】%' ORDER BY id ASC")
        rows = c.fetchall()
    except Exception as e:
        print('椅況圖讀取紀錄失敗:', e)
        rows = []
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    for (note,) in rows:
        secs = _up_split_sections(note or '')
        for line in secs.get('場面問題', '').split('\n'):
            for it in _up_parse_line(line):
                cat = it.get('category') or ''
                is_dlr = ('DLR' in cat and ('櫈' in cat or '椅' in cat))
                is_guest = ('客櫈' in cat or '客椅' in cat)
                if not (is_dlr or is_guest):
                    continue
                key = _up_norm(it['code'])
                rec = out.setdefault(key, {'dlr_bad': False, 'guest_bad': False})
                bad = (it.get('status') != '已處理')
                if is_dlr:
                    rec['dlr_bad'] = bad
                if is_guest:
                    rec['guest_bad'] = bad
    return out


@app.route('/api/chair_map', methods=['GET'])
def chair_map_get():
    """椅況圖：回傳每張台的椅況（停運／好／壞 DLR／壞客椅）及新舊椅設定。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401

    layout = _chair_layout()
    hall_status = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code, status FROM hall_tables")
        for code, st in c.fetchall():
            hall_status[_up_norm(code)] = st
    except Exception as e:
        print('椅況圖讀取台號失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    issues = _chair_issues()
    types = _chair_types()

    cells = []
    for cell in layout.get('cells', []):
        code = cell.get('code') or ''
        key = _up_norm(code)
        st = hall_status.get(key, '營運')
        iss = issues.get(key, {})
        cells.append({
            'r': cell.get('r'), 'c': cell.get('c'), 'pit': cell.get('pit'),
            'num': cell.get('num'), 'code': code,
            'status': st,
            'dlr_bad': bool(iss.get('dlr_bad')),
            'guest_bad': bool(iss.get('guest_bad')),
            'chair': types.get(key) or ''
        })
    return jsonify({'status': 'success', 'maxR': layout.get('maxR'), 'maxC': layout.get('maxC'),
                    'pits': layout.get('pits', []), 'cells': cells})


@app.route('/api/chair_map/type', methods=['POST'])
def chair_map_set_type():
    """設定新舊 DLR 椅（存 app_settings）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    code = _up_norm(data.get('code'))
    typ = (data.get('type') or '').strip().lower()
    if typ not in ('new', 'old', ''):
        return jsonify({'status': 'error', 'message': '類型錯誤'})
    if not code:
        return jsonify({'status': 'error', 'message': '缺少台號'})
    types = _chair_types()
    if typ:
        types[code] = typ
    else:
        types.pop(code, None)
    return jsonify({'status': 'success'} if set_setting(_CHAIR_TYPE_KEY, json.dumps(types, ensure_ascii=False)) else
                   {'status': 'error', 'message': '儲存失敗'})


@app.route('/')
def index():
    # 將是否為管理員的狀態傳給前端
    is_admin = (session.get('role') == 'admin')
    return render_template('index.html', is_admin=is_admin, user_name=session.get('user_name', ''))

if __name__ == '__main__':
    app.run()
