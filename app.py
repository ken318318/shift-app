from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import sqlite3
import os

app = Flask(__name__)
CORS(app)

# 初始化數據庫（如果檔案唔存在就會自動建立）
def init_db():
    conn = sqlite3.connect('handover.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS records
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT,
                  shift TEXT,
                  note TEXT)''')
    conn.commit()
    conn.close()

# 每次啟動程式前先檢查數據庫
init_db()

# 1. 接收交更資料嘅 API
@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    name = data.get('name')
    shift = data.get('shift')
    note = data.get('note')

    # 將資料寫入 SQLite 數據庫
    conn = sqlite3.connect('handover.db')
    c = conn.cursor()
    c.execute("INSERT INTO records (name, shift, note) VALUES (?, ?, ?)", (name, shift, note))
    conn.commit()
    conn.close()

    return jsonify({"message": "交更成功！", "status": "success"})

# 2. 讀取所有交更紀錄嘅 API
@app.route('/api/records', methods=['GET'])
def get_records():
    conn = sqlite3.connect('handover.db')
    c = conn.cursor()
    # 由最新到最舊讀取資料
    c.execute("SELECT name, shift, note FROM records ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    # 將資料轉換成前端識嘅格式
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
