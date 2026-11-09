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

@app.route('/api/pending_issues', methods=['GET'])
def get_pending_issues():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, name, lux_issue, temp_issue, db_issue, note, status, 
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau' 
                 FROM records 
                 WHERE status NOT IN ('已處理', '已 Send mail') 
                 AND (lux_issue != '' OR temp_issue != '' OR db_issue != '' OR note LIKE '%【場面問題】%')
                 ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()
    
    # 1. 先收集所有「已處理」嘅字眼，放入黑名單
    done_items_set = set()
    for row in rows:
        note = row[5] or ""
        if '【早前已處理】' in note:
            done_part = note.split('【早前已處理】')[1].split('【早前未處理】')[0]
            for line in done_part.split('\n'):
                if line.strip():
                    done_items_set.add(line.strip())

    # 2. 真正抽未處理嘅時候，過濾走黑名單入面嘅嘢
    pending = []
    for row in rows:
        pending.append({
            "id": row[0], "name": row[1], "lux_issue": row[2], "temp_issue": row[3], 
            "db_issue": row[4], "note": row[5], "overall_status": row[6],
            "created_at": str(row[7]) if row[7] else None,
            "done_items_set": list(done_items_set)
        })
    return jsonify(pending)

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
