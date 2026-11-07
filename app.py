from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import psycopg2
import os

app = Flask(__name__)
CORS(app)

# ⚠️ 極度重要：請將下面引號入面嘅字，換成你喺 Supabase Copy 落嚟嗰條完整網址！
# 記得要將 [YOUR-PASSWORD] 換成你嘅密碼
DB_URL = "postgresql://postgres:[ye6fM4*ETc5nGiR]@db.mwnwvpvmtjgqwchtpenv.supabase.co:5432/postgres"

def get_db_connection():
    conn = psycopg2.connect(DB_URL)
    return conn

# 初始化數據庫（建立資料表）
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records
                 (id SERIAL PRIMARY KEY,
                  name TEXT,
                  shift TEXT,
                  note TEXT)''')
    conn.commit()
    conn.close()

# 每次啟動程式前先檢查數據庫
try:
    init_db()
except Exception as e:
    print("數據庫連線失敗:", e)

# 1. 接收交更資料嘅 API
@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    name = data.get('name')
    shift = data.get('shift')
    note = data.get('note')

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO records (name, shift, note) VALUES (%s, %s, %s)", (name, shift, note))
    conn.commit()
    conn.close()

    return jsonify({"message": "交更成功！", "status": "success"})

# 2. 讀取所有交更紀錄嘅 API
@app.route('/api/records', methods=['GET'])
def get_records():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name, shift, note FROM records ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    records = []
    for row in rows:
        records.append({
            "name": row[0],
            "shift": row[1],
            "note": row[2]
        })

    return jsonify(records)

# 派發首頁 index.html
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run()
