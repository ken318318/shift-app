from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import psycopg2
from psycopg2 import pool
import os
import re
from datetime import datetime
import pytz

app = Flask(__name__)
app.secret_key = "Ken_Shift_App_Secret_123" 
CORS(app)

# 🌟 你設定嘅密碼
STAFF_PASSWORD = "BS2026"       
ADMIN_PASSWORD = "ADMIN888"     

DB_URL = os.environ.get("DB_URL")

# 🌟 建立連線池
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

# 🌟 登入攔截器 (白名單已加入 verify_wechat)
@app.before_request
def require_login():
    allowed_routes = ['login', 'static', 'verify_wechat']
    if 'logged_in' not in session and request.endpoint not in allowed_routes:
        return redirect(url_for('login'))

# 🌟 微信部署驗證路由 (放喺登入路由上面)
@app.route('/bf392e0c1abdaeb5d994c628262a24a2.txt')
def verify_wechat():
    return "3d858c8569ab9afadbbc62d5f31c30007c34f14b", 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == STAFF_PASSWORD:
            session['logged_in'] = True
            session['role'] = 'staff'
            return redirect(url_for('index'))
        elif pwd == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['role'] = 'admin'
            return redirect(url_for('index'))
        else:
            error = "密碼錯誤，請再試一次！"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        return "⚠️ 權限不足！請用管理員密碼登入。", 403
    return render_template('admin.html')

# ---------------- API 區塊 ----------------

@app.route('/api/get_configs', methods=['GET'])
def get_configs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT category, problem FROM venue_configs ORDER BY id ASC")
    rows = c.fetchall()
    release_db_connection(conn)
    
    configs = {}
    for row in rows:
        category = row[0]
        problem = row[1]
        if category not in configs:
            configs[category] = []
        configs[category].append(problem)
        
    return jsonify(configs)

@app.route('/api/add_config', methods=['POST'])
def add_config():
    data = request.json
    category = data.get('category')
    problem = data.get('problem')
    
    if not category or not problem:
        return jsonify({"status": "error", "message": "請提供分類同問題名稱！"}), 400
        
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO venue_configs (category, problem) VALUES (%s, %s)", (category, problem))
    conn.commit()
    release_db_connection(conn)
    
    return jsonify({"status": "success", "message": "新增成功！"})

@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    
    macau_tz = pytz.timezone('Asia/Macau')
    now_macau = datetime.now(macau_tz)
    
    c.execute("""INSERT INTO records 
                 (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, created_at) 
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
              (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
               data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
               data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'), now_macau))
    conn.commit()
    release_db_connection(conn)
    return jsonify({"message": "交更成功！", "status": "success"})

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
    
    # 1. 搵出所有「已處理」嘅字眼，放入黑名單
    c.execute("SELECT note FROM records WHERE note LIKE '%【早前已處理】%'")
    done_rows = c.fetchall()
    done_items = set()
    for r in done_rows:
        if r[0] and '【早前已處理】' in r[0]:
            part = r[0].split('【早前已處理】')[1]
            if '【早前未處理】' in part:
                part = part.split('【早前未處理】')[0]
            for line in part.split('\n'):
                if line.strip(): done_items.add(line.strip())

    # 2. 搵出所有未完成嘅紀錄
    c.execute("""SELECT id, name, lux_issue, temp_issue, db_issue, note, status, 
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records 
                 WHERE status NOT IN ('已處理') 
                 AND (lux_issue != '' OR temp_issue != '' OR db_issue != '' 
                      OR note LIKE '%【場面問題】%' OR note LIKE '%【早前未處理】%')
                 ORDER BY id DESC""")
    rows = c.fetchall()
    release_db_connection(conn)
    
    pending = []
    for row in rows:
        pending.append({
            "id": row[0], "name": row[1], "lux_issue": row[2], "temp_issue": row[3], 
            "db_issue": row[4], "note": row[5], "overall_status": row[6],
            "created_at": str(row[7]) if row[7] else None
        })
        
    return jsonify({"issues": pending, "done_items": list(done_items)})

@app.route('/api/delete_record', methods=['POST'])
def delete_record():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 權限不足！只有管理員可以刪除紀錄。"})
    
    data = request.json
    record_id = data.get('id')
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM records WHERE id = %s", (record_id,))
    conn.commit()
    release_db_connection(conn)
    
    return jsonify({"status": "success", "message": "✅ 紀錄已成功刪除！"})

@app.route('/api/update_record', methods=['POST'])
def update_record():
    data = request.json
    record_id = data.get('id')
    new_note = data.get('note')
    new_status = data.get('status')
    
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT created_at FROM records WHERE id = %s", (record_id,))
    row = c.fetchone()
    if not row:
        release_db_connection(conn)
        return jsonify({"status": "error", "message": "❌ 找不到該紀錄！"})
        
    created_at = row[0]
    
    if session.get('role') != 'admin':
        import datetime
        now = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        time_diff = now - created_at
        if time_diff.total_seconds() > 300: 
            release_db_connection(conn)
            return jsonify({"status": "error", "message": "⏰ 已超過 5 分鐘修改時限，無法修改！"})

    c.execute("UPDATE records SET note = %s, status = %s WHERE id = %s", (new_note, new_status, record_id))
    conn.commit()
    release_db_connection(conn)
    
    return jsonify({"status": "success", "message": "✅ 紀錄已成功更新！"})

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
