from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import psycopg2
from psycopg2 import pool
import os
import re
import datetime
import time
import pytz

app = Flask(__name__)
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


ensure_tables()

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

@app.route('/api/get_configs', methods=['GET'])
def get_configs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT category, problem FROM venue_configs 
                 ORDER BY 
                 CASE 
                    WHEN category = '賭檯 - Display(大Mon)' THEN 1
                    WHEN category = '賭檯 - Display(細Mon)' THEN 2
                    WHEN category = '賭檯 - Layout' THEN 3
                    WHEN category = '賭檯 - Sound Bar' THEN 4
                    ELSE 5 
                 END, category ASC, id ASC""")
    rows = c.fetchall()
    release_db_connection(conn)
    configs = {}
    for row in rows:
        if row[0] not in configs: 
            configs[row[0]] = []
        configs[row[0]].append(row[1])
    return jsonify(configs)

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

        c.execute("INSERT INTO venue_configs (category, problem) VALUES (%s, %s)", (category, problem))
        conn.commit()
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
                     (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'), now_macau))
        conn.commit()
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
                     (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, created_at) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'), now_macau))
        
        conn.commit()
        log_action('修改紀錄', f"舊紀錄 ID {old_id}")
        return jsonify({"status": "success", "message": "✅ 紀錄已成功修改並覆蓋！"})
    except Exception as e:
        print("Overwrite Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": f"❌ 修改失敗：{str(e)}"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, 
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
            "created_at": str(row[13]) if row[13] else None
        })
    return jsonify(records)

@app.route('/api/pending_issues', methods=['GET'])
def get_pending_issues():
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
    return jsonify({"issues": pending, "done_items": list(done_items)})

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
