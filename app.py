from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app) # 允許我哋個網頁呼叫呢個 API

# 用一個 List 暫存資料（依家放喺記憶體，重開就無）
handover_records = []

# 1. 接收交更資料嘅 API
@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    new_record = {
        "name": data.get('name'),
        "shift": data.get('shift'),
        "note": data.get('note')
    }
    handover_records.insert(0, new_record) # 將新紀錄放喺最前
    return jsonify({"message": "交更成功！", "status": "success"})

# 2. 讀取所有交更紀錄嘅 API
@app.route('/api/records', methods=['GET'])
def get_records():
    return jsonify(handover_records)

if __name__ == '__main__':
    app.run()
