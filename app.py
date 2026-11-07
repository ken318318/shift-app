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

# 初始化數據庫（加入新欄位 lux, temp, status）
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records
                 (id SERIAL PRIMARY KEY,
                  name TEXT,
                  shift TEXT,
                  lux TEXT,
                  temp TEXT,
                  checklist TEXT,
                  status TEXT,
                  note TEXT)''')
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print("數據庫連線失敗:", e)

# 1. 接收交更資料嘅 API
@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO records (name, shift, lux, temp, checklist, status, note) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
              (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('checklist'), data.get('status'), data.get('note')))
    conn.commit()
    conn.close()
    return jsonify({"message": "交更成功！", "status": "success"})

# 2. 讀取所有交更紀錄嘅 API
@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, shift, lux, temp, checklist, status, note FROM records ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    records = []
    for row in rows:
        records.append({
            "name": row[0], "shift": row[1], "lux": row[2], 
            "temp": row[3], "checklist": row[4], "status": row[5], "note": row[6]
        })
    return jsonify(records)

# 派發首頁
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
