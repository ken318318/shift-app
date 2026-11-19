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


# ---------- 系統設定 / 稽核（自動建表，唔需要手動改資料庫） ----------
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
        c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
                        id BIGSERIAL PRIMARY KEY,
                        actor TEXT,
                        action TEXT,
                        detail TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
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
EPOCH_TTL = 10  # 秒；即係改密碼後最多 10 秒內所有舊 session 失效


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
    """第一次啟動時，按舊有顯示次序寫入 cat_order / item_order（之後唔會再動）。"""
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
    shift-app-wvgf.onrender.com 之類嘅舊網址 → 301 跳去新域名。
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
                            "message": "⏰ 你嘅登入已失效（密碼已被更改），請重新登入。"}), 401
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
            session['epoch'] = get_epoch('admin', force=True)
            log_action('登入', '管理員登入')
            return redirect(url_for('index'))
        elif pwd and pwd == staff_pwd:
            session['logged_in'] = True
            session['role'] = 'staff'
            session['epoch'] = get_epoch('staff', force=True)
            log_action('登入', '同事登入')
            return redirect(url_for('index'))
        else:
            error = "密碼錯誤！"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        return "⚠️ 權限不足！", 403
    return render_template('admin.html')

@app.route('/api/diag', methods=['GET'])
def diag():
    """診斷（純文字，方便手機睇）。"""
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
                return jsonify({"status": "success", "message": "已經喺最" + ('前' if direction == 'up' else '後') + "面。"})
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
                return jsonify({"status": "success", "message": "已經喺最" + ('前' if direction == 'up' else '後') + "面。"})
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
            return jsonify({"status": "error", "message": "⚠️ 呢個選項已經存在。"})

        c.execute("""SELECT COALESCE(MAX(cat_order),0), COALESCE(MAX(item_order),0)
                     FROM venue_configs WHERE category = %s""", (category,))
        row = c.fetchone()
        cat_order = row[0] or 0
        item_order = (row[1] or 0) + 1
        if not cat_order:
            c.execute("SELECT COALESCE(MAX(cat_order),0) FROM venue_configs")
            cat_order = (c.fetchone()[0] or 0) + 10
        c.execute("INSERT INTO venue_configs (category, problem, cat_order, item_order) VALUES (%s, %s, %s, %s)",
                  (category, problem, cat_order, item_order))
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
    刪除場面問題選項（任何已登入嘅同事都可以用）。
    problem 有值 → 只刪該類別下嘅該個選項；
    problem 留空 → 刪除整個類別（連同該類別底下所有選項）。
    已提交嘅舊紀錄唔受影響（紀錄係存文字，唔係存 id）。
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
            return jsonify({"status": "error", "message": "⚠️ 搵唔到要刪除嘅選項（可能已經被其他人刪除）。"})

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
            return jsonify({"status": "error", "message": "⚠️ 搵唔到要修改嘅資料。"})

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
                     (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'), now_macau))
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

        c.execute("SELECT created_at FROM records WHERE id = %s", (old_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "❌ 找不到該紀錄！"})

        if session.get('role') != 'admin':
            c.execute("SELECT NOW() - created_at < interval '5 minutes' FROM records WHERE id = %s", (old_id,))
            is_within_5_mins = c.fetchone()[0]
            if not is_within_5_mins:
                return jsonify({"status": "error", "message": "⏰ 已超過 5 分鐘修改時限，無法修改！"})

        c.execute("DELETE FROM records WHERE id = %s", (old_id,))

        now_macau = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        c.execute("""INSERT INTO records 
                     (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'), now_macau))
        
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
                            lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback,
                            created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau'
                     FROM records""" + sql_where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                  params + [limit, offset])
        rows = c.fetchall()
        records = [{
            "id": r[0], "name": r[1], "shift": r[2], "lux": r[3], "temp": r[4], "bgm": r[5],
            "checklist": r[6], "status": r[7], "follower": r[8], "note": r[9],
            "lux_issue": r[10], "temp_issue": r[11], "db_issue": r[12],
            "guest_feedback": r[13], "dlr_feedback": r[14],
            "created_at": str(r[15]) if r[15] else None
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
                 guest_feedback, dlr_feedback,
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records ORDER BY id DESC""")
    rows = c.fetchall()
    release_db_connection(conn)
    records = []
    for row in rows:
        records.append({
            "id": row[0], "name": row[1], "shift": row[2], "lux": row[3], "temp": row[4], "bgm": row[5],
            "checklist": row[6], "status": row[7], "follower": row[8], "note": row[9],
            "lux_issue": row[10], "temp_issue": row[11], "db_issue": row[12],
            "guest_feedback": row[13], "dlr_feedback": row[14],
            "created_at": str(row[15]) if row[15] else None
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

    data = m_build_rows = [(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in rows]
    payload = build_update_report(data)
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d')
    filename = 'BS_update_' + stamp + '.xls'
    return Response(payload, mimetype='application/vnd.ms-excel', headers={
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

    out = [['日期時間', '交更人', '更份', 'Lux', '溫度', '分貝', '狀態', '檢測Hall',
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
            str(r[15]) if r[15] else '', r[1], r[2], r[3], r[4], r[5], r[7], hall,
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
        # 保持目前呢個管理員 session 有效，唔會自己踢自己
        session['epoch'] = get_epoch('admin', force=True)
        msg = "✅ 管理員密碼已更新（其他已登入嘅管理員會被強制登出）。"
    else:
        msg = "✅ 同事密碼已更新，所有已登入嘅同事已被強制登出（最多 10 秒內生效）。"

    log_action('更改密碼', '帳號：' + ('同事' if target == 'staff' else '管理員'))
    return jsonify({"status": "success", "message": msg})


@app.route('/api/kick_sessions', methods=['POST'])
def kick_sessions():
    """唔改密碼，直接踢走所有已登入嘅同事（適合有人離職）。"""
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


@app.route('/')
def index():
    # 將是否為管理員的狀態傳給前端
    is_admin = (session.get('role') == 'admin')
    return render_template('index.html', is_admin=is_admin)

if __name__ == '__main__':
    app.run()
