from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_cors import CORS
import psycopg2
import os
import re
from datetime import datetime
import pytz

app = Flask(__name__)
app.secret_key = "Ken_Shift_App_Secret_123"  # 🌟 用嚟加密登入狀態
CORS(app)

# 🌟 你設定嘅密碼
STAFF_PASSWORD = "BS2026"       # 普通同事交更嘅密碼
ADMIN_PASSWORD = "ADMIN888"     # 管理員密碼

@app.before_request
def require_login():
    # 允許訪問登入頁同靜態檔案
    allowed_routes = ['login', 'static']
    if 'logged_in' not in session and request.endpoint not in allowed_routes:
        return redirect(url_for('login'))

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

# ⚠️ 呢度保留你原本嗰條 Supabase 網址
DB_URL = os.environ.get("DB_URL")

def get_db_connection():
    conn = psycopg2.connect(DB_URL)
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records
                 (id SERIAL PRIMARY KEY,
                  name TEXT,
                  shift TEXT,
                  lux TEXT,
                  temp TEXT,
                  bgm TEXT,
                  checklist TEXT,
                  status TEXT,
                  follower TEXT,
                  note TEXT,
                  lux_issue TEXT,
                  temp_issue TEXT,
                  db_issue TEXT,
                  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print("數據庫連線失敗:", e)
@app.route('/api/get_configs', methods=['GET'])
def get_configs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT category, problem FROM venue_configs ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    
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
    conn.close()
    
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
    conn.close()
    return jsonify({"message": "交更成功！", "status": "success"})

@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, 
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()

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
    
    # 1. 先搵出所有「已處理」嘅字眼，放入黑名單
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
    conn.close()
    
    pending = []
    seen_issues = set() 

    # 🌟 判斷係咪重複嘅核心邏輯
    def is_duplicate(prefix, text):
        if not text: 
            return False
        
        # 抽出所有「英文字母+數字」嘅台號 (例如 NB1101)
        tables = re.findall(r'([A-Za-z]{2}\d+)', text)
        if not tables:
            return False
            
        table_key = tables[0].upper()
        
        # 判斷問題類別
        if prefix in ['Lux', '溫度', '分貝']:
            category_key = prefix
        else:
            # 無視前面有冇「場面 -」，只要見到中括號 [...] 就抽出嚟做類別
            match = re.search(r'\[(.*?)\]', text)
            if match:
                category_key = match.group(1)
            else:
                category_key = text[:20]
            
        unique_key = f"{prefix}_{table_key}_{category_key}"
        
        if unique_key in seen_issues:
            return True # 重複咗
        else:
            seen_issues.add(unique_key) # 第一次見，記錄低
            return False

    # 開始過濾每一條紀錄
    for row in rows:
        new_lux = []
        if row[2]: 
            for item in row[2].split(' | '):
                if item.strip() and not is_duplicate('Lux', item):
                    new_lux.append(item)
                    
        new_temp = []
        if row[3]: 
            for item in row[3].split(' | '):
                if item.strip() and not is_duplicate('溫度', item):
                    new_temp.append(item)
                    
        new_db = []
        if row[4]: 
            for item in row[4].split(' | '):
                if item.strip() and not is_duplicate('分貝', item):
                    new_db.append(item)
        
        new_note = row[5] or ""
        note_lines = new_note.split('\n')
        filtered_note_lines = []
        
        for line in note_lines:
            if line.startswith('【') or not line.strip():
                filtered_note_lines.append(line)
            else:
                prefix = '場面' if '位置:' in line else '早前'
                if not is_duplicate(prefix, line):
                    filtered_note_lines.append(line)
                    
        new_note = '\n'.join(filtered_note_lines)

        # 如果過濾完之後仲有嘢剩，先加入列表
        if new_lux or new_temp or new_db or '位置:' in new_note:
            pending.append({
                "id": row[0], "name": row[1], 
                "lux_issue": ' | '.join(new_lux), 
                "temp_issue": ' | '.join(new_temp), 
                "db_issue": ' | '.join(new_db), 
                "note": new_note, 
                "overall_status": row[6],
                "created_at": str(row[7]) if row[7] else None
            })
        
    return jsonify({"issues": pending, "done_items": list(done_items)})

@app.route('/api/update_status', methods=['POST'])
def update_status():
    data = request.json
    record_id = data.get('id')
    new_status = data.get('status')
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE records SET status = %s WHERE id = %s", (new_status, record_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "狀態已更新", "status": "success"})

@app.route('/admin')
def admin():
    # 只有管理員 (用 ADMIN888 登入) 先可以入到呢度
    if session.get('role') != 'admin':
        return "⚠️ 權限不足！請用管理員密碼登入。", 403
    return render_template('admin.html')

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
