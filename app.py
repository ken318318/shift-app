from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import psycopg2
import os
from datetime import datetime
import pytz

app = Flask(__name__)
CORS(app)

# ⚠️ 呢度保留你原本嗰條 Supabase 網址
DB_URL = "postgresql://postgres.mwnwvpvmtjgqwchtpenv:KenShiftHandover2026@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

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

import re # ⚠️ 記得喺 app.py 最頂部（import pytz 附近）加返呢句

@app.route('/api/pending_issues', methods=['GET'])
def get_pending_issues():
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. 先搵出所有「已處理」嘅字眼，放入黑名單 (保持不變)
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

    # 2. 搵出所有未完成嘅紀錄 (保持不變)
    c.execute("""SELECT id, name, lux_issue, temp_issue, db_issue, note, status, 
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records 
                 WHERE status NOT IN ('已處理', '已 Send mail') 
                 AND (lux_issue != '' OR temp_issue != '' OR db_issue != '' 
                      OR note LIKE '%【場面問題】%' OR note LIKE '%【早前未處理】%')
                 ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    
    pending = []
    # 🌟 新增：用嚟記錄已經見過嘅「台號+問題類別」，防止舊紀錄重複出現
    seen_issues = set() 

    def is_duplicate(prefix, text):
    """解析字串，抽出台號同類別，判斷係咪重複"""
    if not text: return False
    
    tables = re.findall(r'([A-Za-z]{2}\d+)', text)
    if not tables:
        return False
        
    table_key = tables[0].upper()
    
    if prefix in ['Lux', '溫度', '分貝']:
        category_key = prefix
    else:
        # 🌟 呢度改咗：無視前面有冇「場面 -」，總之見到中括號就抽入面嘅嘢
        match = re.search(r'\[(.*?)\]', text)
        if match:
            category_key = match.group(1)
        else:
            category_key = text[:20]
        
    unique_key = f"{prefix}_{table_key}_{category_key}"
    
    if unique_key in seen_issues:
        return True
    else:
        seen_issues.add(unique_key)
        return False

    for row in rows:
        # 我哋要將字串拆開，逐條問題去 check 係咪重複
        # 因為一條紀錄入面可能包含多個問題 (例如 lux_issue 有兩行)
        
        new_lux = []
        if row[2]: # lux_issue
            for item in row[2].split(' | '):
                if item.strip() and not is_duplicate('Lux', item):
                    new_lux.append(item)
                    
        new_temp = []
        if row[3]: # temp_issue
            for item in row[3].split(' | '):
                if item.strip() and not is_duplicate('溫度', item):
                    new_temp.append(item)
                    
        new_db = []
        if row[4]: # db_issue
            for item in row[4].split(' | '):
                if item.strip() and not is_duplicate('分貝', item):
                    new_db.append(item)
        
        new_note = row[5] or ""
        note_lines = new_note.split('\n')
        filtered_note_lines = []
        
        for line in note_lines:
            # 保留標題 (例如 【場面問題】, 【早前未處理】)
            if line.startswith('【') or not line.strip():
                filtered_note_lines.append(line)
            else:
                # 場面問題同早前未處理嘅行數
                prefix = '場面' if '位置:' in line else '早前'
                if not is_duplicate(prefix, line):
                    filtered_note_lines.append(line)
                    
        new_note = '\n'.join(filtered_note_lines)

        # 如果過濾完之後，呢條紀錄仲有嘢剩，先至加入 pending 列表
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

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
