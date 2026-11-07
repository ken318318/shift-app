from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import psycopg2
import os

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
    # 確保所有欄位都存在，同資料庫 100% 匹配
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
                  created_at TIMESTAMP DEFAULT NOW())''')
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
    # 寫入資料（對應 HTML 傳過嚟嘅 key）
    c.execute("""INSERT INTO records 
                 (name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue) 
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
              (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
               data.get('checklist'), data.get('status'), data.get('follower'), data.get('note'),
               data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue')))
    conn.commit()
    conn.close()
    return jsonify({"message": "交更成功！", "status": "success"})

@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    # 讀取資料（一定要同上面 insert 嘅欄位順序一致）
    c.execute("""SELECT name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue, created_at 
                 FROM records ORDER BY id DESC""")
    rows = c.fetchall()
    conn.close()

    records = []
    for row in rows:
        records.append({
            "name": row[0], 
            "shift": row[1], 
            "lux": row[2], 
            "temp": row[3], 
            "bgm": row[4],
            "checklist": row[5], 
            "status": row[6], 
            "follower": row[7], 
            "note": row[8],
            "lux_issue": row[9], 
            "temp_issue": row[10], 
            "db_issue": row[11],
            "created_at": str(row[12]) if row[12] else None
        })
    return jsonify(records)

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
