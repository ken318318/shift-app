from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from flask_cors import CORS
import psycopg2
import psycopg2.extras
from psycopg2 import pool
import os
import io
import re
import datetime
import json
import time
import pytz

app = Flask(__name__)
app.json.sort_keys = False          # 保留字典原有次序（否則 jsonify 會按字母重排）
app.secret_key = "Ken_Shift_App_Secret_123" 
CORS(app)

# ---------- 安全設定 ----------
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('ALLOW_INSECURE_COOKIE') != '1':
    app.config['SESSION_COOKIE_SECURE'] = True

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


# ---------- 系統設定 / 稽核（自動建表，無須手動修改資料庫） ----------
_SET_CACHE = {}      # app_settings 讀取快取：key -> (時間, 值)
_SET_TTL = 8         # 秒；寫入時會即時清掉該 key


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
        c.execute("ALTER TABLE venue_configs ADD COLUMN IF NOT EXISTS cat_order INTEGER DEFAULT 0")
        c.execute("ALTER TABLE venue_configs ADD COLUMN IF NOT EXISTS item_order INTEGER DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS guest_feedback TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS dlr_feedback TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS layout_side TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS on_duty TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed BOOLEAN DEFAULT FALSE")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS reviewed_by TEXT")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS items JSONB DEFAULT '[]'::jsonb")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS soundbar TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS firecheck TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS firecheck15 TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS firecheck30 TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS wifi1 TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS wifi15 TEXT DEFAULT ''")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS shift_day SMALLINT")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
                        id BIGSERIAL PRIMARY KEY,
                        actor TEXT,
                        action TEXT,
                        detail TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("""CREATE TABLE IF NOT EXISTS members (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        mtype TEXT NOT NULL DEFAULT 'staff',
                        gender TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("SELECT COUNT(*) FROM members")
        if (c.fetchone()[0] or 0) == 0:
            seed = [
                ('龍ken (8000285)', 'staff', '男'),
                ('陳ken (80571)', 'staff', '男'),
                ('Fish (8047495)', 'staff', '女'),
                ('阿能 (8055532)', 'staff', '男'),
                ('阿威 (8055475)', 'staff', '男'),
                ('阿倫 (8017308)', 'staff', '男'),
                ('世外高喵 (298)', 'staff', '女'),
                ('Paco (8047393)', 'staff', '男'),
                ('小信 (8056027)', 'staff', '女'),
                ('kiko (1227)', 'staff', '女'),
                ('Wawa (1145)', 'staff', '男'),
                ('Ivy (8001161)', 'staff', '女'),
                ('秋龍 (67162)', 'staff', '女'),
                ('炳 (6234)', 'staff', '男'),
                ('Carman', 'follower', ''),
                ('David', 'follower', ''),
                ('Nam', 'follower', ''),
                ('Stella', 'follower', ''),
            ]
            c.executemany("""INSERT INTO members (name, mtype, gender) VALUES (%s, %s, %s)
                            ON CONFLICT (name) DO NOTHING""", seed)

        # 每人帳號（帳號＝員工ID，密碼＝BS＋員工ID）
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS account TEXT")
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS password TEXT")
        c.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'staff'")
        c.execute("SELECT id, name, COALESCE(mtype, 'staff') FROM members WHERE COALESCE(account, '') = ''")
        for mid, nm, mt in c.fetchall():
            mm = re.search(r"[(（](\d+)[)）]", nm or '')
            if mm:
                acc = mm.group(1)
                c.execute("""UPDATE members SET account = %s, password = %s,
                                          role = COALESCE(NULLIF(role, ''), 'staff')
                             WHERE id = %s""", (acc, 'BS' + acc, mid))
            elif mt == 'follower':
                c.execute("""UPDATE members SET account = %s, password = %s,
                                          role = COALESCE(NULLIF(role, ''), 'staff')
                             WHERE id = %s""", (nm, nm + '888', mid))
        # ---------- 存貨記錄（82倉庫 / CP） ----------
        c.execute("""CREATE TABLE IF NOT EXISTS inventory_items (
                        id BIGSERIAL PRIMARY KEY,
                        zone TEXT NOT NULL DEFAULT 'WH',
                        loc TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL,
                        qty INTEGER,
                        unit TEXT DEFAULT '',
                        detail TEXT DEFAULT '',
                        note TEXT DEFAULT '',
                        sort INTEGER DEFAULT 0,
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_by TEXT DEFAULT '',
                        deleted BOOLEAN DEFAULT FALSE
                     )""")
        c.execute("""CREATE TABLE IF NOT EXISTS inventory_logs (
                        id BIGSERIAL PRIMARY KEY,
                        item_id BIGINT,
                        action TEXT,
                        detail TEXT,
                        actor TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                     )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_zone_loc ON inventory_items (zone, loc)")
        c.execute("SELECT COUNT(*) FROM inventory_items")
        if (c.fetchone()[0] or 0) == 0:
            _seed_path = os.path.join(app.root_path, 'static', 'inventory_seed.json')
            if os.path.exists(_seed_path):
                try:
                    with open(_seed_path, 'r', encoding='utf-8') as _f:
                        _seed_obj = json.load(_f)
                    _rows = []
                    for _it in _seed_obj.get('items', []):
                        _rows.append((_it.get('zone', 'WH'), _it.get('loc', ''), _it.get('name', ''),
                                      _it.get('qty'), _it.get('unit', ''), _it.get('detail', ''),
                                      _it.get('note', ''), _it.get('sort', 0), 'Excel 匯入'))
                    if _rows:
                        c.executemany("""INSERT INTO inventory_items
                                        (zone, loc, name, qty, unit, detail, note, sort, updated_by)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""", _rows)
                        print("存貨初始資料已匯入：%d 項" % len(_rows))
                except Exception as _e:
                    print("存貨初始資料匯入失敗:", _e)

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
    _now = time.time()
    _hit = _SET_CACHE.get(key)
    if _hit is not None and (_now - _hit[0]) < _SET_TTL:
        return default if _hit[1] is None else _hit[1]
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = c.fetchone()
        _val = row[0] if row else None
        _SET_CACHE[key] = (_now, _val)
        return default if _val is None else _val
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
        _SET_CACHE.pop(key, None)
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
EPOCH_TTL = 10  # 秒；即修改密碼後最多 10 秒內所有舊 session 失效


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


# ---------- 簡單記憶體快取（單一 worker 有效，減少重複查詢） ----------
_RESULT_CACHE = {}
_DBG = {}


def cache_get(key):
    item = _RESULT_CACHE.get(key)
    if not item:
        return None
    expiry, val = item
    if time.time() > expiry:
        _RESULT_CACHE.pop(key, None)
        return None
    return val


def cache_set(key, val, ttl=60):
    _RESULT_CACHE[key] = (time.time() + ttl, val)


def bust():
    """任何寫入（交更／完成／刪除／櫈況）後，清走相關快取。"""
    cache_clear('pending', 'pending_items', 'table_codes', 'monthly_checks', 'chair_map', 'layout_schedule', 'inventory')
    for _k in list(_RESULT_CACHE.keys()):
        if _k.startswith('pending_items:'):
            _RESULT_CACHE.pop(_k, None)


def cache_clear(*keys):
    for k in keys:
        _RESULT_CACHE.pop(k, None)


def init_config_order():
    """第一次啟動時，按舊有顯示次序寫入 cat_order / item_order（之後不會再改動）。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM venue_configs WHERE COALESCE(cat_order,0) <> 0 OR COALESCE(item_order,0) <> 0")
        if c.fetchone()[0] > 0:
            return
        c.execute("""SELECT category, problem, id FROM venue_configs
                     ORDER BY CASE
                        WHEN category = '賭檯 - Display(大Mon)' THEN 1
                        WHEN category = '賭檯 - Display(細Mon)' THEN 2
                        WHEN category = '賭檯 - Layout' THEN 3
                        WHEN category = '賭檯 - Sound Bar' THEN 4
                        ELSE 5 END, category ASC, id ASC""")
        rows = c.fetchall()
        cat_order = {}
        item_counter = {}
        for cat, prob, rid in rows:
            if cat not in cat_order:
                cat_order[cat] = (len(cat_order) + 1) * 10
                item_counter[cat] = 0
            item_counter[cat] += 1
            c.execute("UPDATE venue_configs SET cat_order = %s, item_order = %s WHERE id = %s",
                      (cat_order[cat], item_counter[cat], rid))
        conn.commit()
        print("選項排序初始化完成，共", len(cat_order), "個類別")
    except Exception as e:
        print("選項排序初始化失敗:", e)
        if conn: conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)


# ---------- 問題項編碼化（shift_items）：每個問題項有獨立身份 ----------
_FW = '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ'
_HW = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
_FW_TABLE = str.maketrans(_FW, _HW)


def _norm_code(s):
    """台號正規化：全形轉半形、去空白、轉大寫。"""
    s = (s or '').strip().translate(_FW_TABLE)
    return re.sub(r'\s+', '', s).upper()


def _split_key(key):
    p = (key or '').split('|')
    return (p[0] if p else ''), (p[1] if len(p) > 1 else ''), (p[2] if len(p) > 2 else '')


def _item_key(it):
    """由結構化欄位決定性計算身份證（不靠文字反推）。
    場面｜台號｜類別-問題；Lux/NFC｜台號｜Box n；溫度/分貝｜台號"""
    t = (it.get('type') or '').strip()
    code = _norm_code(it.get('code'))
    if t == '場面':
        d = ' - '.join([x.strip() for x in [it.get('category') or '', it.get('problem') or ''] if x and x.strip()])
        if not d:
            d = (it.get('detail') or '').strip()
        return '場面|%s|%s' % (code, d)
    if t in ('Lux', 'NFC'):
        m = re.search(r'(\d+)', it.get('detail') or '')
        return '%s|%s|Box %s' % (t, code, m.group(1) if m else '-')
    return '%s|%s' % (t, code)


def _item_display(it):
    t = it.get('type') or ''
    code = it.get('code') or ''
    if t == '場面':
        d = ' - '.join([x.strip() for x in [it.get('category') or '', it.get('problem') or ''] if x and str(x).strip()])
        s = d or (it.get('detail') or '')
    else:
        s = ('%s %s' % (t, it.get('detail') or '')).strip()
    s = (s or '').strip()
    return ('%s（%s）' % (s, code)) if code else s


def _done_keys_from_note(note):
    """抽出 note 裡【早前已處理】的 key（只要新 key 格式，舊文字格式不理）。"""
    keys = []
    m = re.search(r'【早前已處理】([\s\S]*?)(?=【|$)', str(note or ''))
    if not m:
        return keys
    for line in m.group(1).split('\n'):
        line = line.strip()
        if not line:
            continue
        k = line.split(' [狀態:')[0].strip()
        if '|' in k and k not in keys:
            keys.append(k)
    return keys


def _si_insert(c, record_id, source, key, it=None, status='', event_at=None, resolved_by=''):
    """寫一筆 shift_items（occurrence）。"""
    it = it or {}
    t, code, detail = _split_key(key)
    itype = it.get('type') or t
    icode = it.get('code') or code
    _st = status or it.get('status') or '待處理'
    _is_done = any(x in _st for x in ('已處理', '已完成', '已解決'))
    c.execute("""INSERT INTO shift_items
                 (record_id, source, item_key, type, code, category, problem, detail, side,
                  status, note, display, created_at, event_at, resolved_at, resolved_by)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW(), COALESCE(%s, NOW()), %s, %s)""",
              (record_id, source, key, itype, icode,
               it.get('category') or '', it.get('problem') or '',
               it.get('detail') or detail, it.get('side') or '',
               status or it.get('status') or '待處理', it.get('note') or '',
               _item_display({'type': itype, 'code': icode,
                              'category': it.get('category') or '', 'problem': it.get('problem') or '',
                              'detail': it.get('detail') or detail}),
               event_at, (event_at if _is_done else None), resolved_by or ''))


def _backfill_shift_items(c):
    """一次性：把 records 舊資料轉成 shift_items。"""
    c.execute("SELECT id, name, note, checklist, items, created_at FROM records ORDER BY id ASC")
    rows = c.fetchall()
    n = 0
    for rid, name, note, checklist, items, created in rows:
        ritems = items or []
        if not ritems:
            try:
                ritems = _record_items(note, checklist)
            except Exception:
                ritems = []
        done_keys = _done_keys_from_note(note)
        seen = set()
        for it in (ritems or []):
            key = it.get('key') or ''
            if not key or key in seen or key in done_keys:
                continue
            seen.add(key)
            _si_insert(c, rid, 'backfill', key, it, it.get('status') or '待處理', created, '')
            n += 1
        for key in done_keys:
            _si_insert(c, rid, 'backfill', key, None, '已處理', created, name or '')
            n += 1
    print('shift_items backfill 完成，共', n, '筆')


def ensure_shift_items():
    """建表 + 首次自動 backfill（部署即執行）。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS shift_items (
                        id BIGSERIAL PRIMARY KEY,
                        record_id BIGINT,
                        source TEXT NOT NULL DEFAULT 'submit',
                        item_key TEXT NOT NULL,
                        type TEXT NOT NULL DEFAULT '',
                        code TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        problem TEXT NOT NULL DEFAULT '',
                        detail TEXT NOT NULL DEFAULT '',
                        side TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '待處理',
                        note TEXT NOT NULL DEFAULT '',
                        display TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        event_at TIMESTAMPTZ,
                        resolved_at TIMESTAMPTZ,
                        resolved_by TEXT NOT NULL DEFAULT ''
                     )""")
        c.execute("ALTER TABLE shift_items ADD COLUMN IF NOT EXISTS event_at TIMESTAMPTZ")
        # 修復舊 reopen/resolve 行空白欄位（由同一 item_key 的其他行補回）
        c.execute("""UPDATE shift_items t
                     SET category = s.category, problem = s.problem
                     FROM (SELECT DISTINCT ON (item_key) item_key, category, problem
                           FROM shift_items WHERE COALESCE(category,'') <> ''
                           ORDER BY item_key, id DESC) s
                     WHERE t.item_key = s.item_key AND COALESCE(t.category,'') = ''""")
        c.execute("UPDATE shift_items SET event_at = COALESCE(resolved_at, created_at) WHERE event_at IS NULL")
        c.execute("CREATE INDEX IF NOT EXISTS idx_si_key ON shift_items(item_key, event_at DESC, id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_si_record ON shift_items(record_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_si_event ON shift_items(event_at DESC, id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_si_status ON shift_items(type, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at DESC, id DESC)")
        # 清理孤立行：record 已不存在（例如直接喺 Supabase 刪紀錄）→ 連 shift_items 一齊清
        try:
            c.execute("""DELETE FROM shift_items s
                         WHERE s.record_id IS NOT NULL
                           AND NOT EXISTS (SELECT 1 FROM records r WHERE r.id = s.record_id)""")
            if c.rowcount:
                print('清理孤立 shift_items:', c.rowcount, '筆')
        except Exception as _e:
            print('清理孤立 shift_items 失敗:', _e)
        conn.commit()
        # 一次性 seed：用 app_settings 記版本，重建也不會亂序
        if str(get_setting('shift_items_seed', '')) != 'v1':
            c.execute("DELETE FROM shift_items WHERE source = 'backfill'")
            _backfill_shift_items(c)
            conn.commit()
            set_setting('shift_items_seed', 'v1')
            print('shift_items seed v1 完成')
        print('shift_items 就緒')
    except Exception as e:
        print('shift_items 初始化失敗:', e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


ensure_tables()
init_config_order()

# ---------- 舊域名自動跳轉新域名（令舊微信名片／舊連結仍然可用） ----------
PRIMARY_HOST = (os.environ.get('PRIMARY_HOST') or 'bs-shift.xyz').strip().lower()


@app.before_request
def force_primary_domain():
    """
    shift-app-wvgf.onrender.com 之類的舊網址 → 301 跳轉至新域名。
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


@app.after_request
def _gzip_response(response):
    """文字／JSON 回應 gzip（手機網絡明顯快啲）。"""
    try:
        if response.direct_passthrough:
            return response
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return response
        if response.headers.get('Content-Encoding'):
            return response
        mt = (response.mimetype or '')
        if not (mt.startswith('text/') or mt in ('application/json', 'application/javascript', 'image/svg+xml')):
            return response
        data = response.get_data()
        if len(data) < 1200:
            return response
        import gzip as _gzip
        comp = _gzip.compress(data, 6)
        if len(comp) >= len(data):
            return response
        response.set_data(comp)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(comp))
        response.headers['Vary'] = 'Accept-Encoding'
    except Exception as _e:
        print('gzip skip:', _e)
    return response


@app.after_request
def _security_headers(response):
    """安全標頭＋CSP（只准自己網站嘅 script，阻止任何外來腳本／內嵌框架）。"""
    try:
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=()')
        response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
        if not response.headers.get('Content-Security-Policy'):
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "media-src 'self'; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'"
            )
    except Exception as _e:
        print('security header skip:', _e)
    return response


@app.route('/diag')
def diag_page():
    """診斷頁：完全冇 JavaScript。若此頁仍彈窗，證明彈窗唔係本站發出。"""
    import hashlib as _hl
    _idx = os.path.join(app.root_path, 'templates', 'index.html')
    _sha = ''
    try:
        with open(_idx, 'rb') as _f:
            _sha = _hl.sha256(_f.read()).hexdigest()[:16]
    except Exception:
        _sha = 'n/a'
    html = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>診斷頁 - BS 交更系統</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:18px;background:#f6f7fb;color:#222;line-height:1.7}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
h1{font-size:18px;margin:0 0 10px}code{background:#eef1f5;padding:2px 6px;border-radius:6px;font-size:12px}
.warn{color:#c62828;font-weight:bold}</style></head><body>
<div class="card"><h1>🔍 安全診斷頁</h1>
<div><b>本頁完全冇 JavaScript</b>（冇任何 script 標籤、冇 onclick）。</div>
<div style="margin-top:8px">如果喺呢頁都彈「將公司數據掛載在外部伺服器…」或者其他怪彈窗，<span class="warn">就 100% 證明唔係網站發出</span>，而係手機／瀏覽器／其他 App 層面。</div>
</div>
<div class="card">
<div>伺服器版本：<code>%(ver)s</code></div>
<div>index.html SHA256（前 16 位）：<code>%(sha)s</code></div>
<div>伺服器時間：<code>%(now)s</code></div>
<div>你嘅瀏覽器：<code>%(ua)s</code></div>
</div>
<div class="card">
<div>下一步：</div>
<div>1. 若本頁冇彈 → 請回主頁，留意頁面頂部有冇<b>紅色警告條</b>（有就表示被注入，並會自動上報詳情給我）。</div>
<div>2. 若本頁有彈 → 請清除此網站資料（Chrome → 設定 → 網站設定 → 所有網站 → bs-shift.xyz → 清除並重設），或用無痕模式／另一個瀏覽器再測。</div>
<div style="margin-top:8px"><a href="/">← 返回主頁</a></div>
</div>
</body></html>""" % {
        'ver': 'v20261127-09',
        'sha': _sha,
        'now': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ua': (request.headers.get('User-Agent') or '')[:160],
    }
    return Response(html, mimetype='text/html')


@app.route('/api/security_report', methods=['POST'])
def security_report():
    """前端自我檢查發現異常時上報（寫 audit_logs，方便日後追查）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    detail = (str(d.get('detail') or ''))[:900]
    try:
        log_action('安全報告', 'UA=' + (request.headers.get('User-Agent') or '')[:120] + ' | ' + detail)
    except Exception:
        pass
    return jsonify({'status': 'success'})


@app.route('/healthz')
def healthz():
    return "ok v20261127-09", 200, {'Content-Type': 'text/plain'}


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
                            "message": "⏰ 你的登入已失效（密碼已被更改），請重新登入。"}), 401
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
            session['user_name'] = '管理員'
            session['epoch'] = get_epoch('admin', force=True)
            log_action('登入', '管理員登入')
            return redirect(url_for('index'))
        elif pwd and pwd == staff_pwd:
            session['logged_in'] = True
            session['role'] = 'staff'
            session['user_name'] = '同事'
            session['epoch'] = get_epoch('staff', force=True)
            log_action('登入', '同事登入')
            return redirect(url_for('index'))
        else:
            # 個人帳號（帳號＝員工ID，密碼＝BS＋員工ID）
            member = None
            if pwd:
                conn = None
                try:
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute("""SELECT name, COALESCE(role, 'staff')
                                 FROM members
                                 WHERE password = %s AND COALESCE(account, '') <> ''
                                 LIMIT 1""", (pwd,))
                    member = c.fetchone()
                except Exception as e:
                    print('個人帳號登入檢查失敗:', e)
                finally:
                    if conn:
                        release_db_connection(conn)

            if member:
                role = 'admin' if member[1] == 'admin' else 'staff'
                session['logged_in'] = True
                session['role'] = role
                session['user_name'] = member[0]
                session['epoch'] = get_epoch(role, force=True)
                log_action('登入', '個人帳號：%s' % member[0])
                return redirect(url_for('index'))
            error = "密碼錯誤！"
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        # 普通帳號：直接返回首頁（前端亦已隱藏「設定」按鈕）
        return redirect(url_for('index'))
    return render_template('admin.html')

@app.route('/api/diag', methods=['GET'])
def diag():
    """診斷（純文字，方便手機查看）。"""
    cache_clear('configs')
    conn = None
    lines = []
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM venue_configs")
        lines.append('total_rows=' + str(c.fetchone()[0]))

        c.execute("SELECT COUNT(DISTINCT category) FROM venue_configs")
        lines.append('total_categories=' + str(c.fetchone()[0]))

        c.execute("""SELECT column_name FROM information_schema.columns
                     WHERE table_name = 'venue_configs' ORDER BY column_name""")
        lines.append('columns=' + ','.join(r[0] for r in c.fetchall()))

        c.execute("""SELECT COALESCE(cat_order,-1), COUNT(*) FROM venue_configs
                     GROUP BY COALESCE(cat_order,-1) ORDER BY 1 LIMIT 8""")
        lines.append('cat_order_values=' + ' | '.join(str(r[0]) + 'x' + str(r[1]) for r in c.fetchall()))

        lines.append('--- SQL 排序頭 5 個（repr 顯示隱藏字元）---')
        c.execute("""SELECT category, COALESCE(cat_order,0), COALESCE(item_order,0), id
                     FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC
                     LIMIT 5""")
        for r in c.fetchall():
            lines.append('  cat=' + str(r[1]) + ' item=' + str(r[2]) + ' id=' + str(r[3]) + ' name=' + repr(r[0]))

        lines.append('--- get_configs 頭 3 個 key ---')
        c.execute("""SELECT category FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        keys = []
        for r in c.fetchall():
            if r[0] not in keys:
                keys.append(r[0])
        for k in keys[:3]:
            lines.append('  ' + repr(k))

        lines.append('--- 可能有重複／相似類別（前 6）---')
        for k in keys[:6]:
            lines.append('  ' + repr(k) + ' 長度=' + str(len(k)))

        return Response('\n'.join(lines), mimetype='text/plain; charset=utf-8',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return Response('ERROR: ' + str(e), mimetype='text/plain; charset=utf-8')
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/get_configs', methods=['GET'])
def get_configs():
    cached = cache_get('configs')
    if cached is not None:
        resp = Response(json.dumps(cached, ensure_ascii=False), mimetype='application/json; charset=utf-8')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT category, problem FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        rows = c.fetchall()
        configs = {}
        for row in rows:
            if row[0] not in configs:
                configs[row[0]] = []
            configs[row[0]].append(row[1])
        cache_set('configs', configs, 300)
        resp = Response(json.dumps(configs, ensure_ascii=False), mimetype='application/json; charset=utf-8')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Get Configs Error:", e)
        return jsonify({})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/configs_detail', methods=['GET'])
def configs_detail():
    """回傳帶 id 的選項清單（admin 排序用）。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, category, problem FROM venue_configs
                     ORDER BY COALESCE(cat_order,0) ASC, category ASC, COALESCE(item_order,0) ASC, id ASC""")
        rows = c.fetchall()
        resp = jsonify([{"id": r[0], "category": r[1], "problem": r[2]} for r in rows])
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Configs Detail Error:", e)
        return jsonify([])
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/reorder_config', methods=['POST'])
def reorder_config():
    """上下移動類別或問題選項。"""
    data = request.json or {}
    rtype = (data.get('type') or '').strip()
    direction = (data.get('direction') or '').strip()
    if direction not in ('up', 'down'):
        return jsonify({"status": "error", "message": "❌ 移動方向不正確。"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if rtype == 'category':
            category = (data.get('category') or '').strip()
            if not category:
                return jsonify({"status": "error", "message": "❌ 缺少類別資料。"})
            c.execute("""SELECT category FROM venue_configs
                         GROUP BY category
                         ORDER BY MIN(COALESCE(cat_order,0)) ASC, category ASC""")
            names = [r[0] for r in c.fetchall()]
            if category not in names:
                return jsonify({"status": "error", "message": "❌ 找不到該類別。"})
            i = names.index(category)
            j = i - 1 if direction == 'up' else i + 1
            if j < 0 or j >= len(names):
                return jsonify({"status": "success", "message": "已移至最" + ('前' if direction == 'up' else '後') + "面。"})
            names[i], names[j] = names[j], names[i]
            for k, name in enumerate(names, start=1):
                c.execute("UPDATE venue_configs SET cat_order = %s WHERE category = %s", (k * 10, name))
            conn.commit()
            cache_clear('configs')
            return jsonify({"status": "success", "message": "✅ 已調整類別次序。"})

        if rtype == 'problem':
            rid = data.get('id')
            if not rid:
                return jsonify({"status": "error", "message": "❌ 缺少選項資料。"})
            c.execute("SELECT category FROM venue_configs WHERE id = %s", (rid,))
            row = c.fetchone()
            if not row:
                return jsonify({"status": "error", "message": "❌ 找不到該選項。"})
            c.execute("""SELECT id FROM venue_configs WHERE category = %s
                         ORDER BY COALESCE(item_order,0) ASC, id ASC""", (row[0],))
            ids = [r[0] for r in c.fetchall()]
            i = ids.index(rid)
            j = i - 1 if direction == 'up' else i + 1
            if j < 0 or j >= len(ids):
                return jsonify({"status": "success", "message": "已移至最" + ('前' if direction == 'up' else '後') + "面。"})
            ids[i], ids[j] = ids[j], ids[i]
            for k, oid in enumerate(ids, start=1):
                c.execute("UPDATE venue_configs SET item_order = %s WHERE id = %s", (k, oid))
            conn.commit()
            cache_clear('configs')
            return jsonify({"status": "success", "message": "✅ 已調整問題次序。"})

        return jsonify({"status": "error", "message": "❌ 未知的排序類型。"})
    except Exception as e:
        print("Reorder Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 排序失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)


def _cfg_prefix(name):
    """用於判斷同一個群組（例：『賭檯 - xxx』、『燈-xxx』）。"""
    n = (name or '').strip()
    for sep in (' - ', '-'):
        if sep in n:
            return n.split(sep, 1)[0].strip()
    return n


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
            return jsonify({"status": "error", "message": "⚠️ 此選項已經存在。"})

        c.execute("""SELECT COALESCE(MAX(cat_order),0), COALESCE(MAX(item_order),0)
                     FROM venue_configs WHERE category = %s""", (category,))
        row = c.fetchone()
        cat_order = row[0] or 0
        item_order = (row[1] or 0) + 1
        is_new_cat = (cat_order == 0)
        if is_new_cat:
            c.execute("SELECT COALESCE(MAX(cat_order),0) FROM venue_configs")
            cat_order = (c.fetchone()[0] or 0) + 10
        c.execute("INSERT INTO venue_configs (category, problem, cat_order, item_order) VALUES (%s, %s, %s, %s)",
                  (category, problem, cat_order, item_order))

        if is_new_cat:
            # 新類別自動排入同一個群組後面（例：賭檯 - 新項目 會排在其他「賭檯」類別之後）
            c.execute("""SELECT category, MIN(COALESCE(cat_order,0)) AS o
                         FROM venue_configs GROUP BY category ORDER BY o ASC, category ASC""")
            order = [r[0] for r in c.fetchall() if r[0] != category]
            pos = len(order)
            prefix = _cfg_prefix(category)
            if prefix:
                for i, name in enumerate(order):
                    if _cfg_prefix(name) == prefix:
                        pos = i + 1
            order.insert(pos, category)
            for idx, name in enumerate(order):
                c.execute("UPDATE venue_configs SET cat_order = %s WHERE category = %s",
                          ((idx + 1) * 10, name))

        conn.commit()
        cache_clear('configs')
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
    刪除場面問題選項（任何已登入的同事均可使用）。
    problem 有值 → 只刪該類別下的該個選項；
    problem 留空 → 刪除整個類別（連同該類別底下所有選項）。
    已提交的舊紀錄不受影響（紀錄儲存文字，不是儲存 id）。
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
            return jsonify({"status": "error", "message": "⚠️ 找不到要刪除的選項（可能已被其他人刪除）。"})

        cache_clear('configs')
        log_action('刪除選項', f"{category} / {problem or '整個類別'}（共 {deleted}）")
        return jsonify({"status": "success", "message": f"✅ 已刪除 {deleted} 個選項。", "deleted": deleted})
    except Exception as e:
        print("Delete Config Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 刪除失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

def _si_rename_keys(c, old_third, new_third, new_cat=None, new_prob=None):
    """改名時同步 shift_items 的 item_key。
    old_third／new_third 是 key 的第三段（例如「櫈 - 客櫈 - 扶手鬆脫」）。
    不依賴 category/problem 欄位（reopen 行可能為空）。"""
    c.execute("SELECT id, item_key, display FROM shift_items WHERE item_key LIKE %s",
              ('%|' + old_third,))
    rows = c.fetchall()
    n = 0
    for rid, key, disp in rows:
        parts = key.split('|')
        if len(parts) < 3:
            continue
        new_key = parts[0] + '|' + parts[1] + '|' + new_third
        new_disp = (disp or '').replace(old_third, new_third)
        sets = ["item_key=%s", "display=%s"]
        params = [new_key, new_disp]
        if new_cat is not None:
            sets.append("category=%s")
            params.append(new_cat)
        if new_prob is not None:
            sets.append("problem=%s")
            params.append(new_prob)
        params.append(rid)
        c.execute("UPDATE shift_items SET " + ", ".join(sets) + " WHERE id=%s", params)
        n += 1
    return n


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
            # 同步已存在的問題項（否則改名後同一問題會變兩項）
            c.execute("SELECT DISTINCT item_key FROM shift_items WHERE type = '場面' AND item_key LIKE %s",
                      ('%|' + old_cat + ' - %',))
            for (_k,) in c.fetchall():
                _p = _k.split('|')
                if len(_p) < 3:
                    continue
                _new_third = new_cat + _p[2][len(old_cat):]
                _si_rename_keys(c, _p[2], _new_third, new_cat=new_cat)

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
            # 同步已存在的問題項
            _si_rename_keys(c, category + ' - ' + old_prob, category + ' - ' + new_prob,
                            new_cat=category, new_prob=new_prob)
            c.execute("UPDATE venue_configs SET problem = %s WHERE category = %s AND problem = %s",
                      (new_prob, category, old_prob))

        else:
            return jsonify({"status": "error", "message": "❌ 未知的修改類型。"})

        updated = c.rowcount
        conn.commit()

        if updated == 0:
            return jsonify({"status": "error", "message": "⚠️ 找不到要修改的資料。"})

        cache_clear('configs')
        log_action('修改選項名稱', f"{rtype} / 更新 {updated} 筆")
        return jsonify({"status": "success", "message": f"✅ 已更新 {updated} 筆。", "updated": updated})
    except Exception as e:
        print("Rename Config Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": "❌ 修改失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)


def _record_items(note, checklist=''):
    """把一筆交更解析成結構化問題項（每個有獨立 key）。"""
    items = []
    seen = set()
    for line in str(note or '').split('\n'):
        if '位置' not in line:
            continue
        for it in _up_parse_line(line):
            code = _up_norm(it.get('code') or '')
            cat = it.get('category') or ''
            prob = it.get('problem') or ''
            detail = (' - '.join([x for x in (cat, prob) if x])).strip()
            key = '場面|%s|%s' % (code, detail)
            if key in seen:
                continue
            seen.add(key)
            items.append({'key': key, 'type': '場面', 'code': code,
                          'detail': detail, 'status': it.get('status') or ''})
    for part in str(checklist or '').split(' | '):
        part = part.strip()
        if not part.startswith('NFC異常:'):
            continue
        raw = part.replace('NFC異常:', '').strip()
        for seg in re.split(r',\s*(?=[A-Za-z]{2}\d{3,6}\s*\()', raw):
            seg = seg.strip()
            m = re.match(r'([A-Za-z]{2}\d{3,6})\s*\((.*?)\)', seg)
            if not m:
                continue
            code = _up_norm(m.group(1))
            detail = m.group(2).replace('Box:', '').strip()
            key = 'NFC|%s|%s' % (code, detail)
            if key in seen:
                continue
            seen.add(key)
            st = re.search(r'\[狀態[:：]\s*([^\]]*)\]', seg)
            items.append({'key': key, 'type': 'NFC', 'code': code,
                          'detail': detail, 'status': (st.group(1).strip() if st else '')})
    # 【早前已處理】段落：把 key 標記為已處理（用於覆蓋舊 pending）
    m = re.search(r'【早前已處理】([\s\S]*?)(?=【|$)', str(note or ''))
    if m:
        for line in m.group(1).split('\n'):
            line = line.strip()
            if not line:
                continue
            key = line.split(' [狀態:')[0].strip()
            if '|' not in key:
                continue
            if key in seen:
                # 同一 key 已在本文出現：覆蓋為已處理（否則 overwrite 重算後問題會復活）
                for _it in items:
                    if _it.get('key') == key:
                        _it['status'] = '已處理'
                continue
            seen.add(key)
            parts = key.split('|')
            items.append({'key': key, 'type': parts[0] if parts else '',
                          'code': parts[1] if len(parts) > 1 else '',
                          'detail': parts[2] if len(parts) > 2 else '',
                          'status': '已處理'})
    return items



def _si_resolve(c, record_id, key, who=''):
    """寫一筆 source='resolve' 的 shift_items（標記完成）。"""
    key = (key or '').strip()
    if not key or '|' not in key:
        return False
    c.execute("""SELECT item_key, type, code, category, problem, detail, side, note, display
                 FROM shift_items WHERE item_key = %s ORDER BY id DESC LIMIT 1""", (key,))
    row = c.fetchone()
    if row:
        v = list(row)
    else:
        t, code, detail = _split_key(key)
        v = [key, t, code, '', '', detail, '', '',
             _item_display({'type': t, 'code': code, 'detail': detail})]
    c.execute("""INSERT INTO shift_items
                 (record_id, source, item_key, type, code, category, problem, detail, side,
                  status, note, display, event_at, resolved_at, resolved_by)
                 VALUES (%s,'resolve',%s,%s,%s,%s,%s,%s,%s,'已處理',%s,%s,NOW(),NOW(),%s)""",
              (record_id, v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], who or ''))
    return True


def _si_set_status(c, record_id, key, status, who=''):
    """把某個問題項改成指定狀態（append-only，寫一筆 source='status'）。"""
    key = (key or '').strip()
    if not key or '|' not in key:
        return False
    c.execute("""SELECT item_key, type, code, category, problem, detail, side, note, display
                 FROM shift_items WHERE item_key = %s ORDER BY id DESC LIMIT 1""", (key,))
    row = c.fetchone()
    if row:
        v = list(row)
    else:
        t, code, detail = _split_key(key)
        v = [key, t, code, '', '', detail, '', '',
             _item_display({'type': t, 'code': code, 'detail': detail})]
    _done = any(x in status for x in ('已處理', '已完成', '已解決'))
    c.execute("""INSERT INTO shift_items
                 (record_id, source, item_key, type, code, category, problem, detail, side,
                  status, note, display, event_at, resolved_at, resolved_by)
                 VALUES (%s,'status',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s)""",
              (record_id, v[0], v[1], v[2], v[3], v[4], v[5], v[6], status, v[7], v[8],
               (datetime.datetime.now() if _done else None), (who if _done else '')))
    return True


def _si_write_done(c, record_id, keys, who=''):
    """把前端勾選完成的 key 寫成 resolve 行。"""
    n = 0
    for key in (keys or []):
        if _si_resolve(c, record_id, key, who):
            n += 1
    return n



def _si_write_reopen(c, record_id, keys):
    """把「取消勾選」的項目寫回待處理（append-only）。
    會複製該問題項最新一筆的結構化欄位（類別／問題／顯示），
    否則更新表／櫈況圖會因為欄位空白而辨認不到。"""
    n = 0
    for key in (keys or []):
        key = (key or '').strip()
        if not key or '|' not in key:
            continue
        c.execute("""SELECT item_key, type, code, category, problem, detail, side, note, display
                     FROM shift_items WHERE item_key = %s ORDER BY id DESC LIMIT 1""", (key,))
        row = c.fetchone()
        if row:
            v = list(row)
        else:
            t, code, detail = _split_key(key)
            v = [key, t, code, '', '', detail, '', '',
                 _item_display({'type': t, 'code': code, 'detail': detail})]
        c.execute("""INSERT INTO shift_items
                     (record_id, source, item_key, type, code, category, problem, detail, side,
                      status, note, display, event_at, resolved_at, resolved_by)
                     VALUES (%s,'reopen',%s,%s,%s,%s,%s,%s,%s,'待處理',%s,%s,NOW(),NULL,'')""",
                  (record_id, v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8]))
        n += 1
    return n


def _si_guess_cat_prob(it):
    """category／problem 空白時，由 detail 推導（以最後一個 ' - ' 分割）。
    例：'櫈 - 客櫈 - 坐墊破損' → ('櫈 - 客櫈', '坐墊破損')；'地墊 - 污漬' → ('地墊', '污漬')"""
    cat = (it.get('category') or '').strip()
    prob = (it.get('problem') or '').strip()
    if cat:
        return cat, prob
    detail = (it.get('detail') or '').strip()
    if not detail:
        return '', prob
    if ' - ' in detail:
        left, right = detail.rsplit(' - ', 1)
        return left.strip(), (prob or right.strip())
    return '', prob


def _si_write_items(c, record_id, items, source='submit', event_map=None):
    """把前端送來的結構化 items 寫入 shift_items（同 key 只寫一次）。"""
    if not items:
        return 0
    seen = set()
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        _cat, _prob = _si_guess_cat_prob(it)
        it2 = dict(it)
        if _cat and not (it2.get('category') or '').strip():
            it2['category'] = _cat
        if _prob and not (it2.get('problem') or '').strip():
            it2['problem'] = _prob
        key = _item_key(it2)
        if not key or key in seen:
            continue
        code = (it2.get('code') or '').strip()
        if not code:
            continue
        seen.add(key)
        _ea = (event_map or {}).get(key)
        _si_insert(c, record_id, source, key, it2, it2.get('status') or '待處理', _ea, '')
        n += 1
    return n


@app.route('/api/submit', methods=['POST'])
def submit():
    data = request.json
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now_macau = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        _items = _record_items(data.get('note'), data.get('checklist'))
        c.execute("""INSERT INTO records 
                     (name, shift, lux, temp, bgm, checklist, status, follower, on_duty, note, lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, layout_side, soundbar, firecheck, firecheck15, firecheck30, wifi1, wifi15, shift_day, created_at, items) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('on_duty'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'),
                   data.get('layout_side'), data.get('soundbar') or '', data.get('firecheck15') or data.get('firecheck') or '',
                   data.get('firecheck15') or '', data.get('firecheck30') or '',
                   data.get('wifi1') or '', data.get('wifi15') or '',
                   data.get('shift_day') or None,
                   now_macau, psycopg2.extras.Json(_items)))
        _new_rid = c.fetchone()[0]
        _si_write_items(c, _new_rid, data.get('items'), 'submit')
        _si_write_done(c, _new_rid, data.get('done_keys'), session.get('user_name') or '')
        try:
            _mk = set(str(k).strip() for k in (data.get('done_keys') or []))
            if _mk:
                _lst = _manual_pending()
                _changed = False
                for _m in _lst:
                    if _m.get('key') in _mk and (_m.get('status') or '') != '已交更':
                        _m['status'] = '已交更'
                        _m['submitted_at'] = _up_today()
                        _changed = True
                if _changed:
                    _manual_save(_lst)
        except Exception as _e:
            print('manual submit mark failed:', _e)
        _si_write_reopen(c, _new_rid, data.get('reopen_keys'))
        conn.commit()
        bust()
        return jsonify({"message": "交更成功！", "status": "success", "record_id": _new_rid})
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

        c.execute("""SELECT created_at, COALESCE(reviewed, FALSE), reviewed_at, COALESCE(reviewed_by, '')
                     FROM records WHERE id = %s""", (old_id,))
        row = c.fetchone()
        if not row:
            return jsonify({"status": "error", "message": "❌ 找不到該紀錄！"})
        old_reviewed, old_reviewed_at, old_reviewed_by = row[1], row[2], row[3]

        if session.get('role') != 'admin':
            c.execute("SELECT NOW() - created_at < interval '20 minutes' FROM records WHERE id = %s", (old_id,))
            is_within_5_mins = c.fetchone()[0]
            if not is_within_5_mins:
                return jsonify({"status": "error", "message": "⏰ 已超過 20 分鐘修改時限，無法修改！"})

        # ★ 改為 UPDATE 同一 row：保留 id／created_at／reviewed，修改後 items 身份不會流失
        now_macau = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        _items = _record_items(data.get('note'), data.get('checklist'))
        c.execute("""UPDATE records SET
                     name=%s, shift=%s, lux=%s, temp=%s, bgm=%s, checklist=%s, status=%s,
                     follower=%s, on_duty=%s, note=%s, lux_issue=%s, temp_issue=%s, db_issue=%s,
                     guest_feedback=%s, dlr_feedback=%s, layout_side=%s, soundbar=%s, firecheck=%s,
                     firecheck15=%s, firecheck30=%s, wifi1=%s, wifi15=%s, shift_day=%s, items=%s
                     WHERE id=%s""", 
                  (data.get('name'), data.get('shift'), data.get('lux'), data.get('temp'), data.get('bgm'), 
                   data.get('checklist'), data.get('status'), data.get('follower'), data.get('on_duty'), data.get('note'),
                   data.get('lux_issue'), data.get('temp_issue'), data.get('db_issue'),
                   data.get('guest_feedback'), data.get('dlr_feedback'),
                   data.get('layout_side'), data.get('soundbar') or '', data.get('firecheck15') or data.get('firecheck') or '',
                   data.get('firecheck15') or '', data.get('firecheck30') or '',
                   data.get('wifi1') or '', data.get('wifi15') or '', data.get('shift_day') or None,
                   psycopg2.extras.Json(_items), old_id))
        # 該筆的問題項重寫（保留 mark_done 寫下的 resolve 記錄）
        # 重寫前記住每個 item_key 最早 event_at，避免「發生日期」變成修改日期
        keep_ea = {}
        try:
            c.execute("SELECT item_key, MIN(event_at) FROM shift_items WHERE record_id = %s GROUP BY item_key", (old_id,))
            for _k, _ea in c.fetchall():
                if _k and _ea:
                    keep_ea[_k] = _ea
        except Exception as _e:
            print('overwrite keep_ea failed:', _e)
        c.execute("DELETE FROM shift_items WHERE record_id = %s AND source <> 'resolve'", (old_id,))
        _si_write_items(c, old_id, data.get('items'), 'submit', keep_ea)
        _si_write_done(c, old_id, data.get('done_keys'), session.get('user_name') or '')
        _si_write_reopen(c, old_id, data.get('reopen_keys'))
        conn.commit()
        bust()
        log_action('修改紀錄', f"紀錄 ID {old_id}")
        return jsonify({"status": "success", "message": "✅ 紀錄已成功修改並覆蓋！"})
    except Exception as e:
        print("Overwrite Error:", e)
        if conn: conn.rollback()
        return jsonify({"status": "error", "message": f"❌ 修改失敗：{str(e)}"})
    finally:
        if conn:
            release_db_connection(conn)

@app.route('/api/records_home', methods=['GET'])
def get_records_home():
    """首頁用：分頁 + 伺服器端篩選（避免一次載入全部紀錄）。"""
    try:
        limit = min(max(int(request.args.get('limit', 60)), 1), 300)
    except Exception:
        limit = 60
    try:
        offset = max(int(request.args.get('offset', 0)), 0)
    except Exception:
        offset = 0

    date_from = (request.args.get('from') or '').strip()
    date_to = (request.args.get('to') or '').strip()
    q = (request.args.get('q') or '').strip()

    where = []
    params = []
    if date_from:
        where.append("(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date >= %s")
        params.append(date_from)
    if date_to:
        where.append("(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s")
        params.append(date_to)
    if q:
        like = '%' + q + '%'
        where.append("(name ILIKE %s OR checklist ILIKE %s OR note ILIKE %s"
                     " OR COALESCE(lux_issue,'') ILIKE %s OR COALESCE(temp_issue,'') ILIKE %s"
                     " OR COALESCE(db_issue,'') ILIKE %s OR COALESCE(guest_feedback,'') ILIKE %s"
                     " OR COALESCE(dlr_feedback,'') ILIKE %s"
                     " OR TO_CHAR(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau', 'YYYY-MM-DD HH24:MI:SS') ILIKE %s)")
        params += [like] * 9

    sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM records" + sql_where, params)
        total = c.fetchone()[0]

        c.execute("""SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note,
                            lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback, layout_side, on_duty,
                            created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed, FALSE),
                            reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed_by, ''),
                            items, COALESCE(soundbar, ''), COALESCE(firecheck, ''),
                            COALESCE(firecheck15, ''), COALESCE(firecheck30, ''),
                            COALESCE(wifi1, ''), COALESCE(wifi15, ''), COALESCE(shift_day, 0),
                            EXTRACT(EPOCH FROM (NOW() - created_at))::int
                     FROM records""" + sql_where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                  params + [limit, offset])
        rows = c.fetchall()
        records = [{
            "id": r[0], "name": r[1], "shift": r[2], "lux": r[3], "temp": r[4], "bgm": r[5],
            "checklist": r[6], "status": r[7], "follower": r[8], "note": r[9],
            "lux_issue": r[10], "temp_issue": r[11], "db_issue": r[12],
            "guest_feedback": r[13], "dlr_feedback": r[14], "layout_side": r[15], "on_duty": r[16],
            "created_at": str(r[17]) if r[17] else None,
            "reviewed": bool(r[18]),
            "reviewed_at": str(r[19]) if r[19] else '',
            "reviewed_by": r[20] or '',
            "items": r[21] or [],
            "soundbar": r[22] or '',
            "firecheck": r[23] or '',
            "firecheck15": r[24] or '',
            "firecheck30": r[25] or '',
            "wifi1": r[26] or '',
            "wifi15": r[27] or '',
            "shift_day": r[28] or 0,
            "age_sec": r[29] if len(r) > 29 else None
        } for r in rows]
        resp = jsonify({"records": records, "total": total, "limit": limit, "offset": offset})
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print("Records Home Error:", e)
        return jsonify({"records": [], "total": 0, "error": str(e)})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/records', methods=['GET'])
def get_records():
    # limit／offset 可選：唔用就照舊回全部（前端需要全量做搜尋／篩選）
    _limit = request.args.get('limit', type=int) or 0
    _offset = request.args.get('offset', type=int) or 0
    conn = get_db_connection()
    c = conn.cursor()
    _sql = """SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note, lux_issue, temp_issue, db_issue,
                 guest_feedback, dlr_feedback, layout_side, on_duty,
                 created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                 COALESCE(reviewed, FALSE),
                 reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                 COALESCE(reviewed_by, ''),
                 items, COALESCE(soundbar, ''), COALESCE(firecheck, ''),
                 COALESCE(firecheck15, ''), COALESCE(firecheck30, ''),
                 COALESCE(wifi1, ''), COALESCE(wifi15, ''), COALESCE(shift_day, 0),
                 EXTRACT(EPOCH FROM (NOW() - created_at))::int
                 FROM records ORDER BY id DESC"""
    if _limit > 0:
        c.execute(_sql + " LIMIT %s OFFSET %s", (min(_limit, 1000), max(0, _offset)))
    else:
        c.execute(_sql)
    rows = c.fetchall()
    release_db_connection(conn)
    records = []
    for row in rows:
        records.append({
            "id": row[0], "name": row[1], "shift": row[2], "lux": row[3], "temp": row[4], "bgm": row[5],
            "checklist": row[6], "status": row[7], "follower": row[8], "note": row[9],
            "lux_issue": row[10], "temp_issue": row[11], "db_issue": row[12],
            "guest_feedback": row[13], "dlr_feedback": row[14], "layout_side": row[15], "on_duty": row[16],
            "created_at": str(row[17]) if row[17] else None,
            "reviewed": bool(row[18]),
            "reviewed_at": str(row[19]) if row[19] else '',
            "reviewed_by": row[20] or '',
            "items": row[21] or [],
            "soundbar": row[22] or '',
            "firecheck": row[23] or '',
            "firecheck15": row[24] or '',
            "firecheck30": row[25] or '',
            "wifi1": row[26] or '',
            "wifi15": row[27] or '',
            "shift_day": row[28] or 0,
            "age_sec": row[29] if len(row) > 29 else None
        })
    return jsonify(records)

# ---------- 匯出（伺服器端產生檔案，手機／微信都可下載） ----------
def _csv_bytes(rows):
    def esc(v):
        s = '' if v is None else str(v)
        return '"' + s.replace('"', '""') + '"'
    body = '\r\n'.join(','.join(esc(c) for c in row) for row in rows)
    return ('\ufeff' + body).encode('utf-8')


def _xls_bytes(rows, sheet_name):
    def x(v):
        s = '' if v is None else str(v)
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    body = ''.join('<Row>' + ''.join('<Cell><Data ss:Type="String">' + x(c) + '</Data></Cell>' for c in row) + '</Row>' for row in rows)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
           'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           '<Worksheet ss:Name="' + x(sheet_name) + '"><Table>' + body + '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


def _feedback_text(v):
    if not v:
        return ''
    try:
        d = json.loads(v)
        parts = []
        if d.get('items'):
            parts.append('內容：' + '、'.join(str(i) for i in d['items']))
        parts.append(f"好評{d.get('good', 0)}／中評{d.get('normal', 0)}／差評{d.get('bad', 0)}")
        if d.get('note'):
            parts.append('備註：' + str(d['note']))
        return '；'.join(parts)
    except Exception:
        return str(v)


def _file_response(rows, sheet_name, fmt):
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d_%H%M')
    if fmt == 'xls':
        data, mimetype, ext = _xls_bytes(rows, sheet_name), 'application/vnd.ms-excel', 'xls'
    else:
        data, mimetype, ext = _csv_bytes(rows), 'text/csv; charset=utf-8', 'csv'
    filename = f"BS_{sheet_name}_{stamp}.{ext}"
    return Response(data, mimetype=mimetype, headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


# ---------- 交更報表模板（圖1 格式） ----------
_REPORT_ROWS = [
    ('Hall1', ['Pit 1', 'Pit 2', 'Pit 30', 'Pit 32', 'Pit 35', 'Pit 31 LT']),
    ('Hall2', ['Pit 3', 'Pit 5', 'Pit 8', 'Pit 26', 'Pit 27', 'Pit 28', 'Pit 29']),
    ('Hall3', ['Pit 33', 'Pit 9', 'Pit 11', 'Pit 12']),
    ('Hall4', ['Pit 25', 'Pit 16']),
    ('Hall5', ['Pit 13', 'Pit 15', 'Pit 17', 'Pit 18', 'Pit 19', 'Pit 20', 'Pit 63 LT']),
    ('Hall6', ['Pit 103', 'Pit 105', 'Pit 106', 'Pit 107']),
]

_REPORT_LOC_DETAIL = {
    'Pit 9': 'Pit 9\nDS x3, BA x23',
    'Pit 11': 'Pit 11\nDS x6, BA x23',
}

_REPORT_ACTIVE_DETAIL = {
    'Pit 1': 'BJ x8,CS x5,RL x4\nFP x6,Craps x2',
}


def _xml_esc(v):
    s = '' if v is None else str(v)
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
             .replace('"', '&quot;').replace('\n', '&#10;'))


def build_report_template():
    def cell(text='', style=None, across=0, down=0, formula=None):
        attrs = ''
        if across:
            attrs += ' ss:MergeAcross="' + str(across) + '"'
        if down:
            attrs += ' ss:MergeDown="' + str(down) + '"'
        if style:
            attrs += ' ss:StyleID="' + style + '"'
        if formula:
            attrs += ' ss:Formula="' + _xml_esc(formula) + '"'
            return '<Cell' + attrs + '/>'
        return ('<Cell' + attrs + '><Data ss:Type="String">' + _xml_esc(text) + '</Data></Cell>')

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')
    rows = []

    # 標題
    rows.append('<Row ss:Height="34">' + cell('Update ' + stamp, 'Title', across=6) + '</Row>')

    # 表頭（兩行）
    rows.append('<Row ss:Height="26">' +
                cell('Mass', 'Head', down=1) +
                cell('Location', 'Head', down=1) +
                cell('ALL', 'Head', across=1) +
                cell('Dealer Chair', 'Head', across=1) +
                cell('Total', 'Head', down=1) +
                '</Row>')
    rows.append('<Row ss:Height="22">' +
                cell('Active', 'HeadPink') +
                cell('Deactive', 'HeadYellow') +
                cell('New', 'Head') +
                cell('Old', 'Head') +
                '</Row>')

    first_data_row = len(rows) + 1
    for hall, pits in _REPORT_ROWS:
        for idx, pit in enumerate(pits):
            r = '<Row ss:Height="20">'
            if idx == 0:
                r += cell(hall, 'Mass', down=len(pits) - 1)
            loc_text = _REPORT_LOC_DETAIL.get(pit, pit)
            active_text = _REPORT_ACTIVE_DETAIL.get(pit, '')
            r += cell(loc_text, 'Loc')
            r += cell(active_text, 'Pink')
            r += cell('', 'Yellow')
            r += cell('', 'Plain')
            r += cell('', 'Plain')
            r += cell('', 'Green')
            r += '</Row>'
            rows.append(r)
    last_data_row = len(rows)

    # 合計行
    total_formula_tpl = '=SUM(R[-{}]C:R[-1]C)'.format(last_data_row - first_data_row + 1)
    rows.append('<Row ss:Height="24">' +
                cell('Total DLR Chair New/Old', 'TotalLabel', across=3) +
                cell(formula=total_formula_tpl, style='TotalCell') +
                cell(formula=total_formula_tpl, style='TotalCell') +
                cell(formula=total_formula_tpl, style='TotalCell') +
                '</Row>')

    border = ('<Borders>'
              '<Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Left" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Right" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Top" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '</Borders>')

    styles = (
        '<Styles>'
        '<Style ss:ID="Default" ss:Name="Normal">'
        '<Alignment ss:Vertical="Center" ss:Horizontal="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Title">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="20" ss:Bold="1" ss:Color="#2E74B5"/></Style>'
        '<Style ss:ID="Head">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadPink">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFD9D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadYellow">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFFF99" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Mass">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="Loc">'
        '<Alignment ss:Horizontal="Left" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Pink">'
        '<Interior ss:Color="#FFECEC" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Yellow">'
        '<Interior ss:Color="#FFFFE0" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Plain">' + border + '</Style>'
        '<Style ss:ID="Green">'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="TotalLabel">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="TotalCell">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="11" ss:Bold="1"/>'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '</Styles>'
    )

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
           ' xmlns:o="urn:schemas-microsoft-com:office:office"'
           ' xmlns:x="urn:schemas-microsoft-com:office:excel"'
           ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           + styles +
           '<Worksheet ss:Name="Update"><Table ss:DefaultRowHeight="18">'
           '<Column ss:Width="52"/><Column ss:Width="110"/>'
           '<Column ss:Width="150"/><Column ss:Width="80"/>'
           '<Column ss:Width="66"/><Column ss:Width="66"/><Column ss:Width="62"/>'
           + ''.join(rows) +
           '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


# ---------- 交更報表：由 hall_tables 自動填數 ----------
def build_update_report(data_rows):
    """data_rows: [(hall, pit, 營運數, 停運數), ...]"""

    def cell(text='', style=None, across=0, down=0, num=None):
        attrs = ''
        if across:
            attrs += ' ss:MergeAcross="' + str(across) + '"'
        if down:
            attrs += ' ss:MergeDown="' + str(down) + '"'
        if style:
            attrs += ' ss:StyleID="' + style + '"'
        if num is not None:
            return ('<Cell' + attrs + '><Data ss:Type="Number">' + str(num) + '</Data></Cell>')
        return ('<Cell' + attrs + '><Data ss:Type="String">' + _xml_esc(text) + '</Data></Cell>')

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')
    rows_xml = []
    rows_xml.append('<Row ss:Height="34">' + cell('Update ' + stamp, 'Title', across=5) + '</Row>')
    rows_xml.append('<Row ss:Height="26">' +
                    cell('Mass', 'Head', down=1) +
                    cell('Location', 'Head', down=1) +
                    cell('ALL', 'Head', across=1) +
                    cell('Total', 'Head', down=1) +
                    '</Row>')
    rows_xml.append('<Row ss:Height="22">' +
                    cell('Active', 'HeadPink') +
                    cell('Deactive', 'HeadYellow') +
                    '</Row>')

    grouped = {}
    order = []
    for hall, pit, act, dea in data_rows:
        if hall not in grouped:
            grouped[hall] = []
            order.append(hall)
        grouped[hall].append((pit, act, dea))

    sum_act = sum_dea = 0
    for hall in order:
        items = grouped[hall]
        for idx, (pit, act, dea) in enumerate(items):
            r = '<Row ss:Height="20">'
            if idx == 0:
                r += cell(hall, 'Mass', down=len(items) - 1)
            r += cell('Pit ' + str(pit), 'Loc')
            r += cell(num=act, style='Pink')
            r += cell(num=dea, style='Yellow')
            r += cell(num=act + dea, style='Green')
            r += '</Row>'
            rows_xml.append(r)
            sum_act += act
            sum_dea += dea

    rows_xml.append('<Row ss:Height="24">' +
                    cell('Total', 'TotalLabel', across=2) +
                    cell(num=sum_act, style='TotalCell') +
                    cell(num=sum_dea, style='TotalCell') +
                    cell(num=sum_act + sum_dea, style='TotalCell') +
                    '</Row>')

    return _xls_from_rows(rows_xml)


def _xls_from_rows(rows_xml):
    border = ('<Borders>'
              '<Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Left" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Right" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '<Border ss:Position="Top" ss:LineStyle="Continuous" ss:Weight="1"/>'
              '</Borders>')

    styles = (
        '<Styles>'
        '<Style ss:ID="Default" ss:Name="Normal">'
        '<Alignment ss:Vertical="Center" ss:Horizontal="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Title">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="20" ss:Bold="1" ss:Color="#2E74B5"/></Style>'
        '<Style ss:ID="Head">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center" ss:WrapText="1"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadPink">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFD9D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="HeadYellow">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#FFFF99" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Mass">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="Loc">'
        '<Alignment ss:Horizontal="Left" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10"/>' + border + '</Style>'
        '<Style ss:ID="Pink">'
        '<Interior ss:Color="#FFECEC" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Yellow">'
        '<Interior ss:Color="#FFFFE0" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="Green">'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>' + border + '</Style>'
        '<Style ss:ID="TotalLabel">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="10" ss:Bold="1"/>'
        '<Interior ss:Color="#CCFFFF" ss:Pattern="Solid"/>' + border + '</Style>'
        '<Style ss:ID="TotalCell">'
        '<Alignment ss:Horizontal="Center" ss:Vertical="Center"/>'
        '<Font ss:FontName="Arial" ss:Size="11" ss:Bold="1"/>'
        '<Interior ss:Color="#D9F2D9" ss:Pattern="Solid"/>' + border + '</Style>'
        '</Styles>'
    )

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
           ' xmlns:o="urn:schemas-microsoft-com:office:office"'
           ' xmlns:x="urn:schemas-microsoft-com:office:excel"'
           ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           + styles +
           '<Worksheet ss:Name="Update"><Table ss:DefaultRowHeight="18">'
           '<Column ss:Width="56"/><Column ss:Width="90"/>'
           '<Column ss:Width="90"/><Column ss:Width="90"/><Column ss:Width="70"/>'
           + ''.join(rows_xml) +
           '</Table></Worksheet></Workbook>')
    return ('\ufeff' + xml).encode('utf-8')


# ---------- 真 .xlsx 產生器（顯示最穩定） ----------
def _xlsx_col(i):
    """1 -> A, 2 -> B ..."""
    out = ''
    while i > 0:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def build_update_report_xlsx(data_rows):
    import io
    import zipfile
    from xml.sax.saxutils import escape as xe

    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%b/%d')

    grouped = {}
    order = []
    for hall, pit, act, dea in data_rows:
        if hall not in grouped:
            grouped[hall] = []
            order.append(hall)
        grouped[hall].append((pit, act, dea))

    rows_xml = []
    merges = []

    def row(idx, cells):
        parts = []
        for col, (style, val, is_num) in cells:
            ref = _xlsx_col(col) + str(idx)
            if is_num:
                parts.append(f'<c r="{ref}" s="{style}"><v>{val}</v></c>')
            else:
                parts.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{xe(str(val))}</t></is></c>')
        rows_xml.append(f'<row r="{idx}" ht="20" customHeight="1">' + ''.join(parts) + '</row>')

    # 標題
    rows_xml.append('<row r="1" ht="34" customHeight="1">'
                    f'<c r="A1" s="1" t="inlineStr"><is><t>Update {stamp}</t></is></c></row>')
    merges.append('A1:E1')

    # 表頭
    rows_xml.append('<row r="2" ht="26" customHeight="1">'
                    '<c r="A2" s="2" t="inlineStr"><is><t>Mass</t></is></c>'
                    '<c r="B2" s="2" t="inlineStr"><is><t>Location</t></is></c>'
                    '<c r="C2" s="2" t="inlineStr"><is><t>ALL</t></is></c>'
                    '<c r="E2" s="2" t="inlineStr"><is><t>Total</t></is></c></row>')
    merges += ['A2:A3', 'B2:B3', 'C2:D2', 'E2:E3']

    rows_xml.append('<row r="3" ht="22" customHeight="1">'
                    '<c r="C3" s="3" t="inlineStr"><is><t>Active</t></is></c>'
                    '<c r="D3" s="4" t="inlineStr"><is><t>Deactive</t></is></c></row>')

    idx = 4
    sum_act = sum_dea = 0
    for hall in order:
        items = grouped[hall]
        start = idx
        for j, (pit, act, dea) in enumerate(items):
            cells = []
            if j == 0:
                cells.append((1, (5, hall, False)))
            cells.append((2, (6, 'Pit ' + str(pit), False)))
            cells.append((3, (7, act, True)))
            cells.append((4, (8, dea, True)))
            cells.append((5, (9, act + dea, True)))
            row(idx, cells)
            sum_act += act
            sum_dea += dea
            idx += 1
        if len(items) > 1:
            merges.append('A' + str(start) + ':A' + str(idx - 1))

    # 合計行
    rows_xml.append(f'<row r="{idx}" ht="24" customHeight="1">'
                    f'<c r="A{idx}" s="10" t="inlineStr"><is><t>Total</t></is></c>'
                    f'<c r="C{idx}" s="11"><v>{sum_act}</v></c>'
                    f'<c r="D{idx}" s="11"><v>{sum_dea}</v></c>'
                    f'<c r="E{idx}" s="11"><v>{sum_act + sum_dea}</v></c></row>')
    merges += [f'A{idx}:B{idx}']

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<cols>'
             '<col min="1" max="1" width="11" customWidth="1"/>'
             '<col min="2" max="2" width="12" customWidth="1"/>'
             '<col min="3" max="4" width="12" customWidth="1"/>'
             '<col min="5" max="5" width="10" customWidth="1"/>'
             '</cols>'
             '<sheetData>' + ''.join(rows_xml) + '</sheetData>'
             '<mergeCells count="' + str(len(merges)) + '">' +
             ''.join(f'<mergeCell ref="{m}"/>' for m in merges) +
             '</mergeCells></worksheet>')

    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="4">'
              '<font><sz val="10"/><name val="Arial"/></font>'
              '<font><b/><sz val="10"/><name val="Arial"/></font>'
              '<font><b/><sz val="20"/><color rgb="FF2E74B5"/><name val="Arial"/></font>'
              '<font><b/><sz val="11"/><name val="Arial"/></font>'
              '</fonts>'
              '<fills count="8">'
              '<fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFCCFFFF"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFD9D9"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF99"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFECEC"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFE0"/><bgColor indexed="64"/></patternFill></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFD9F2D9"/><bgColor indexed="64"/></patternFill></fill>'
              '</fills>'
              '<borders count="2">'
              '<border><left/><right/><top/><bottom/><diagonal/></border>'
              '<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border>'
              '</borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="12">'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="left" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="0" fillId="6" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '<xf numFmtId="0" fontId="3" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
              '</cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              '</styleSheet>')

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '</Types>')

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')

    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Update" sheetId="1" r:id="rId1"/></sheets></workbook>')

    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels)
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


# ---------- 完整備份 / 還原 ----------
_BACKUP_COLUMNS = ['id', 'name', 'shift', 'lux', 'temp', 'bgm', 'checklist', 'status', 'follower',
                   'on_duty', 'note', 'lux_issue', 'temp_issue', 'db_issue', 'guest_feedback',
                   'dlr_feedback', 'layout_side', 'soundbar', 'firecheck', 'firecheck15', 'firecheck30',
                   'wifi1', 'wifi15', 'shift_day',
                   'reviewed', 'reviewed_at', 'reviewed_by', 'created_at', 'items']

_SHIFT_ITEM_COLUMNS = ['id', 'record_id', 'source', 'item_key', 'type', 'code', 'category',
                       'problem', 'detail', 'side', 'status', 'note', 'display',
                       'created_at', 'event_at', 'resolved_at', 'resolved_by']


def _rows_as_dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.route('/api/backup/export', methods=['GET'])
def backup_export():
    """下載完整備份（JSON）：紀錄、選項、台號總表、系統設定、操作紀錄。"""
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以備份。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM records ORDER BY id")
        records = _rows_as_dicts(c)
        c.execute("SELECT * FROM venue_configs ORDER BY id")
        configs = _rows_as_dicts(c)
        c.execute("SELECT * FROM hall_tables ORDER BY id")
        halls = _rows_as_dicts(c)
        c.execute("SELECT * FROM app_settings")
        settings = _rows_as_dicts(c)
        c.execute("SELECT * FROM shift_items ORDER BY id")
        shift_items = _rows_as_dicts(c)
        c.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 2000")
        logs = _rows_as_dicts(c)

        now = datetime.datetime.now(pytz.timezone('Asia/Macau'))
        payload = {
            'format': 'bs-shift-backup',
            'version': 1,
            'exported_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'counts': {'records': len(records), 'configs': len(configs),
                       'hall_tables': len(halls), 'settings': len(settings), 'audit_logs': len(logs),
                       'shift_items': len(shift_items)},
            'records': records,
            'shift_items': shift_items,
            'venue_configs': configs,
            'hall_tables': halls,
            'app_settings': settings,
            'audit_logs': logs,
        }
        body = json.dumps(payload, ensure_ascii=False, default=str, indent=1)
        fname = 'bs_backup_' + now.strftime('%Y%m%d_%H%M') + '.json'
        log_action('下載備份', '紀錄 %d 筆' % len(records))
        return Response(body, mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename="%s"' % fname})
    except Exception as e:
        print('Backup Error:', e)
        return jsonify({'status': 'error', 'message': '❌ 備份失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/backup/import_records', methods=['POST'])
def backup_import_records():
    """由備份還原交更紀錄（只新增，不覆蓋／不刪除）。"""
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以還原。'})

    data = request.json or {}
    rows = data.get('records')
    if not isinstance(rows, list) or not rows:
        return jsonify({'status': 'error', 'message': '❌ 備份檔內沒有紀錄。'})

    allowed = set(_BACKUP_COLUMNS)
    added = skipped = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        for r in rows[:20000]:
            if not isinstance(r, dict):
                skipped += 1
                continue
            cols = [k for k in _BACKUP_COLUMNS if k in r]
            if 'id' not in cols:
                skipped += 1
                continue
            vals = []
            for k in cols:
                v = r[k]
                if k in ('reviewed_at', 'created_at') and (v is None or str(v).strip() == ''):
                    v = None
                if k == 'items':
                    if not isinstance(v, list):
                        v = []
                    v = psycopg2.extras.Json(v)
                vals.append(v)
            sql = 'INSERT INTO records (%s) VALUES (%s) ON CONFLICT (id) DO NOTHING RETURNING id' % (
                ','.join(cols), ','.join(['%s'] * len(cols)))
            c.execute('SAVEPOINT sp')
            try:
                c.execute(sql, vals)
                inserted = c.fetchone()
                c.execute('RELEASE SAVEPOINT sp')
                if inserted:
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                print('還原單筆失敗:', e)
                c.execute('ROLLBACK TO SAVEPOINT sp')
                skipped += 1

        c.execute("SELECT setval(pg_get_serial_sequence('records','id'), "
                  "COALESCE((SELECT MAX(id) FROM records), 1))")

        si_added = si_skipped = 0
        si_rows = data.get('shift_items') or []
        if isinstance(si_rows, list) and si_rows:
            for r in si_rows[:60000]:
                if not isinstance(r, dict):
                    si_skipped += 1
                    continue
                cols = [k for k in _SHIFT_ITEM_COLUMNS if k in r]
                if 'id' not in cols or 'item_key' not in cols:
                    si_skipped += 1
                    continue
                vals = []
                for k in cols:
                    v = r[k]
                    if k in ('created_at', 'event_at', 'resolved_at') and (v is None or str(v).strip() == ''):
                        v = None
                    vals.append(v)
                c.execute('SAVEPOINT spi')
                try:
                    c.execute('INSERT INTO shift_items (%s) VALUES (%s) ON CONFLICT (id) DO NOTHING RETURNING id' % (
                        ','.join(cols), ','.join(['%s'] * len(cols))), vals)
                    if c.fetchone():
                        si_added += 1
                    else:
                        si_skipped += 1
                    c.execute('RELEASE SAVEPOINT spi')
                except Exception as e:
                    print('還原問題項失敗:', e)
                    c.execute('ROLLBACK TO SAVEPOINT spi')
                    si_skipped += 1
            c.execute("SELECT setval(pg_get_serial_sequence('shift_items','id'), "
                      "COALESCE((SELECT MAX(id) FROM shift_items), 1))")
        conn.commit()
        try:
            bust()
        except Exception:
            pass
        log_action('還原紀錄', '新增 %d 筆，略過 %d 筆；問題項新增 %d／略過 %d' % (added, skipped, si_added, si_skipped))
        return jsonify({'status': 'success',
                        'message': '✅ 還原完成：新增 %d 筆，略過 %d 筆；問題項新增 %d、略過 %d。' % (added, skipped, si_added, si_skipped),
                        'added': added, 'skipped': skipped, 'items_added': si_added, 'items_skipped': si_skipped})
    except Exception as e:
        print('Restore Error:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 還原失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/export/report_update', methods=['GET'])
def export_report_update():
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""select hall, pit,
                            count(*) filter (where status = '營運') as act,
                            count(*) filter (where status = '停運') as dea
                     from hall_tables
                     group by hall, pit
                     order by hall, pit""")
        rows = c.fetchall()
    except Exception as e:
        print("Report Update Error:", e)
        return jsonify({"status": "error",
                        "message": "❌ 讀取不通。請先執行「台號總表_seed.sql」建立 hall_tables。"})
    finally:
        if conn:
            release_db_connection(conn)

    if not rows:
        return jsonify({"status": "error", "message": "❌ hall_tables 沒有任何資料。"})

    data = [(r[0], r[1], int(r[2] or 0), int(r[3] or 0)) for r in rows]
    payload = build_update_report_xlsx(data)
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d')
    filename = 'BS_update_' + stamp + '.xlsx'
    return Response(payload, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


@app.route('/api/export/report_template', methods=['GET'])
def export_report_template():
    data = build_report_template()
    stamp = datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y%m%d')
    filename = 'BS_report_template_' + stamp + '.xls'
    return Response(data, mimetype='application/vnd.ms-excel', headers={
        'Content-Disposition': "attachment; filename=" + filename + "; filename*=UTF-8''" + filename,
        'Cache-Control': 'no-store'
    })


def _unit_temp_text(v):
    s = str(v or '').strip()
    if not s:
        return ''
    if '°' in s or '℃' in s:
        return s
    if '偏低' in s:
        return '偏低（< 22.5 °C）'
    if '偏高' in s:
        return '偏高（> 24.5 °C）'
    if '正常' in s:
        return '正常（22.5–24.5 °C）'
    return s


def _unit_bgm_text(v):
    s = str(v or '').strip()
    if not s:
        return ''
    if 'db' in s.lower():
        return s
    if '偏低' in s:
        return '偏低（< 65 dB）'
    if '偏高' in s:
        return '偏高（> 75 dB）'
    if '正常' in s:
        return '正常（65–75 dB）'
    return s


@app.route('/api/export/records', methods=['GET'])
def export_records_file():
    date_from = (request.args.get('from') or '').strip()
    date_to = (request.args.get('to') or '').strip()
    fmt = (request.args.get('format') or 'csv').lower()

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sql = """SELECT id, name, shift, lux, temp, bgm, checklist, status, follower, note,
                        lux_issue, temp_issue, db_issue, guest_feedback, dlr_feedback,
                        COALESCE(soundbar, ''), COALESCE(firecheck15, ''), COALESCE(firecheck30, ''),
                        COALESCE(wifi1, ''), COALESCE(wifi15, ''),
                        created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau'
                 FROM records WHERE 1=1"""
        params = []
        if date_from:
            sql += " AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date >= %s"
            params.append(date_from)
        if date_to:
            sql += " AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s"
            params.append(date_to)
        sql += " ORDER BY id DESC"
        c.execute(sql, params)
        rows = c.fetchall()
    except Exception as e:
        print("Export Records Error:", e)
        return jsonify({"status": "error", "message": "❌ 匯出失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

    out = [['日期時間', '交更人', '更份', '全場Lux', '全場溫度', '全場分貝', '狀態', 'NFC檢測Hall',
            'Checklist', '詳細事項', 'Lux異常', '溫度異常', '分貝異常', '客人意見', 'DLR意見',
            'Sound Bar 檢測', '消防（15日）', '消防（30日）', 'WIFI（1日）', 'WIFI（15日）']]
    for r in rows:
        checklist = r[6] or ''
        hall = ''
        for seg in checklist.split(' | '):
            s = seg.strip()
            if s.startswith('NFC正常'):
                hall = s.replace('NFC正常:', '').replace('NFC正常：', '').strip()
                break
        out.append([
            str(r[17]) if r[17] else '', r[1], r[2], r[3], _unit_temp_text(r[4]), _unit_bgm_text(r[5]), r[7], hall,
            checklist, r[9] or '', r[10] or '', r[11] or '', r[12] or '',
            _feedback_text(r[13]), _feedback_text(r[14]), r[15] or '', r[16] or '', r[17] or '',
            r[18] or '', r[19] or ''
        ])
    return _file_response(out, 'records', fmt)


@app.route('/api/export/configs', methods=['GET'])
def export_configs_file():
    fmt = (request.args.get('format') or 'csv').lower()
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT category, problem FROM venue_configs ORDER BY category ASC, id ASC")
        rows = c.fetchall()
    except Exception as e:
        print("Export Configs Error:", e)
        return jsonify({"status": "error", "message": "❌ 匯出失敗，請稍後再試。"})
    finally:
        if conn:
            release_db_connection(conn)

    out = [['類別', '問題']]
    for r in rows:
        out.append([r[0] or '', r[1] or ''])
    return _file_response(out, 'options', fmt)


@app.route('/api/mark_done', methods=['POST'])
def mark_done():
    """勾選完成：插入一筆 source='resolve' 的 shift_items（append-only）。
    最新一筆自然覆蓋舊狀態，結構上不可能「復活」，且保留完整處理歷史。"""
    data = request.json or {}
    raw = data.get('keys') or []
    if isinstance(raw, str):
        raw = [raw]
    keys = [str(k).strip() for k in raw if str(k).strip()]
    if not keys:
        return jsonify({"status": "error", "message": "❌ 未選擇任何項目"})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        who = session.get('user_name') or session.get('role') or ''
        # 修改某張紀錄時，resolve 事件要綁在該紀錄，才能正確還原勾選狀態
        rid = data.get('record_id')
        try:
            rid = int(rid) if rid else None
        except Exception:
            rid = None
        if not rid:
            c.execute("SELECT id FROM records ORDER BY id DESC LIMIT 1")
            last = c.fetchone()
            rid = last[0] if last else None
        done_count = 0
        for key in keys:
            if _si_resolve(c, rid, key, who):
                done_count += 1
        conn.commit()
        bust()
        if done_count:
            log_action('勾選完成', ','.join(keys)[:200])
        return jsonify({"status": "success", "done_count": done_count,
                        "message": "✅ 已標記 %d 項為已完成" % done_count})
    except Exception as e:
        print('MarkDone Error:', e)
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "❌ 標記失敗：%s" % str(e)})
    finally:
        if conn:
            release_db_connection(conn)


_MANUAL_PENDING_KEY = 'pending_manual'


def _manual_pending(fresh=False):
    if fresh:
        _SET_CACHE.pop(_MANUAL_PENDING_KEY, None)
    raw = get_setting(_MANUAL_PENDING_KEY, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else []
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    return [x for x in data if isinstance(x, dict)]


def _manual_save(lst):
    return set_setting(_MANUAL_PENDING_KEY, json.dumps(lst, ensure_ascii=False))


@app.route('/api/manual_pending', methods=['GET'])
def manual_pending_get():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    items = [x for x in _manual_pending() if (x.get('status') or '待處理') not in ('已交更',)]
    return jsonify({'status': 'success', 'items': items})


@app.route('/api/manual_pending/add', methods=['POST'])
def manual_pending_add():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    date_str = (data.get('date') or '').strip() or _up_today()
    try:
        datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ 日期格式必須係 YYYY-MM-DD。'})
    category = (data.get('category') or '').strip()
    problem = (data.get('problem') or '').strip()
    code = (data.get('code') or '').strip().upper()
    note = (data.get('note') or '').strip()
    done = bool(data.get('done'))
    if not category or not problem:
        return jsonify({'status': 'error', 'message': '❌ 請選擇類別及問題。'})
    key = '手動|' + (code or '-') + '|' + category + ' - ' + problem
    lst = _manual_pending()
    for x in lst:
        if x.get('key') == key and (x.get('status') or '') not in ('已交更',):
            return jsonify({'status': 'error', 'message': '⚠️ 相同項目已在清單內。'})
    item = {'id': str(int(time.time() * 1000)), 'key': key, 'date': date_str,
            'category': category, 'problem': problem, 'code': code, 'note': note,
            'status': ('已處理' if done else '待處理'), 'created_at': _up_today()}
    lst.append(item)
    if not _manual_save(lst):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    bust()
    try:
        log_action('待處理清單', '新增 %s（%s）' % (key, item['status']))
    except Exception:
        pass
    return jsonify({'status': 'success', 'item': item, 'message': '✅ 已加入待處理清單。'})


@app.route('/api/manual_pending/update', methods=['POST'])
def manual_pending_update():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    mid = str(data.get('id') or '')
    lst = _manual_pending()
    hit = None
    for x in lst:
        if str(x.get('id')) == mid:
            hit = x
            break
    if not hit:
        return jsonify({'status': 'error', 'message': '❌ 找不到項目。'})
    if 'done' in data:
        hit['status'] = '已處理' if data.get('done') else '待處理'
    if data.get('date'):
        hit['date'] = str(data.get('date'))
    if not _manual_save(lst):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    bust()
    return jsonify({'status': 'success', 'item': hit, 'message': '✅ 已更新。'})


@app.route('/api/manual_pending/delete', methods=['POST'])
def manual_pending_delete():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    mid = str(data.get('id') or '')
    lst = _manual_pending()
    keep = [x for x in lst if str(x.get('id')) != mid]
    if len(keep) == len(lst):
        return jsonify({'status': 'error', 'message': '❌ 找不到項目。'})
    if not _manual_save(keep):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    bust()
    return jsonify({'status': 'success', 'message': '✅ 已刪除。'})


@app.route('/api/pending_items', methods=['GET'])
def api_pending_items():
    """黃框唯一資料來源：每個 item_key 取最新一筆，不用任何文字解析。"""
    before_id = request.args.get('before_id', type=int)
    _ck = 'pending_items:%s' % (before_id or 0)
    _cached = None if request.args.get('_t') else cache_get(_ck)   # 帶 _t＝強制讀最新（避多 worker 快取不同步）
    if _cached is not None:
        return jsonify(_cached)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sql = ("SELECT d.id, d.record_id, d.item_key, d.type, d.code, d.category,"
               " d.problem, d.detail, d.side, d.status, d.note, d.display, d.source,"
               " to_char(d.created_at AT TIME ZONE 'Asia/Macau', 'YYYY-MM-DD HH24:MI:SS') AS created_at,"
               " (SELECT to_char(MIN(x.event_at) AT TIME ZONE 'Asia/Macau', 'YYYY-MM-DD')"
               "    FROM shift_items x WHERE x.item_key = d.item_key) AS first_at"
               " FROM (SELECT DISTINCT ON (item_key) id, record_id, item_key, type, code, category,"
               " problem, detail, side, status, note, display, source, created_at, event_at"
               " FROM shift_items ORDER BY item_key, event_at DESC, id DESC) d")
        params = []
        if before_id:
            sql += " WHERE (d.record_id IS NULL OR d.record_id < %s)"
            params.append(before_id)
        sql += " ORDER BY d.item_key"
        c.execute(sql, params)
        rows = c.fetchall()
        release_db_connection(conn)
        conn = None
        _done = ('已處理', '已处理', '已完成', '已解決')
        pending, done = [], []
        for r in rows:
            rec = {"occ_id": r[0], "record_id": r[1], "key": r[2], "type": r[3], "code": r[4],
                   "category": r[5], "problem": r[6], "detail": r[7], "side": r[8],
                   "status": r[9] or '', "note": r[10] or '', "display": r[11] or '',
                   "source": r[12], "created_at": str(r[13]) if r[13] else None,
                   "first_at": str(r[14]) if len(r) > 14 and r[14] else None}
            if any(d in rec['status'] for d in _done):
                done.append(rec)
            else:
                pending.append(rec)
        try:
            _seen = set([x.get('key') for x in pending] + [x.get('key') for x in done])
            for m in _manual_pending(fresh=bool(request.args.get('_t'))):
                st = (m.get('status') or '待處理')
                if st == '已交更':
                    continue
                k = m.get('key')
                if not k:
                    continue
                if k in _seen:
                    # 同一 key 已有紀錄：若手動鏡像為「已處理」，標記該筆，令清單可顯示完成
                    if st != '待處理':
                        for _d in done:
                            if _d.get('key') == k:
                                _d['manual'] = True
                                _d['manual_id'] = m.get('id')
                                _d['status'] = st
                                break
                    continue
                rec = {"occ_id": None, "record_id": None, "key": k, "type": "手動",
                       "code": m.get('code') or '', "category": m.get('category') or '',
                       "problem": m.get('problem') or '', "detail": m.get('problem') or '',
                       "side": '', "status": st, "note": m.get('note') or '',
                       "display": '手動｜' + (m.get('code') or '—') + '｜' + (m.get('category') or '') + ' - ' + (m.get('problem') or ''),
                       "source": "manual", "created_at": (m.get('date') or '') + ' 00:00:00',
                       "first_at": m.get('date') or None, "manual": True, "manual_id": m.get('id')}
                _seen.add(k)
                if any(d in st for d in _done):
                    done.append(rec)
                else:
                    pending.append(rec)
        except Exception as _e:
            print('manual merge failed:', _e)
        _payload = {"pending_items": pending, "done_items": done, "count": len(pending)}
        if not request.args.get('_t'):
            cache_set(_ck, _payload, 20)
        resp = jsonify(_payload)
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print('pending_items Error:', e)
        return jsonify({"pending_items": [], "done_items": [], "count": 0, "error": str(e)})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/pending_items/complete', methods=['POST'])
def pending_item_complete():
    """喺待處理清單直接標記完成／取消完成（會寫 shift_items，並鏡像入手動清單以便交更表引用）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    key = (data.get('key') or '').strip()
    done = bool(data.get('done', True))
    if not key or '|' not in key:
        return jsonify({'status': 'error', 'message': '❌ 項目資料不正確。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT type, code, category, problem, detail, note, display,
                            to_char(MIN(event_at) AT TIME ZONE 'Asia/Macau', 'YYYY-MM-DD')
                     FROM shift_items WHERE item_key = %s GROUP BY type, code, category, problem, detail, note, display
                     ORDER BY 1 LIMIT 1""", (key,))
        row = c.fetchone()
        who = session.get('user_name') or '同事'
        if done:
            if not _si_resolve(c, None, key, who):
                return jsonify({'status': 'error', 'message': '❌ 更新失敗。'})
        else:
            _si_write_reopen(c, None, [key])
        conn.commit()
        lst = _manual_pending()
        hit = None
        for m in lst:
            if m.get('key') == key and (m.get('status') or '') != '已交更':
                hit = m
                break
        if done:
            date_str = (row[7] if row and row[7] else _up_today())
            if hit:
                hit['status'] = '已處理'
            else:
                t, code, detail = _split_key(key)
                lst.append({'id': str(int(time.time() * 1000)), 'key': key, 'date': date_str,
                            'category': (row[2] if row else ''), 'problem': (row[3] if row else ''),
                            'code': (row[1] if row else code), 'note': (row[5] if row else ''),
                            'status': '已處理', 'created_at': _up_today(), 'from_list': True})
            _manual_save(lst)
        else:
            if hit:
                hit['status'] = '待處理'
                _manual_save(lst)
        try:
            bust()
        except Exception:
            pass
        try:
            log_action('待處理清單', ('完成 ' if done else '取消完成 ') + key)
        except Exception:
            pass
        return jsonify({'status': 'success', 'message': '✅ 已標記完成。' if done else '✅ 已取消完成。'})
    except Exception as e:
        print('pending complete error:', e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'status': 'error', 'message': '❌ 操作失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/pending_items/status', methods=['POST'])
def pending_item_status():
    """待處理清單：直接把項目改成指定狀態（待處理／觀察中／已通知…／已 Send mail／已處理）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    key = (data.get('key') or '').strip()
    status = (data.get('status') or '').strip()
    mid = str(data.get('manual_id') or '')
    _allowed = ('待處理', '觀察中', '已通知相關部門跟進', '已 Send mail', '已處理')
    if status not in _allowed:
        return jsonify({'status': 'error', 'message': '❌ 狀態不正確。'})
    has_key = bool(key) and ('|' in key)
    if not has_key and not mid:
        return jsonify({'status': 'error', 'message': '❌ 缺少項目資料。'})
    conn = None
    try:
        who = session.get('user_name') or '同事'
        if has_key:
            conn = get_db_connection()
            c = conn.cursor()
            if not _si_set_status(c, None, key, status, who):
                return jsonify({'status': 'error', 'message': '❌ 更新失敗。'})
            conn.commit()
        lst = _manual_pending()
        changed = False
        for m in lst:
            if (mid and str(m.get('id')) == mid) or (has_key and m.get('key') == key and (m.get('status') or '') != '已交更'):
                m['status'] = status
                changed = True
                if mid:
                    break
        if changed:
            _manual_save(lst)
        bust()
        try:
            log_action('待處理清單', '改狀態 %s → %s' % (key or mid, status))
        except Exception:
            pass
        return jsonify({'status': 'success', 'message': '✅ 已改為「%s」' % status})
    except Exception as e:
        print('pending status error:', e)
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 操作失敗，請稍後再試。'})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/record_items/<int:rid>', methods=['GET'])
def api_record_items(rid):
    """修改紀錄時還原勾選用。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT item_key, type, code, category, problem, detail, side, status, note, display, source
                     FROM shift_items WHERE record_id = %s ORDER BY id ASC""", (rid,))
        rows = c.fetchall()
        return jsonify({"items": [{"key": r[0], "type": r[1], "code": r[2], "category": r[3],
                                   "problem": r[4], "detail": r[5], "side": r[6],
                                   "status": r[7] or '', "note": r[8] or '',
                                   "display": r[9] or '', "source": r[10]} for r in rows]})
    except Exception as e:
        print('record_items Error:', e)
        return jsonify({"items": [], "error": str(e)})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/delete_record', methods=['POST'])
def delete_record():
    if session.get('role') != 'admin':
        return jsonify({"status": "error", "message": "⚠️ 權限不足！只有管理員可以刪除紀錄。"})
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    _rid = data.get('id')
    c.execute("DELETE FROM shift_items WHERE record_id = %s", (_rid,))
    c.execute("DELETE FROM records WHERE id = %s", (_rid,))
    # 順手清走其他孤立行（若有）
    try:
        c.execute("""DELETE FROM shift_items s
                     WHERE s.record_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM records r WHERE r.id = s.record_id)""")
    except Exception as _e:
        print('delete_record cleanup 失敗:', _e)
    conn.commit()
    release_db_connection(conn)
    bust()
    log_action('刪除紀錄', f"紀錄 ID {_rid}")
    return jsonify({"status": "success", "message": "✅ 紀錄已成功刪除！"})

@app.route('/api/cleanup_orphans', methods=['POST'])
def cleanup_orphans():
    """管理員：清走 record 已刪但 shift_items 仍在嘅孤立問題項。"""
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 權限不足'}), 403
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""DELETE FROM shift_items s
                     WHERE s.record_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM records r WHERE r.id = s.record_id)""")
        n = c.rowcount
        conn.commit()
        bust()
        return jsonify({'status': 'success', 'deleted': n,
                        'message': '✅ 已清理 %d 筆孤立問題項' % n})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/record_review', methods=['POST'])
def record_review():
    """已檢閱（👍）：可多人累加／取消。
    action: add（加自己名）／remove（移除自己名）／clear（全部取消）
    """
    data = request.json or {}
    rid = data.get('id')
    by_name = (data.get('by') or '').strip()[:40]
    action = (data.get('action') or ('add' if data.get('reviewed') else 'clear')).strip()
    if action not in ('add', 'remove', 'clear'):
        action = 'add' if data.get('reviewed') else 'clear'
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到紀錄。'})
    if action in ('add', 'remove') and not by_name:
        return jsonify({'status': 'error', 'message': '❌ 請選擇檢閱人。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COALESCE(reviewed_by, '') FROM records WHERE id = %s", (rid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該紀錄！'})

        names = [n.strip() for n in (row[0] or '').split(',') if n.strip()]
        if action == 'add':
            if by_name and by_name not in names:
                names.append(by_name)
        elif action == 'remove':
            names = [n for n in names if n != by_name]
        else:
            names = []

        if names:
            c.execute("""UPDATE records SET reviewed = TRUE, reviewed_at = NOW(), reviewed_by = %s
                         WHERE id = %s""", (', '.join(names), rid))
        else:
            c.execute("""UPDATE records SET reviewed = FALSE, reviewed_at = NULL, reviewed_by = NULL
                         WHERE id = %s""", (rid,))

        c.execute("""SELECT COALESCE(reviewed, FALSE),
                            reviewed_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau',
                            COALESCE(reviewed_by, '')
                     FROM records WHERE id = %s""", (rid,))
        row = c.fetchone()
        conn.commit()
        if action == 'add':
            log_action('已檢閱', '紀錄 ID %s（%s）' % (rid, by_name))
        elif action == 'remove':
            log_action('取消檢閱', '紀錄 ID %s（%s）' % (rid, by_name))
        else:
            log_action('取消檢閱', '紀錄 ID %s（全部）' % rid)
        return jsonify({'status': 'success',
                        'reviewed': bool(row[0]) if row else False,
                        'reviewed_at': str(row[1]) if row and row[1] else '',
                        'reviewed_by': (row[2] or '') if row else ''})
    except Exception as e:
        print('Review Error:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 操作失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


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
            c.execute("SELECT NOW() - created_at < interval '20 minutes' FROM records WHERE id = %s", (data.get('id'),))
            is_within_5_mins = c.fetchone()[0]
            if not is_within_5_mins: 
                return jsonify({"status": "error", "message": "⏰ 已超過 20 分鐘修改時限，無法修改！"})
                
        c.execute("UPDATE records SET note = %s, status = %s WHERE id = %s", (data.get('note'), data.get('status'), data.get('id')))
        conn.commit()
        bust()
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
        # 保持目前這個管理員 session 有效，不會將自己登出
        session['epoch'] = get_epoch('admin', force=True)
        msg = "✅ 管理員密碼已更新（其他已登入的管理員會被強制登出）。"
    else:
        msg = "✅ 同事密碼已更新，所有已登入的同事已被強制登出（最多 10 秒內生效）。"

    log_action('更改密碼', '帳號：' + ('同事' if target == 'staff' else '管理員'))
    return jsonify({"status": "success", "message": msg})


@app.route('/api/kick_sessions', methods=['POST'])
def kick_sessions():
    """不修改密碼，直接登出所有已登入的同事（適合有人離職）。"""
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


# ---------- 更新表（任何類別，一鍵生成） ----------
# 交更表單記錄格式：  [賭檯 - Layout - 污跡] 位置: NB0201 [狀態:已 Send mail]
# 本功能把「已 Send mail」的台號抽出，按類別／問題分欄，產生主管查看的更新表。
_UP_KEY = 'update_reports'
_UP_MAPKEY = 'update_map'
_UP_EXCLKEY = 'update_excluded'
_UP_LAYOUT_COLS = [
    ('Layout', 'Stain Nearly Betting Area'),
    ('Layout', 'Stain Nearly Dealer Area'),
    ('Thin Layout', 'Thin Layout Affects Dealing'),
]
_UP_CODE_RE = re.compile(r'^([A-Z]{1,4})?(\d{3,6})$')
_UP_MAX_COLS = 12


def _up_today():
    return datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y-%m-%d')


def _up_now_text():
    return datetime.datetime.now(pytz.timezone('Asia/Macau')).strftime('%Y/%m/%d %H:%M:%S')


def _up_norm(v):
    s = '' if v is None else str(v)
    s = s.replace('\u3000', ' ').strip().upper()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[（(].*?[)）]', '', s)
    return s


def _up_is_code(s):
    return bool(_UP_CODE_RE.match(s or ''))


def _up_short_cat(cat):
    c = (cat or '').strip()
    if ' - ' in c:
        return c.split(' - ', 1)[1].strip()
    return c or '（未分類）'


def _up_mailish(status):
    s = (status or '').lower()
    return ('send' in s) and ('mail' in s)


def _up_is_layout(cat, problem):
    t = ((cat or '') + ' ' + (problem or '')).lower()
    return 'layout' in t


def _up_layout_guess(problem, side=''):
    p = (problem or '').lower()
    if '薄' in p or 'thin' in p:
        return 2
    if side == '客位':
        return 0
    if side == 'DLR位':
        return 1
    return None


def _up_take_note(line):
    """抽出行尾的「備註：…」內容。"""
    m = re.search(r'備註\s*[:：]\s*(.+)$', str(line or ''))
    return m.group(1).strip() if m else ''


def _up_parse_metric(raw):
    """解析 Lux／溫度／分貝 異常行：NB0201 (Box 1: 380) [狀態:已 Send mail]"""
    s = str(raw or '').strip()
    if not s:
        return None
    m = re.search(r'([A-Za-z]{1,4})\s*[-–]?\s*(\d{3,6})', s)
    if not m:
        return None
    code = m.group(1).upper() + m.group(2)
    if not _up_is_code(code):
        return None
    st = re.search(r'\[狀態\s*[:：]\s*([^\]]*)\]', s)
    return {'code': code, 'status': st.group(1).strip() if st else '',
            'note': _up_take_note(s)}


def _up_parse_line(line):
    """解析單行： [類別 - 問題] 位置: 台號 [狀態:xxx]"""
    if not line or '位置' not in line:
        return []
    m = re.search(r'[\[【]([^\[\]【】]+?)[\]】]\s*位置\s*[:：]\s*(.+)', line)
    if not m:
        return []
    parts = [p.strip() for p in m.group(1).split(' - ')]
    problem = parts[-1] if parts else ''
    category = ' - '.join(parts[:-1]).strip() if len(parts) > 1 else ''
    loc_text = m.group(2)
    st = re.search(r'[\[【]狀態\s*[:：]\s*([^\]】]*)[\]】]', loc_text)
    status = st.group(1).strip() if st else ''
    if st:
        loc_text = loc_text[:st.start()]
    out = []
    note_text = _up_take_note(line)
    for raw in re.split(r'[,，、;；]', loc_text):
        code = _up_norm(raw)
        if not _up_is_code(code):
            # 台號後面可能跟住描述（例：NB1713 #1位內面、DS1106 A證…），抽出前後字母＋數字
            mm = re.search(r'([A-Za-z]{1,4})\s*[-–]?\s*(\d{3,6})', raw)
            code = (mm.group(1).upper() + mm.group(2)) if mm else ''
        if code and _up_is_code(code):
            out.append({'code': code, 'category': category,
                        'problem': problem, 'status': status, 'note': note_text})
    return out


def _up_split_sections(note):
    """把備註按 【標題】 分段落。"""
    text = str(note or '')
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'【([^】]+)】', text)]
    if not marks:
        return {'': text}
    out = {}
    if marks[0][0] > 0:
        out[''] = text[:marks[0][0]]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        seg = re.sub(r'^【[^】]+】', '', text[pos:end])
        out[name] = (out.get(name, '') + '\n' + seg)
    return out


_NFC_RE = re.compile(r'NFC\s*異常\s*[:：]\s*(.*?)\s*[（(]\s*Box\s*[:：]\s*(.*?)\s*[)）]\s*(?:\[狀態\s*[:：]\s*([^\]]*)\])?')
_NFC_DONE_RE = re.compile(r'NFC\s*[-－]\s*(.*?)\s*[（(]\s*Box\s*[:：]\s*(.*?)\s*[)）]')


def _up_parse_nfc(text, done=False):
    """解析 NFC 異常： NFC異常: NB0201 (Box: Box 3) [狀態:...] / NFC - NB0201 (Box: Box 3)"""
    out = []
    if not text:
        return out
    rx = _NFC_DONE_RE if done else _NFC_RE
    for m in rx.finditer(str(text)):
        code = _up_norm(m.group(1))
        boxes = (m.group(2) or '').strip()
        status = ''
        if not done and m.lastindex and m.lastindex >= 3:
            status = (m.group(3) or '').strip()
        out.append({'code': code, 'boxes': boxes, 'status': status})
    return out


def _up_nfc_problem(boxes):
    b = (boxes or '').strip()
    if not b:
        return 'Box 未填'
    return re.sub(r'\s*[,，]\s*', ' + ', b)


def _up_parse_note(note):
    out = []
    if not note:
        return out
    for line in str(note).split('\n'):
        out.extend(_up_parse_line(line))
    return out



def _si_overlay_detect(out, date_str, done_at=None):
    """用 shift_items 的最新狀態覆寫更新表結果；只有 items 而冇 note 文字的項目亦會補上。"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT type, code, category, problem, status, COALESCE(detail, ''),
                            (event_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date
                     FROM shift_items
                     WHERE (event_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s
                     ORDER BY event_at ASC, id ASC""", (date_str,))
        rows = c.fetchall()
    except Exception as e:
        print('更新表覆寫失敗:', e)
        return
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception:
                pass

    latest = {}
    first_seen = {}
    for typ, code, cat, prob, status, detail, _d in rows:
        catname = {'NFC': 'NFC', 'Lux': 'Lux 異常',
                   '溫度': '溫度 異常', '分貝': '分貝 異常'}.get(typ, cat or '')
        if not catname:
            # 舊行 category 空白（例如直接打 API 寫 items）→ 由 detail 推導
            catname, _p = _si_guess_cat_prob({'category': cat, 'problem': prob, 'detail': detail})
            if not prob:
                prob = _p
        if not catname or not code:
            continue
        k = (catname, _up_norm(code))
        ds = _d.strftime('%Y-%m-%d') if hasattr(_d, 'strftime') else str(_d)[:10]
        latest[k] = {'status': status or '', 'problem': prob or '', 'date': ds}
        if k not in first_seen:
            first_seen[k] = ds

    # out 內的台號可能未 normalize，建索引避免重複項
    idx = {}
    for k in out:
        idx[(k[0], _up_norm(k[1]))] = k
    done_norm = set()
    for k in (done_at or {}):
        try:
            done_norm.add((k[0], _up_norm(k[1])))
        except Exception:
            pass

    _DBG['overlay_latest'] = len(latest)
    added = 0
    for k, v in latest.items():
        real = idx.get(k)
        if any(x in v['status'] for x in ('已處理', '已处理', '已完成', '已解決')):
            if real is not None:
                out.pop(real, None)
                idx.pop(k, None)
            continue
        if real is None:
            # 文字解析唔到（例如只打 API 寫 items[]、冇 note）都要出現在更新表
            if k in done_norm:
                continue
            out[k] = {'category': k[0], 'code': k[1], 'problem': v['problem'],
                      'status': v['status'], 'note': '', 'side': '',
                      'first_seen': first_seen.get(k, v['date']), 'last_seen': v['date']}
            idx[k] = k
            added += 1
        else:
            rec = out[real]
            rec['status'] = v['status']
            if v['problem']:
                rec['problem'] = v['problem']
    _DBG['overlay_added'] = added


def _up_detect(date_str):
    """截至指定日期，每個「類別＋台號」最新一次的資料。
    回傳 (out, done_at, nfc_nocode)
    """
    rows = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT COALESCE(note, ''),
                            (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date,
                            COALESCE(layout_side, ''),
                            COALESCE(checklist, ''),
                            COALESCE(lux_issue, ''),
                            COALESCE(temp_issue, ''),
                            COALESCE(db_issue, '')
                     FROM records
                     WHERE (COALESCE(note, '') <> '' OR COALESCE(checklist, '') <> '')
                       AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date <= %s
                     ORDER BY id ASC""", (date_str,))
        rows = c.fetchall()
        _DBG['rows'] = len(rows)
    except Exception as e:
        _DBG['rows_error'] = str(e)
        print('更新表讀取紀錄失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    out = {}
    sides = {}
    done_at = {}
    nfc_nocode = []

    def upsert(cat, code, problem, status, ds, note=''):
        key = (cat, code)
        rec = out.get(key)
        if rec is None:
            out[key] = {'category': cat, 'code': code, 'problem': problem, 'status': status,
                        'note': note or '',
                        'first_seen': ds, 'last_seen': ds}
        else:
            rec['problem'] = problem
            rec['status'] = status
            if note:
                rec['note'] = note
            if ds > rec['last_seen']:
                rec['last_seen'] = ds

    for row in rows:
        note = row[0]
        d = row[1]
        side_json = row[2] if len(row) > 2 else ''
        checklist = row[3] if len(row) > 3 else ''
        lux_issue = row[4] if len(row) > 4 else ''
        temp_issue = row[5] if len(row) > 5 else ''
        db_issue = row[6] if len(row) > 6 else ''
        ds = d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]
        if side_json:
            try:
                sd = json.loads(side_json)
                if isinstance(sd, dict):
                    for k, v in sd.items():
                        ck = _up_norm(k)
                        if ck and v in ('客位', 'DLR位'):
                            sides[ck] = v
            except Exception:
                pass

        # NFC 異常（存在 checklist 欄位）
        for it in _up_parse_nfc(checklist):
            if it['code']:
                upsert('NFC', it['code'], _up_nfc_problem(it['boxes']), it['status'], ds)
            else:
                nfc_nocode.append(it['status'])

        # Lux／溫度／分貝 異常（存在獨立欄位，也是異常類型）
        for raw in str(lux_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('Lux 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')
        for raw in str(temp_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('溫度 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')
        for raw in str(db_issue).split(' | '):
            it = _up_parse_metric(raw)
            if it:
                upsert('分貝 異常', it['code'], '數值異常', it['status'], ds, it.get('note') or '')

        secs = _up_split_sections(note)
        # 【早前已處理】＝已經完成，不應再出現在更新表
        # 以「類別＋台號」判斷（因為有時同事沒有填寫問題，只填台號）
        for line in secs.get('早前已處理', '').split('\n'):
            for it in _up_parse_line(line):
                done_at[(it['category'], it['code'])] = (ds, it['problem'])
            for it in _up_parse_nfc(line, done=True):
                if it['code']:
                    done_at[('NFC', it['code'])] = (ds, _up_nfc_problem(it['boxes']))
            for label, catname in (('Lux', 'Lux 異常'), ('溫度', '溫度 異常'), ('分貝', '分貝 異常')):
                if line.strip().startswith(label + ' -'):
                    it = _up_parse_metric(line)
                    if it:
                        done_at[(catname, it['code'])] = (ds, '數值異常')

        for name, seg in secs.items():
            # 「早前已處理」＝完成標記；「早前未處理」只是上一張紀錄的文字副本，
            # 原始台號仍會從原本那張紀錄抽出，所以兩段都不作新項目，避免舊台號殘留。
            if name in ('早前已處理', '早前未處理'):
                continue
            for line in seg.split('\n'):
                for it in _up_parse_line(line):
                    if not it['category']:
                        continue
                    upsert(it['category'], it['code'], it['problem'], it['status'], ds, it.get('note') or '')

    for key in list(out.keys()):
        rec = out[key]
        info = done_at.get(key)
        if info:
            dd, dprob = info
            if dd >= rec['last_seen']:
                # 若「已完成」的問題與最新上報的問題不同，不當作已完成（避免新問題被誤刪）
                if (not dprob) or (not rec['problem']) or dprob == rec['problem']:
                    out.pop(key, None)
                    continue
        rec['side'] = sides.get(rec['code'], '')

    _DBG['out_before_overlay'] = len(out)
    _DBG['done_at_size'] = len(done_at)
    # 以 shift_items 的最新狀態為準（完成／復發即時反映；items 優先於 note 文字）
    _si_overlay_detect(out, date_str, done_at)
    _DBG['out_after_overlay'] = len(out)

    return out, done_at, nfc_nocode


def _up_table_lookup():
    lut = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code, hall, pit FROM hall_tables")
        for code, hall, pit in c.fetchall():
            key = _up_norm(code)
            if key and key not in lut:
                h = str(hall or '')
                h = re.sub(r'(?i)^hall\s*', '', h).strip()
                lut[key] = {'hall': h, 'pit': str(pit)}
    except Exception as e:
        print('更新表讀取台號總表失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass
    return lut


def _up_fallback_hp(code):
    m = re.match(r'^([A-Z]{1,4})?(\d{3,6})$', code or '')
    if not m:
        return '', ''
    d = m.group(2)
    pit = d[:-2] if len(d) >= 5 else (d[:2] if len(d) == 4 else d)
    try:
        return '', str(int(pit))
    except Exception:
        return '', ''


def _up_load_json(key, default):
    raw = get_setting(key, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else default
    except Exception:
        data = default
    return data if isinstance(data, type(default)) else default


def _up_load_reports():
    return _up_load_json(_UP_KEY, {})


def _up_store_reports(data):
    try:
        keys = sorted(data.keys())
        for k in keys[:-400]:
            data.pop(k, None)
    except Exception:
        pass
    return set_setting(_UP_KEY, json.dumps(data, ensure_ascii=False))


def _up_load_map():
    return _up_load_json(_UP_MAPKEY, {})


def _up_load_excl():
    return _up_load_json(_UP_EXCLKEY, {})


def _up_key(date_str, rtype):
    return date_str + '|' + rtype


@app.route('/api/update_report_cats', methods=['GET'])
def update_report_cats():
    date_str = (request.args.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        date_str = _up_today()

    det, _done_map, _nfc_nocode = _up_detect(date_str)
    agg = {}
    layout_mail = layout_all = 0
    for (cat_key, code), e in det.items():
        cat = e['category'] or ''
        rec = agg.setdefault(cat, {'mail': 0, 'all': 0})
        rec['all'] += 1
        if _up_mailish(e['status']):
            rec['mail'] += 1
        if _up_is_layout(cat, e['problem']):
            layout_all += 1
            if _up_mailish(e['status']):
                layout_mail += 1

    cats = [{'cat': k, 'short': _up_short_cat(k), 'mail': v['mail'], 'all': v['all']}
            for k, v in agg.items() if k and not _up_is_layout(k, '')]
    cats.sort(key=lambda x: (-x['mail'], -x['all'], x['short']))
    resp = jsonify({'status': 'success', 'date': date_str, 'cats': cats,
                    'layout_mail': layout_mail, 'layout_all': layout_all})
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@app.route('/api/update_report', methods=['GET'])
def update_report_get():
    date_str = (request.args.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        date_str = _up_today()
    rtype = (request.args.get('type') or 'layout').strip()
    if not (rtype == 'layout' or rtype.startswith('cat:')):
        rtype = 'layout'
    mail_only = (request.args.get('mail_only', '1') != '0')
    is_layout = (rtype == 'layout')
    cat_name = None if is_layout else rtype[4:]

    reports = _up_load_reports()
    saved = reports.get(_up_key(date_str, rtype)) or {}

    # 已移除台號：記住「移除日期」；日後有新紀錄就自動重新顯示（不會永久隱藏）
    excl_at = {}
    for k, rep in reports.items():
        if '|' not in k:
            continue
        d, t = k.split('|', 1)
        if t != rtype or d > date_str:
            continue
        for x in (rep.get('excluded') or []):
            c = _up_norm(x)
            if c:
                excl_at[c] = max(excl_at.get(c, ''), d)

    # 上一份同類報告 → 用來判斷「新增」（紅字）
    prev_dates = []
    for k in reports.keys():
        if '|' in k:
            d, t = k.split('|', 1)
            if t == rtype and d < date_str:
                prev_dates.append(d)
    baseline = set()
    if prev_dates:
        prev = reports.get(_up_key(max(prev_dates), rtype)) or {}
        for col in (prev.get('columns') or []):
            for x in (col.get('codes') or []):
                c = _up_norm(x)
                if c:
                    baseline.add(c)

    det_all, done_map, nfc_nocode = _up_detect(date_str)
    lut = _up_table_lookup()

    def resolve_hp(code):
        """台號對應 Hall／Pit。若只輸入數字（例：0307），嘗試用結尾比對。"""
        hp = lut.get(code)
        if hp:
            return hp.get('hall', ''), hp.get('pit', '')
        if re.match(r'^\d{3,6}$', code or ''):
            hits = [lut[k] for k in lut if k.endswith(code)]
            if len(hits) == 1:
                return hits[0].get('hall', ''), hits[0].get('pit', '')
        return _up_fallback_hp(code)

    def eligible(e):
        if is_layout:
            if not _up_is_layout(e['category'], e['problem']):
                return False
        else:
            if (e['category'] or '') != cat_name:
                return False
        if mail_only and not _up_mailish(e['status']):
            return False
        return True

    det = {}
    for (cat_key, code), e in det_all.items():
        if not eligible(e):
            continue
        ed = excl_at.get(code)
        if ed and e['last_seen'] <= ed:
            continue
        det[code] = e

    # 仍然存在於紀錄內的台號（不分狀態、不分完成），用於清理舊儲存報表
    present_all = set()
    for (cat_key, code), e in det_all.items():
        if is_layout:
            if _up_is_layout(e['category'], e['problem']):
                present_all.add(code)
        elif e['category'] == cat_name:
            present_all.add(code)
    manual = set()
    for x in (saved.get('manual') or []):
        c = _up_norm(x)
        if c:
            manual.add(c)

    # 已標記完成、而且之後沒有再上報的台號（只限當前類別）
    done_only = set()
    for (c, code), _info in done_map.items():
        if is_layout:
            if not _up_is_layout(c, ''):
                continue
        else:
            if c != cat_name:
                continue
        if code not in det:
            done_only.add(code)

    nfc_missing = 0
    if (not is_layout) and cat_name == 'NFC':
        nfc_missing = sum(1 for s in nfc_nocode if (not mail_only or _up_mailish(s)))

    columns = []
    for col in (saved.get('columns') or [])[:_UP_MAX_COLS]:
        codes = []
        for x in (col.get('codes') or []):
            c = _up_norm(x)
            if not c or c in done_only:
                continue
            ed = excl_at.get(c)
            if ed and c not in det:
                continue
            # 台號已不在任何紀錄內（例如後來修正了台號）→ 自動移除；手動加入者保留
            if c not in present_all and c not in manual:
                continue
            codes.append(c)
        columns.append({'main': col.get('main') or '',
                        'sub': (col.get('sub') or '').replace('Dearly', 'Dealer'),
                        'codes': codes})
    if is_layout and not columns:
        columns = [{'main': m, 'sub': s, 'codes': []} for m, s in _UP_LAYOUT_COLS]

    code_to_col = {}
    for idx, col in enumerate(columns):
        for c in col['codes']:
            code_to_col[c] = idx

    cmap = _up_load_map().get(rtype) or {}
    pool = []
    for code, e in det.items():
        if code in code_to_col:
            continue
        target = None
        saved_key = cmap.get(code)
        if saved_key:
            saved_key = saved_key.replace('Dearly', 'Dealer')
            for idx, col in enumerate(columns):
                if (col['main'] + '||' + col['sub']) == saved_key:
                    target = idx
                    break
        if target is None and is_layout:
            target = _up_layout_guess(e['problem'], e.get('side') or '')
        if target is None and not is_layout:
            main = _up_short_cat(e['category'])
            sub = e['problem'] or '（未填問題）'
            for idx, col in enumerate(columns):
                if col['main'] == main and col['sub'] == sub:
                    target = idx
                    break
            if target is None and len(columns) < _UP_MAX_COLS:
                columns.append({'main': main, 'sub': sub, 'codes': []})
                target = len(columns) - 1
        if target is None:
            pool.append(code)
        else:
            columns[target]['codes'].append(code)

    def make_item(code):
        e = det.get(code) or {}
        hall, pit = resolve_hp(code)
        return {
            'table': code,
            'hall': hall,
            'pit': pit,
            'problem': e.get('problem') or '',
            'status': e.get('status') or '',
            'side': e.get('side') or '',
            'note': e.get('note') or '',
            'is_new': code not in baseline,
        }

    def sort_items(items):
        def k(it):
            try: h = int(it['hall'])
            except Exception: h = 9999
            try: p = int(it['pit'])
            except Exception: p = 9999
            return (h, p, it['table'])
        items.sort(key=k)

    out_cols = []
    counts = []
    total = 0
    for col in columns:
        items = [make_item(c) for c in col['codes']]
        sort_items(items)
        out_cols.append({'main': col['main'], 'sub': col['sub'], 'items': items})
        counts.append(len(items))
        total += len(items)
    pool_items = [make_item(c) for c in pool]
    sort_items(pool_items)

    resp = jsonify({
        'status': 'success',
        'date': date_str,
        'today': _up_today(),
        'type': rtype,
        'mail_only': mail_only,
        'sent': bool(saved.get('sent')),
        'has_saved': bool(saved),
        'saved_at': saved.get('updated_at') or '',
        'columns': out_cols,
        'pool': pool_items,
        'counts': counts,
        'total': total,
        'baseline': sorted(baseline),
        'excluded': sorted(excl_at),
        'nfc_missing': nfc_missing,
        'detected_count': len(det),
    })
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp


@app.route('/api/update_report/save', methods=['POST'])
def update_report_save():
    data = request.json or {}
    date_str = (data.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'status': 'error', 'message': '❌ 日期格式不正確。'})
    rtype = (data.get('type') or 'layout').strip()
    if not (rtype == 'layout' or rtype.startswith('cat:')):
        return jsonify({'status': 'error', 'message': '❌ 報表類型不正確。'})

    cols_in = data.get('columns')
    if not isinstance(cols_in, list):
        cols_in = []
    excl_in = data.get('excluded')
    if not isinstance(excl_in, list):
        excl_in = []

    clean_cols = []
    seen = set()
    for col in cols_in[:_UP_MAX_COLS]:
        if not isinstance(col, dict):
            continue
        main = (col.get('main') or '').strip()[:40]
        sub = (col.get('sub') or '').strip()[:60]
        codes = []
        src = col.get('codes')
        if not isinstance(src, list):
            src = []
        for x in src[:400]:
            c = _up_norm(x)
            if c and _up_is_code(c) and c not in seen:
                seen.add(c)
                codes.append(c)
        clean_cols.append({'main': main, 'sub': sub, 'codes': codes})

    excl = []
    for x in excl_in[:400]:
        c = _up_norm(x)
        if c and c not in seen and c not in excl:
            excl.append(c)

    # 記錄「手動加入」的台號（不在紀錄內），以免下次載入時被自動清除
    manual = []
    try:
        det_save, _dm, _nc = _up_detect(date_str)
        present = set()
        for (cat_key, code), e in det_save.items():
            if rtype == 'layout':
                if _up_is_layout(e['category'], e['problem']):
                    present.add(code)
            elif e['category'] == rtype[4:]:
                present.add(code)
        for col in clean_cols:
            for c in col['codes']:
                if c not in present and c not in manual:
                    manual.append(c)
    except Exception as e:
        print('更新表手動台號檢查失敗:', e)

    reports = _up_load_reports()
    reports[_up_key(date_str, rtype)] = {
        'date': date_str,
        'type': rtype,
        'sent': bool(data.get('sent')),
        'columns': clean_cols,
        'excluded': excl,
        'manual': manual,
        'updated_at': _up_now_text(),
    }
    if not _up_store_reports(reports):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})

    mp = _up_load_map()
    tmap = mp.get(rtype) or {}
    for col in clean_cols:
        ck = col['main'] + '||' + col['sub']
        for c in col['codes']:
            tmap[c] = ck
    for c in excl:
        tmap.pop(c, None)
    mp[rtype] = tmap
    set_setting(_UP_MAPKEY, json.dumps(mp, ensure_ascii=False))

    ex = _up_load_excl()
    elist = set(ex.get(rtype) or [])
    for c in excl:
        elist.add(c)
    for c in seen:
        elist.discard(c)
    ex[rtype] = sorted(elist)
    set_setting(_UP_EXCLKEY, json.dumps(ex, ensure_ascii=False))

    label = 'Layout' if rtype == 'layout' else rtype[4:]
    log_action('更新表', '%s｜%s（%s）共 %d 台' % (date_str, label,
                                                  '已 Send Email' if data.get('sent') else '未 Send Email',
                                                  len(seen)))
    return jsonify({'status': 'success',
                    'message': '✅ 已儲存（%s）' % ('已 Send Email' if data.get('sent') else '未 Send Email')})


# ---------- 成員管理（交更人 / 跟進人） ----------
@app.route('/api/members', methods=['GET'])
def get_members():
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, name, mtype, COALESCE(gender, ''),
                            COALESCE(account, ''), COALESCE(role, 'staff')
                     FROM members ORDER BY mtype DESC, id ASC""")
        rows = c.fetchall()
        return jsonify({'status': 'success', 'members': [
            {'id': r[0], 'name': r[1], 'mtype': r[2], 'gender': r[3],
             'account': r[4], 'role': r[5]} for r in rows]})
    except Exception as e:
        print('讀取成員失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取成員失敗。', 'members': []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/add', methods=['POST'])
def add_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以新增成員。'})
    data = request.json or {}
    name = (data.get('name') or '').strip()[:60]
    mtype = (data.get('mtype') or 'staff').strip()
    gender = (data.get('gender') or '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': '❌ 請輸入姓名。'})
    if mtype not in ('staff', 'follower'):
        mtype = 'staff'
    if gender not in ('男', '女'):
        gender = ''
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1 FROM members WHERE name = %s", (name,))
        if c.fetchone():
            return jsonify({'status': 'error', 'message': '⚠️ 該成員已存在。'})
        mm = re.search(r"[(（](\d+)[)）]", name)
        if mtype == 'staff' and mm:
            acc = mm.group(1)
            c.execute("""INSERT INTO members (name, mtype, gender, account, password, role)
                         VALUES (%s, %s, %s, %s, %s, 'staff')""",
                      (name, mtype, gender, acc, 'BS' + acc))
        elif mtype == 'follower':
            c.execute("""INSERT INTO members (name, mtype, gender, account, password, role)
                         VALUES (%s, %s, %s, %s, %s, 'staff')""",
                      (name, mtype, gender, name, name + '888'))
        else:
            c.execute("INSERT INTO members (name, mtype, gender) VALUES (%s, %s, %s)", (name, mtype, gender))
        conn.commit()
        log_action('新增成員', '%s（%s）' % (name, '交更人' if mtype == 'staff' else '跟進人'))
        return jsonify({'status': 'success', 'message': '✅ 已新增成員。'})
    except Exception as e:
        print('新增成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 新增失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/update', methods=['POST'])
def update_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改成員。'})
    data = request.json or {}
    mid = data.get('id')
    name = (data.get('name') or '').strip()[:60]
    mtype = (data.get('mtype') or 'staff').strip()
    gender = (data.get('gender') or '').strip()
    if not mid or not name:
        return jsonify({'status': 'error', 'message': '❌ 請輸入姓名。'})
    if mtype not in ('staff', 'follower'):
        mtype = 'staff'
    if gender not in ('男', '女'):
        gender = ''
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        old = c.fetchone()
        if not old:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        c.execute("SELECT 1 FROM members WHERE name = %s AND id <> %s", (name, mid))
        if c.fetchone():
            return jsonify({'status': 'error', 'message': '⚠️ 已有同名成員。'})
        c.execute("UPDATE members SET name = %s, mtype = %s, gender = %s WHERE id = %s",
                  (name, mtype, gender, mid))
        conn.commit()
        log_action('修改成員', '%s → %s' % (old[0], name))
        return jsonify({'status': 'success', 'message': '✅ 已更新成員（只影響新交更，舊紀錄不變）。'})
    except Exception as e:
        print('修改成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 修改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/set_role', methods=['POST'])
def set_member_role():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以更改權限。'})
    data = request.json or {}
    mid = data.get('id')
    role = (data.get('role') or '').strip()
    if role not in ('admin', 'staff'):
        return jsonify({'status': 'error', 'message': '❌ 權限類型不正確。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""UPDATE members SET role = %s WHERE id = %s AND COALESCE(account, '') <> ''""",
                  (role, mid))
        if c.rowcount == 0:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '❌ 該成員尚未設定帳號。'})
        conn.commit()
        log_action('更改權限', '成員 ID %s → %s' % (mid, '管理員' if role == 'admin' else '普通'))
        return jsonify({'status': 'success', 'message': '✅ 已更改為「%s」。' % ('管理員' if role == 'admin' else '普通')})
    except Exception as e:
        print('更改權限失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 更改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/reset_password', methods=['POST'])
def reset_member_password():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以重設密碼。'})
    data = request.json or {}
    mid = data.get('id')
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        mm = re.search(r"[(（](\d+)[)）]", row[0] or '')
        if mm:
            acc = mm.group(1)
            pwd = 'BS' + acc
        else:
            # 負責人：帳號＝姓名，密碼＝姓名＋888
            acc = row[0]
            pwd = row[0] + '888'
        c.execute("UPDATE members SET account = %s, password = %s WHERE id = %s", (acc, pwd, mid))
        conn.commit()
        log_action('重設密碼', row[0])
        return jsonify({'status': 'success', 'message': '✅ 密碼已重設為 %s（帳號 %s）。' % (pwd, acc),
                        'account': acc, 'password': pwd})
    except Exception as e:
        print('重設密碼失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 重設失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/members/delete', methods=['POST'])
def delete_member():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以刪除成員。'})
    data = request.json or {}
    mid = data.get('id')
    if not mid:
        return jsonify({'status': 'error', 'message': '❌ 找不到成員。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM members WHERE id = %s", (mid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該成員。'})
        c.execute("DELETE FROM members WHERE id = %s", (mid,))
        conn.commit()
        log_action('刪除成員', row[0])
        return jsonify({'status': 'success', 'message': '✅ 已刪除成員（舊紀錄不受影響）。'})
    except Exception as e:
        print('刪除成員失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 刪除失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


# ---------- 台號狀態管理 ----------
@app.route('/api/table_codes', methods=['GET'])
def api_table_codes():
    """回傳台號總表（供前端檢查台號是否存在）。任何已登入同事可用。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    _cached = cache_get('table_codes')
    if _cached is not None:
        return jsonify(_cached)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code, COALESCE(status, '營運'), COALESCE(hall::text, ''), COALESCE(pit::text, '') FROM hall_tables")
        codes, stopped, stopped_detail = [], [], []
        for code, st, hall, pit in c.fetchall():
            k = _up_norm(code)
            if not k:
                continue
            codes.append(k)
            if st == '停運':
                stopped.append(k)
                h = re.sub(r'(?i)^hall\s*', '', str(hall or '')).strip()
                stopped_detail.append({'code': k, 'hall': h, 'pit': str(pit or '')})
        try:
            stopped_detail.sort(key=lambda x: (int(x['hall']) if str(x['hall']).isdigit() else 99,
                                               int(x['pit']) if str(x['pit']).isdigit() else 9999,
                                               x['code']))
        except Exception:
            pass
        event_codes = sorted(_chair_event_tables())
        for x in stopped_detail:
            if x['code'] in event_codes:
                x['event'] = True
        _payload = {'status': 'success', 'codes': codes, 'stopped': stopped,
                    'stopped_detail': stopped_detail, 'event': event_codes,
                    'count': len(codes), 'stopped_count': len(stopped)}
        cache_set('table_codes', _payload, 60)
        resp = jsonify(_payload)
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print('table_codes Error:', e)
        return jsonify({'status': 'error', 'message': str(e), 'codes': [], 'stopped': []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/monthly_checks', methods=['GET'])
def api_monthly_checks():
    """本月設備檢查狀態：Sound Bar（每月一次）、消防（每月 15／30 日）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    _cached = cache_get('monthly_checks')
    if _cached is not None:
        return jsonify(_cached)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT
              MAX(CASE WHEN COALESCE(soundbar, '') NOT IN ('', '未檢查', '未檢測') THEN d END) AS sb,
              MAX(CASE WHEN (COALESCE(firecheck15, '') NOT IN ('', '未檢查', '未檢測')
                             OR COALESCE(firecheck, '') NOT IN ('', '未檢查', '未檢測')) THEN d END) AS f15,
              MAX(CASE WHEN COALESCE(firecheck30, '') NOT IN ('', '未檢查', '未檢測') THEN d END) AS f30,
              MAX(CASE WHEN COALESCE(wifi1, '') NOT IN ('', '未檢查', '未檢測') THEN d END) AS w1,
              MAX(CASE WHEN COALESCE(wifi15, '') NOT IN ('', '未檢查', '未檢測') THEN d END) AS w15,
              MAX(CASE WHEN COALESCE(soundbar, '') IN ('未檢查', '未檢測') THEN d END) AS sb_m,
              MAX(CASE WHEN COALESCE(firecheck15, '') IN ('未檢查', '未檢測') THEN d END) AS f15_m,
              MAX(CASE WHEN COALESCE(firecheck30, '') IN ('未檢查', '未檢測') THEN d END) AS f30_m,
              MAX(CASE WHEN COALESCE(wifi1, '') IN ('未檢查', '未檢測') THEN d END) AS w1_m,
              MAX(CASE WHEN COALESCE(wifi15, '') IN ('未檢查', '未檢測') THEN d END) AS w15_m
            FROM (
              SELECT (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Macau')::date AS d,
                     COALESCE(soundbar, '') AS soundbar,
                     COALESCE(firecheck15, '') AS firecheck15,
                     COALESCE(firecheck30, '') AS firecheck30,
                     COALESCE(wifi1, '') AS wifi1,
                     COALESCE(wifi15, '') AS wifi15,
                     COALESCE(firecheck, '') AS firecheck
              FROM records
            ) x
            WHERE date_trunc('month', x.d) = date_trunc('month', (NOW() AT TIME ZONE 'Asia/Macau')::date)
        """)
        sb, f15, f30, w1, w15, sb_m, f15_m, f30_m, w1_m, w15_m = c.fetchone()
        def pack(v, miss=None):
            return {'done': bool(v), 'date': str(v) if v else '',
                    'missed': bool(miss), 'missed_date': str(miss) if miss else ''}
        _payload = {'status': 'success',
                    'month': _up_today()[:7],
                    'today': _up_today(),
                        'soundbar': pack(sb, sb_m),
                        'fire15': pack(f15, f15_m),
                        'fire30': pack(f30, f30_m),
                    'wifi1': pack(w1, w1_m),
                    'wifi15': pack(w15, w15_m)}
        cache_set('monthly_checks', _payload, 20)
        resp = jsonify(_payload)
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        return resp
    except Exception as e:
        print('monthly_checks Error:', e)
        return jsonify({'status': 'error', 'message': str(e),
                        'soundbar': {'done': False, 'date': ''},
                        'fire15': {'done': False, 'date': ''},
                        'fire30': {'done': False, 'date': ''},
                        'wifi1': {'done': False, 'date': ''},
                        'wifi15': {'done': False, 'date': ''}})
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception:
                pass


_LAYOUT_SCHED_KEY = 'layout_schedule_done'
_LAYOUT_ROUND_KEY = 'layout_schedule_round'
_LAYOUT_HIST_KEY = 'layout_schedule_history'
_LAYOUT_COLORS_KEY = 'layout_schedule_colors'
_LAYOUT_CODE_COLORS_KEY = 'layout_schedule_code_colors'
_LAYOUT_PIT_TOTAL = 28


def _layout_sched_done():
    raw = get_setting(_LAYOUT_SCHED_KEY, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            return {str(k).upper(): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def _layout_round_no():
    try:
        return max(1, int(get_setting(_LAYOUT_ROUND_KEY, '1') or 1))
    except Exception:
        return 1


def _layout_history():
    raw = get_setting(_LAYOUT_HIST_KEY, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else []
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _layout_last_summary():
    hist = _layout_history()
    if not hist:
        return None
    last = hist[-1] or {}
    items = last.get('items') or {}
    return {
        'round': last.get('round'),
        'count': len(items),
        'total': _LAYOUT_PIT_TOTAL,
        'finished_at': last.get('finished_at') or '',
        'full': len(items) >= _LAYOUT_PIT_TOTAL,
    }


def _layout_colors_override():
    raw = get_setting(_LAYOUT_COLORS_KEY, '') or ''
    try:
        d = json.loads(raw) if raw.strip() else {}
        if isinstance(d, dict):
            return {str(k).upper(): str(v) for k, v in d.items() if str(v) in ('b', 'v', 'p', 'o')}
    except Exception:
        pass
    return {}


def _layout_code_colors():
    raw = get_setting(_LAYOUT_CODE_COLORS_KEY, '') or ''
    try:
        d = json.loads(raw) if raw.strip() else {}
        if isinstance(d, dict):
            return {str(k).upper(): str(v) for k, v in d.items() if str(v) in ('b', 'v', 'p', 'o')}
    except Exception:
        pass
    return {}


@app.route('/api/layout_colors', methods=['POST'])
def layout_colors_set():
    """手動指定某個 PIT 或台號嘅顏色（b/v/p/o），colour 為空＝還原預設。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    colour = (data.get('colour') or '').strip().lower()
    code = (data.get('code') or '').strip().upper()
    pit = (data.get('pit') or '').strip().upper()
    if colour not in ('b', 'v', 'p', 'o', ''):
        return jsonify({'status': 'error', 'message': '❌ 顏色不正確。'})
    if code:
        if not re.match(r'^[A-Z]{1,4}\d{2,6}$', code):
            return jsonify({'status': 'error', 'message': '❌ 台號格式不正確。'})
        cur = _layout_code_colors()
        if colour:
            cur[code] = colour
        else:
            cur.pop(code, None)
        if not set_setting(_LAYOUT_CODE_COLORS_KEY, json.dumps(cur, ensure_ascii=False)):
            return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
        try:
            log_action('Layout 顏色', '%s → %s' % (code, colour or '預設'))
        except Exception:
            pass
        cache_clear('layout_schedule', 'chair_map')
        return jsonify({'status': 'success', 'code_colors': cur, 'colors': _layout_colors_override(),
                        'message': ('✅ %s 顏色已手動設定。' % code) if colour else ('✅ %s 已還原預設顏色。' % code)})
    if not re.match(r'^PIT\d{1,3}$', pit):
        return jsonify({'status': 'error', 'message': '❌ 請提供台號或 PIT 編號。'})
    cur = _layout_colors_override()
    if colour:
        cur[pit] = colour
    else:
        cur.pop(pit, None)
    if not set_setting(_LAYOUT_COLORS_KEY, json.dumps(cur, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    try:
        log_action('Layout 顏色', '%s → %s' % (pit, colour or '預設'))
    except Exception:
        pass
    cache_clear('layout_schedule', 'chair_map')
    return jsonify({'status': 'success', 'colors': cur, 'code_colors': _layout_code_colors(),
                    'message': ('✅ %s 顏色已改為手動設定。' % pit) if colour else ('✅ %s 已還原預設顏色。' % pit)})


@app.route('/api/layout_schedule', methods=['GET'])
def layout_schedule_get():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    _cached = cache_get('layout_schedule')
    if _cached is not None:
        return jsonify(_cached)
    _payload = {'status': 'success', 'done': _layout_sched_done(), 'today': _up_today(),
                'round': _layout_round_no(), 'last': _layout_last_summary(),
                'rounds_done': len(_layout_history()), 'total_pits': _LAYOUT_PIT_TOTAL,
                'colors': _layout_colors_override(), 'code_colors': _layout_code_colors()}
    cache_set('layout_schedule', _payload, 20)
    return jsonify(_payload)


@app.route('/api/layout_schedule/reset', methods=['POST'])
def layout_schedule_reset():
    """完成一輪 → 存檔舊一輪（保留日期）並開始新一輪；不影響交更紀錄同主頁紀錄卡。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    done = _layout_sched_done()
    if not done:
        return jsonify({'status': 'error', 'message': '本輪尚未有任何完成記錄，無需開始新一輪。'})
    rnd = _layout_round_no()
    hist = _layout_history()
    hist.append({'round': rnd, 'items': done, 'finished_at': _up_today(), 'count': len(done)})
    hist = hist[-30:]
    if not set_setting(_LAYOUT_HIST_KEY, json.dumps(hist, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    if not set_setting(_LAYOUT_SCHED_KEY, '{}'):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    set_setting(_LAYOUT_ROUND_KEY, str(rnd + 1))
    try:
        log_action('Layout 更換', '第 %d 輪完成（%d／%d），開始第 %d 輪' % (rnd, len(done), _LAYOUT_PIT_TOTAL, rnd + 1))
    except Exception:
        pass
    cache_clear('layout_schedule')
    return jsonify({'status': 'success', 'round': rnd + 1, 'done': {}, 'last': _layout_last_summary(),
                    'message': '✅ 第 %d 輪已存檔（%d 個 PIT），已開始第 %d 輪。' % (rnd, len(done), rnd + 1)})


@app.route('/api/layout_schedule/undo', methods=['POST'])
def layout_schedule_undo():
    """還原上一次「開始新一輪」（找回舊一輪的完成日期）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    hist = _layout_history()
    if not hist:
        return jsonify({'status': 'error', 'message': '沒有可還原嘅上一輪。'})
    last = hist.pop() or {}
    items = last.get('items') or {}
    if not set_setting(_LAYOUT_HIST_KEY, json.dumps(hist, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    if not set_setting(_LAYOUT_SCHED_KEY, json.dumps(items, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    rnd = last.get('round') or (len(hist) + 1)
    set_setting(_LAYOUT_ROUND_KEY, str(rnd))
    try:
        log_action('Layout 更換', '還原第 %d 輪（%d 個 PIT）' % (rnd, len(items)))
    except Exception:
        pass
    cache_clear('layout_schedule')
    return jsonify({'status': 'success', 'round': rnd, 'done': items, 'last': _layout_last_summary(),
                    'message': '✅ 已還原第 %d 輪（%d 個 PIT）。' % (rnd, len(items))})


@app.route('/api/layout_schedule', methods=['POST'])
def layout_schedule_set():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    pit = (data.get('pit') or '').strip().upper()
    if not re.match(r'^PIT\d{1,3}$', pit):
        return jsonify({'status': 'error', 'message': '❌ PIT 編號不正確。'})
    done = _layout_sched_done()
    if data.get('done'):
        use_date = _up_today()
        raw_date = (data.get('date') or '').strip()
        if raw_date:
            try:
                datetime.datetime.strptime(raw_date, '%Y-%m-%d')
                use_date = raw_date
            except Exception:
                return jsonify({'status': 'error', 'message': '❌ 日期格式必須係 YYYY-MM-DD。'})
        done[pit] = use_date
        msg = '✅ %s 已標記更換完成（%s）' % (pit, done[pit])
    else:
        done.pop(pit, None)
        msg = '✅ 已取消 %s 的更換完成標記' % pit
    if not set_setting(_LAYOUT_SCHED_KEY, json.dumps(done, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    try:
        log_action('Layout 更換', ('完成 ' if data.get('done') else '取消 ') + pit)
    except Exception:
        pass
    cache_clear('layout_schedule')
    return jsonify({'status': 'success', 'done': done, 'message': msg})


@app.route('/api/hall_tables', methods=['GET'])
def list_hall_tables():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以使用。'})
    q = (request.args.get('q') or '').strip().upper()
    hall = (request.args.get('hall') or '').strip()
    pit = (request.args.get('pit') or '').strip()

    where = []
    params = []
    if q:
        where.append("UPPER(table_code) LIKE %s")
        params.append('%' + q + '%')
    if hall:
        where.append("hall = %s")
        params.append(hall)
    if pit:
        where.append("pit = %s")
        params.append(pit)
    sql_where = (' WHERE ' + ' AND '.join(where)) if where else ''

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM hall_tables" + sql_where, params)
        total = c.fetchone()[0]
        c.execute("""SELECT id, hall, pit, table_code, status
                     FROM hall_tables""" + sql_where + " ORDER BY hall, pit, table_code LIMIT 300", params)
        rows = c.fetchall()
        return jsonify({'status': 'success', 'total': total, 'rows': [
            {'id': r[0], 'hall': r[1], 'pit': r[2], 'table_code': r[3], 'status': r[4]} for r in rows]})
    except Exception as e:
        print('讀取台號總表失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取失敗。', 'rows': []})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/summary', methods=['GET'])
def hall_tables_summary():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以使用。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT hall, pit, COUNT(*),
                            COUNT(*) FILTER (WHERE status = '營運'),
                            COUNT(*) FILTER (WHERE status = '停運')
                     FROM hall_tables
                     GROUP BY hall, pit
                     ORDER BY hall, pit""")
        rows = c.fetchall()
        c.execute("""SELECT UPPER(REGEXP_REPLACE(table_code, '[0-9]+$', '')) AS prefix,
                            COUNT(*),
                            COUNT(*) FILTER (WHERE status = '營運'),
                            COUNT(*) FILTER (WHERE status = '停運')
                     FROM hall_tables
                     GROUP BY 1
                     ORDER BY 2 DESC""")
        type_rows = c.fetchall()
        types = [{'type': (t or '其他'), 'total': int(n), 'active': int(a or 0), 'inactive': int(d or 0)}
                 for t, n, a, d in type_rows]
        halls = []
        by_hall = {}
        total = {'total': 0, 'active': 0, 'inactive': 0}
        for hall, pit, n, act, dea in rows:
            n, act, dea = int(n), int(act or 0), int(dea or 0)
            if hall not in by_hall:
                by_hall[hall] = {'hall': hall, 'total': 0, 'active': 0, 'inactive': 0, 'pits': []}
                halls.append(by_hall[hall])
            h = by_hall[hall]
            h['total'] += n
            h['active'] += act
            h['inactive'] += dea
            h['pits'].append({'pit': pit, 'total': n, 'active': act, 'inactive': dea})
            total['total'] += n
            total['active'] += act
            total['inactive'] += dea
        return jsonify({'status': 'success', 'halls': halls, 'types': types, 'total': total})
    except Exception as e:
        print('台號統計失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ 讀取統計失敗。', 'halls': [], 'total': {}})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/add', methods=['POST'])
def add_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以新增台號。'})
    data = request.json or {}
    hall = (data.get('hall') or '').strip()
    code = (data.get('table_code') or '').strip().upper()
    status = (data.get('status') or '營運').strip()
    try:
        pit = int(data.get('pit'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ Pit 必須是數字。'})
    if not hall or not code:
        return jsonify({'status': 'error', 'message': '❌ Hall 及台號必須填寫。'})
    if status not in ('營運', '停運'):
        status = '營運'

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO hall_tables (hall, pit, table_code, status)
                     VALUES (%s, %s, %s, %s) ON CONFLICT (hall, pit, table_code) DO NOTHING RETURNING id""",
                  (hall, pit, code, status))
        row = c.fetchone()
        if not row:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '⚠️ 該 Hall／Pit／台號 已存在。'})
        conn.commit()
        log_action('新增台號', '%s / Pit %s / %s' % (hall, pit, code))
        bust()
        return jsonify({'status': 'success', 'message': '✅ 已新增台號 %s。' % code})
    except Exception as e:
        print('新增台號失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 新增失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/update', methods=['POST'])
def update_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改台號。'})
    data = request.json or {}
    rid = data.get('id')
    hall = (data.get('hall') or '').strip()
    code = (data.get('table_code') or '').strip().upper()
    status = (data.get('status') or '').strip()
    try:
        pit = int(data.get('pit'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ Pit 必須是數字。'})
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到台號。'})
    if not hall or not code:
        return jsonify({'status': 'error', 'message': '❌ Hall 及台號必須填寫。'})
    if status not in ('營運', '停運'):
        status = '營運'

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""UPDATE hall_tables SET hall = %s, pit = %s, table_code = %s, status = %s, updated_at = NOW()
                     WHERE id = %s""", (hall, pit, code, status, rid))
        if c.rowcount == 0:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '❌ 找不到該台號。'})
        conn.commit()
        log_action('修改台號', '%s / Pit %s / %s（%s）' % (hall, pit, code, status))
        bust()
        return jsonify({'status': 'success', 'message': '✅ 已更新台號。'})
    except Exception as e:
        print('修改台號失敗:', e)
        if conn: conn.rollback()
        if 'duplicate key' in str(e):
            return jsonify({'status': 'error', 'message': '⚠️ 該 Hall／Pit／台號 組合已存在。'})
        return jsonify({'status': 'error', 'message': '❌ 修改失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/delete', methods=['POST'])
def delete_hall_table():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以刪除台號。'})
    data = request.json or {}
    rid = data.get('id')
    if not rid:
        return jsonify({'status': 'error', 'message': '❌ 找不到台號。'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code FROM hall_tables WHERE id = %s", (rid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該台號。'})
        c.execute("DELETE FROM hall_tables WHERE id = %s", (rid,))
        conn.commit()
        log_action('刪除台號', row[0])
        bust()
        return jsonify({'status': 'success', 'message': '✅ 已刪除台號 %s。' % row[0]})
    except Exception as e:
        print('刪除台號失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 刪除失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


@app.route('/api/hall_tables/status', methods=['POST'])
def set_hall_status():
    if session.get('role') != 'admin':
        return jsonify({'status': 'error', 'message': '⚠️ 只有管理員可以修改。'})
    data = request.json or {}
    status = (data.get('status') or '').strip()
    ids = data.get('ids')
    if not isinstance(ids, list):
        ids = [data.get('id')] if data.get('id') else []
    ids = [i for i in ids if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
    if status not in ('營運', '停運'):
        return jsonify({'status': 'error', 'message': '❌ 狀態不正確。'})
    if not ids:
        return jsonify({'status': 'error', 'message': '❌ 沒有選取台號。'})

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE hall_tables SET status = %s, updated_at = NOW() WHERE id = ANY(%s)",
                  (status, [int(i) for i in ids]))
        updated = c.rowcount
        conn.commit()
        log_action('修改台號狀態', '%d 個台號 → %s' % (updated, status))
        bust()
        return jsonify({'status': 'success', 'message': '✅ 已更新 %d 個台號為「%s」。' % (updated, status),
                        'updated': updated})
    except Exception as e:
        print('修改台號狀態失敗:', e)
        if conn: conn.rollback()
        return jsonify({'status': 'error', 'message': '❌ 更新失敗，請稍後再試。'})
    finally:
        if conn:
            release_db_connection(conn)


_CHAIR_TYPE_KEY = 'chair_types'
_CHAIR_EDIT_KEY = 'chair_edits'
_CHAIR_EVENT_KEY = 'chair_event_tables'


def _chair_event_tables():
    """活動台名單（唔計入台數/DLR 數量，地圖粉色顯示）。"""
    raw = get_setting(_CHAIR_EVENT_KEY, '') or ''
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return set(_up_norm(x) for x in data if x)
    except Exception:
        pass
    return set()
_CHAIR_LAYOUT_CACHE = None


_CHAIR_LAYOUT_KEY = 'chair_layout'
_BORDER_THICK = {'medium', 'thick', 'double', 'mediumDashDot', 'mediumDashDotDot', 'slantDashDot'}
_BORDER_THIN = {'thin', 'hair', 'dotted', 'dashed', 'dashDot', 'dashDotDot'}


_CHAIR_STATIC_CACHE = None


def _chair_static_layout():
    global _CHAIR_STATIC_CACHE
    if _CHAIR_STATIC_CACHE is None:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
        path = os.path.join(base, 'chair_grid.json')
        if not os.path.exists(path):
            path = os.path.join(base, 'chair_map.json')
        with open(path, 'r', encoding='utf-8') as f:
            _CHAIR_STATIC_CACHE = json.load(f)
    return _CHAIR_STATIC_CACHE


def _chair_layout():
    """版面：優先用手機上傳（存資料庫），否則用 static。每都讀 DB，上傳即時生效。"""
    raw = get_setting(_CHAIR_LAYOUT_KEY, '') or ''
    if raw.strip():
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get('cells'):
                return d
        except Exception as e:
            print('資料庫版面讀取失敗:', e)
    try:
        return _chair_static_layout()
    except Exception as e:
        print('櫈況圖版面讀取失敗:', e)
        return {'maxR': 0, 'maxC': 0, 'pits': [], 'cells': [], 'extras': []}


def _chair_base_codes():
    """以 static 原始版面的台號位置作基準（上傳 Excel 時套用）。"""
    m = {}
    try:
        lay = _chair_static_layout()
        for c in lay.get('cells', []):
            code = c.get('code')
            if code:
                m['%d,%d' % (int(c.get('r')), int(c.get('c')))] = code
    except Exception as e:
        print('台號基準讀取失敗:', e)
    return m


def _chair_border_val(style):
    if style in _BORDER_THICK:
        return 2
    if style in _BORDER_THIN:
        return 1
    return 0


def _chair_color(c):
    try:
        rgb = c.rgb if c is not None else None
        if isinstance(rgb, str) and len(rgb) == 8:
            if rgb == '00000000':
                return ''
            return '#' + rgb[2:]
    except Exception:
        pass
    return ''


def _chair_run_ranges(vals):
    vals = sorted(set(vals))
    out = []
    if not vals:
        return out
    start = prev = vals[0]
    for v in vals[1:]:
        if v == prev + 1:
            prev = v
        else:
            out.append((start, prev))
            start = prev = v
    out.append((start, prev))
    return out


def _xlsx_apply_layout(data, base_layout):
    """用 Excel 更新現有版面的框線、文字、顏色（不改位置，唔會走位）。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.worksheets[0]
    offset = 6

    merged_set = set()
    for rng in ws.merged_cells.ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged_set.add((r, c))

    style = {}
    for row in ws.iter_rows():
        if row and row[0].row > 200:
            break
        for cell in row:
            if cell.column > 160:
                continue
            if (cell.row, cell.column) in merged_set:
                continue
            f = cell.font
            if f.size is not None and float(f.size) > 16:
                continue
            val = cell.value
            bg = _chair_color(cell.fill.fgColor if cell.fill else None)
            b = cell.border
            t = _chair_border_val(b.top.style if b.top else None)
            l = _chair_border_val(b.left.style if b.left else None)
            rr = _chair_border_val(b.right.style if b.right else None)
            bb = _chair_border_val(b.bottom.style if b.bottom else None)
            if not ((val not in (None, '')) or bg or (t + l + rr + bb)):
                continue
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            style[(cell.row + offset, cell.column)] = {
                'v': '' if val is None else str(val),
                't': t, 'l': l, 'rr': rr, 'b': bb,
                'fc': _chair_color(f.color) or '#000000',
                'fs': float(f.size or 11),
                'bold': bool(f.bold),
                'bg': bg
            }

    merges = {}
    for rng in ws.merged_cells.ranges:
        tl = ws.cell(row=rng.min_row, column=rng.min_col)
        val = tl.value
        if val is None or str(val).strip() == '':
            continue
        f = tl.font
        if f.size is not None and float(f.size) > 16:
            continue
        b = tl.border
        merges[(rng.min_row + offset, rng.min_col)] = {
            'rs': rng.max_row - rng.min_row + 1,
            'cs': rng.max_col - rng.min_col + 1,
            'v': str(val),
            'fc': _chair_color(f.color) or '',
            'fs': float(f.size or 11),
            'bold': bool(f.bold),
            'bg': _chair_color(tl.fill.fgColor if tl.fill else None),
            't': _chair_border_val(b.top.style if b.top else None),
            'l': _chair_border_val(b.left.style if b.left else None),
            'rr': _chair_border_val(b.right.style if b.right else None),
            'b': _chair_border_val(b.bottom.style if b.bottom else None)
        }

    out = json.loads(json.dumps(base_layout))
    hit = 0
    for c in out.get('cells', []):
        s = style.get((int(c.get('r')), int(c.get('c'))))
        if not s:
            continue
        for k in ('v', 't', 'l', 'rr', 'b', 'fc', 'fs', 'bold', 'bg'):
            if k == 'v' and not s['v'] and c.get('code'):
                continue
            c[k] = s[k]
        hit += 1
    mhit = 0
    for m in out.get('merges', []):
        s = merges.get((int(m.get('r')), int(m.get('c'))))
        if not s:
            continue
        for k in ('v', 'fc', 'bg', 'fs', 'bold', 't', 'l', 'rr', 'b', 'rs', 'cs'):
            if k in s:
                m[k] = s[k]
        mhit += 1
    return out, hit, mhit


def _xlsx_build_layout(data, code_map, base_layout=None, row_offset=None):
    """完全按 Excel 重建版面（框線、合併、文字、位置），再套用台號。
    row_offset 為 None 時，自動揀最匹配台號嘅 Sheet 同偏移。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    if row_offset is None:
        keys = []
        for k in code_map:
            a, b = k.split(',')
            keys.append((int(a), int(b)))
        best = (0, None, 0)
        for cand in wb.worksheets:
            for off in range(0, 10):
                hit = 0
                for (kr, kc) in keys:
                    er = kr - off
                    if er < 1:
                        continue
                    try:
                        v = cand.cell(row=er, column=kc).value
                    except Exception:
                        continue
                    if v not in (None, ''):
                        hit += 1
                if hit > best[0]:
                    best = (hit, cand, off)
        ws = best[1] or wb.worksheets[0]
        row_offset = best[2]
    else:
        ws = wb.worksheets[0]
    merged_ranges = list(ws.merged_cells.ranges)
    merged_set = set()
    for rng in merged_ranges:
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged_set.add((r, c))

    cells = []
    hset = {}
    vset = {}

    for row in ws.iter_rows():
        if row and row[0].row > 200:
            break
        for cell in row:
            if cell.column > 160:
                continue
            if (cell.row, cell.column) in merged_set:
                continue
            f = cell.font
            if f.size is not None and float(f.size) > 40:
                continue
            val = cell.value
            if isinstance(val, str) and ('Chair' in val or 'Update' in val):
                continue
            bg = _chair_color(cell.fill.fgColor if cell.fill else None)
            b = cell.border
            t = _chair_border_val(b.top.style if b.top else None)
            l = _chair_border_val(b.left.style if b.left else None)
            rr = _chair_border_val(b.right.style if b.right else None)
            bb = _chair_border_val(b.bottom.style if b.bottom else None)
            if not ((val not in (None, '')) or bg or (t + l + rr + bb)):
                continue
            mr, mc = cell.row + row_offset, cell.column
            if mr < 1 or mc < 1:
                continue
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            cells.append({
                'r': mr, 'c': mc,
                'v': '' if val is None else str(val),
                'code': code_map.get('%d,%d' % (mr, mc), ''),
                'num': None,
                't': t, 'l': l, 'rr': rr, 'b': bb,
                'fc': _chair_color(f.color) or '#000000',
                'fs': float(f.size or 11),
                'bold': bool(f.bold),
                'bg': bg
            })

    merges = []
    for rng in merged_ranges:
        tl = ws.cell(row=rng.min_row, column=rng.min_col)
        val = tl.value
        if val is None or str(val).strip() == '':
            continue
        if str(val).strip().isdigit():
            continue
        f = tl.font
        if f.size is not None and float(f.size) > 40:
            continue
        if 'Chair' in str(val) or 'Update' in str(val):
            continue
        b = tl.border
        merges.append({
            'r': rng.min_row + row_offset, 'c': rng.min_col,
            'rs': rng.max_row - rng.min_row + 1,
            'cs': rng.max_col - rng.min_col + 1,
            'v': str(val),
            'fc': _chair_color(f.color) or '',
            'fs': float(f.size or 11),
            'bold': bool(f.bold),
            'bg': _chair_color(tl.fill.fgColor if tl.fill else None),
            't': _chair_border_val(b.top.style if b.top else None),
            'l': _chair_border_val(b.left.style if b.left else None),
            'rr': _chair_border_val(b.right.style if b.right else None),
            'b': _chair_border_val(b.bottom.style if b.bottom else None)
        })

    # 補回缺失的台號格（用原始版面），確保 611 台齊全
    if base_layout:
        seen = set('%d,%d' % (c['r'], c['c']) for c in cells)
        for bc in base_layout.get('cells', []):
            if not bc.get('code'):
                continue
            k = '%d,%d' % (bc['r'], bc['c'])
            if k in seen:
                continue
            cells.append(dict(bc))
            seen.add(k)

    # Pit 63 / Pit 31 下方嘅數字放大
    for c in cells:
        if (24 <= c['r'] <= 28 and 36 <= c['c'] <= 42) or (45 <= c['r'] <= 49 and 62 <= c['c'] <= 68):
            if c.get('code'):
                c['fs'] = 18
                c['bold'] = True
        if str(c.get('v') or '').strip().replace(' ', '') == 'Pit27':
            c['fc'] = '#FF0000'
            c['fs'] = 16
            c['bold'] = True

    # 收集所有邊（1=細，2=粗），畫齊所有框
    for c in cells:
        mr, mc = int(c['r']), int(c['c'])
        if c.get('t'):
            hset.setdefault(mr, {})[mc] = c['t']
        if c.get('b'):
            hset.setdefault(mr + 1, {})[mc] = c['b']
        if c.get('l'):
            vset.setdefault(mc, {})[mr] = c['l']
        if c.get('rr'):
            vset.setdefault(mc + 1, {})[mr] = c['rr']

    # 合併格嘅外框（例如 Pit 26/33 大框頂邊）
    for m in merges:
        mr, mc = int(m['r']), int(m['c'])
        rs, cs = int(m.get('rs') or 1), int(m.get('cs') or 1)
        if m.get('t'):
            for i in range(cs):
                hset.setdefault(mr, {})[mc + i] = m['t']
        if m.get('b'):
            for i in range(cs):
                hset.setdefault(mr + rs, {})[mc + i] = m['b']
        if m.get('l'):
            for i in range(rs):
                vset.setdefault(mc, {})[mr + i] = m['l']
        if m.get('rr'):
            for i in range(rs):
                vset.setdefault(mc + cs, {})[mr + i] = m['rr']

    # 掃描整張 Excel 所有粗框線（包括合併區內），確保一條都唔漏
    row_off = row_offset
    max_r = min(ws.max_row, 200)
    max_c = min(ws.max_column, 160)
    for er in range(1, max_r + 1):
        for ec in range(1, max_c + 1):
            cell = ws.cell(row=er, column=ec)
            b = cell.border
            if b.top and b.top.style in ('medium', 'thick'):
                hset.setdefault(er + row_off, {})[ec] = 2
            if b.bottom and b.bottom.style in ('medium', 'thick'):
                hset.setdefault(er + 1 + row_off, {})[ec] = 2
            if b.left and b.left.style in ('medium', 'thick'):
                vset.setdefault(ec, {})[er + row_off] = 2
            if b.right and b.right.style in ('medium', 'thick'):
                vset.setdefault(ec + 1, {})[er + row_off] = 2

    rs = [int(c['r']) for c in cells]
    cs = [int(c['c']) for c in cells]
    if rs:
        minr, maxr, minc, maxc = min(rs), max(rs), min(cs), max(cs)
    else:
        minr, maxr, minc, maxc = 1, 1, 1, 1

    cols = []
    from openpyxl.utils import column_index_from_string
    # Pit 63 / Pit 31 下面 4 格所在嘅欄平均闊度
    widen = set()
    for k, dim in ws.column_dimensions.items():
        try:
            idx = column_index_from_string(k)
        except Exception:
            continue
        if not dim.width:
            continue
        lo = getattr(dim, 'min', None) or idx
        hi = getattr(dim, 'max', None) or idx
        if hi > 200:
            hi = idx
        for c in range(int(lo), int(hi) + 1):
            w = 6.0 if c in widen else round(float(dim.width), 3)
            if not any(x['c'] == c for x in cols):
                cols.append({'c': c, 'w': w})
    _have = set(x['c'] for x in cols)
    for c in widen:
        if c not in _have:
            cols.append({'c': c, 'w': 6.0})

    rows = []
    for k, dim in ws.row_dimensions.items():
        if not dim.height:
            continue
        lo = getattr(dim, 'min', None) or int(k)
        hi = getattr(dim, 'max', None) or int(k)
        if hi > 200:
            hi = int(k)
        for r in range(int(lo), int(hi) + 1):
            rows.append({'r': r + row_offset, 'h': round(float(dim.height), 3)})

    def _runs_with_w(d):
        vals = sorted(d.keys())
        out = []
        if not vals:
            return out
        start = prev = vals[0]
        w = d[start]
        for v in vals[1:]:
            if v == prev + 1:
                prev = v
                if d[v] > w:
                    w = d[v]
            else:
                out.append((start, prev, w))
                start = prev = v
                w = d[v]
        out.append((start, prev, w))
        return out

    hlines = []
    for r, d in hset.items():
        for a, z, w in _runs_with_w(d):
            hlines.append({'r': r, 'c1': a, 'c2': z, 'w': w})
    vlines = []
    for c, d in vset.items():
        for a, z, w in _runs_with_w(d):
            vlines.append({'c': c, 'r1': a, 'r2': z, 'w': w})

    return {
        'maxR': maxr, 'maxC': maxc, 'minR': minr, 'minC': minc,
        'maxRu': maxr, 'maxCu': maxc,
        'cols': cols, 'rows': rows,
        'cells': cells, 'merges': merges,
        'hlines': hlines, 'vlines': vlines, 'extras': []
    }


def _chair_types():
    base = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'chair_types_seed.json')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                seed = json.load(f)
            if isinstance(seed, dict):
                base = seed
    except Exception as e:
        print('新舊櫈預設讀取失敗:', e)
    raw = get_setting(_CHAIR_TYPE_KEY, '') or ''
    try:
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            base.update(data)
    except Exception:
        pass
    return base


def _chair_edits():
    """櫈況圖手動編輯覆蓋層（存 app_settings['chair_edits']）。
    只有管理員改過的格才有記錄；冇記錄的格完全照自動規則。"""
    empty = {'cells': {}, 'merges': [], 'unmerge': []}
    raw = get_setting(_CHAIR_EDIT_KEY, '') or ''
    if not raw.strip():
        return empty
    try:
        data = json.loads(raw)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    out = {'cells': {}, 'merges': [], 'unmerge': []}
    cells = data.get('cells')
    if isinstance(cells, dict):
        for k, v in cells.items():
            if isinstance(v, dict) and re.match(r'^\d+,\d+$', str(k)):
                out['cells'][str(k)] = v
    merges = data.get('merges')
    if isinstance(merges, list):
        for m in merges:
            if isinstance(m, dict) and m.get('r') and m.get('c'):
                out['merges'].append(m)
    unmerge = data.get('unmerge')
    if isinstance(unmerge, list):
        out['unmerge'] = [str(x) for x in unmerge if re.match(r'^\d+,\d+$', str(x))]
    return out



def _chair_issues():
    """由 shift_items 推算每張台的最新椅況（已處理不計；復發會自動回復）。"""
    out = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT DISTINCT ON (item_key) item_key, code, category, status, detail
                     FROM shift_items
                     WHERE (type = '場面' OR item_key LIKE '場面|%')
                     ORDER BY item_key, event_at DESC, id DESC""")
        rows = c.fetchall()
    except Exception as e:
        print('櫈況圖讀取 shift_items 失敗:', e)
        rows = []
    finally:
        if conn:
            try:
                release_db_connection(conn)
            except Exception:
                pass

    for _k, code, cat, status, detail in rows:
        # category 有時會空白（舊資料／直接寫 API），所以一併用 detail 同 item_key 判斷
        hay = ' '.join([str(cat or ''), str(detail or ''), str(_k or '')])
        is_dlr = ('DLR' in hay.upper() and ('櫈' in hay or '椅' in hay))
        is_guest = ('客櫈' in hay or '客椅' in hay)
        if not (is_dlr or is_guest):
            continue
        if not code:
            continue
        key = _up_norm(code)
        rec = out.setdefault(key, {'dlr_bad': False, 'guest_bad': False})
        st_ = status or ''
        bad = not any(x in st_ for x in ('已處理', '已处理', '已完成', '已解決'))
        # 一張台可能有多個櫈問題（例如客櫈-坐墊破損 未解決 + 客櫈-螺絲鬆動 已處理）：
        # 必須用 OR 累加，唔可以後寫覆蓋前寫（以前會令未解決問題被蓋成正常）
        if is_dlr and bad:
            rec['dlr_bad'] = True
        if is_guest and bad:
            rec['guest_bad'] = True

    if not out:
        # 萬一 shift_items 未有資料，退回舊文字解析
        return _chair_issues_text()
    return out


def _chair_issues_text():
    """（後備）由交更紀錄文字推算每張台的椅況。"""
    out = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT note FROM records WHERE (note LIKE '%客櫈%' OR note LIKE '%DLR櫈%' OR note LIKE '%客椅%' OR note LIKE '%【場面問題】%') ORDER BY id ASC")
        rows = c.fetchall()
    except Exception as e:
        print('櫈況圖讀取紀錄失敗:', e)
        rows = []
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    tmp = {}
    for (note,) in rows:
        text = str(note or '')
        done_pos = -1
        dm = re.search(r'【早前已處理】', text)
        if dm:
            done_pos = dm.start()
        pos = 0
        for line in text.split('\n'):
            in_done = done_pos >= 0 and pos > done_pos
            pos += len(line) + 1
            for it in _up_parse_line(line):
                cat = it.get('category') or ''
                is_dlr = ('DLR' in cat and ('櫈' in cat or '椅' in cat))
                is_guest = ('客櫈' in cat or '客椅' in cat)
                if not (is_dlr or is_guest):
                    continue
                key = _up_norm(it['code'])
                st_ = it.get('status') or ''
                if in_done:
                    bad = False
                else:
                    bad = not (('已處理' in st_) or ('已处理' in st_) or ('已完成' in st_) or ('已解決' in st_))
                # 按「台號＋類別＋問題」保存最新狀態，避免同一張台唔同問題互相覆蓋
                tmp[(key, cat, it.get('problem') or it.get('detail') or '')] = (is_dlr, is_guest, bad)
    out = {}
    for (key, _cat, _p), (is_dlr, is_guest, bad) in tmp.items():
        rec = out.setdefault(key, {'dlr_bad': False, 'guest_bad': False})
        if is_dlr and bad:
            rec['dlr_bad'] = True
        if is_guest and bad:
            rec['guest_bad'] = True
    return out


@app.route('/api/chair_map', methods=['GET'])
def chair_map_get():
    """櫈況圖：回傳每張台的椅況（停運／好／壞 DLR／壞客櫈）及新舊櫈設定。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401

    _cached = None if request.args.get('_t') else cache_get('chair_map')   # 帶 _t＝強制讀最新（避免多 worker 快取未同步）
    if _cached is not None:
        return jsonify(_cached)
    layout = _chair_layout()
    hall_status = {}
    hall_pit = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT table_code, status, pit FROM hall_tables")
        for code, st, pit in c.fetchall():
            hall_status[_up_norm(code)] = st
            hall_pit[_up_norm(code)] = pit
    except Exception as e:
        print('櫈況圖讀取台號失敗:', e)
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass

    issues = _chair_issues()
    types = _chair_types()
    event_codes = _chair_event_tables()

    cells = []
    for cell in layout.get('cells', []):
        code = cell.get('code') or ''
        key = _up_norm(code)
        st = hall_status.get(key, '營運') if key else ''
        iss = issues.get(key, {}) if key else {}
        stand = bool(key) and code.upper().startswith(('RL', 'DS'))   # 企位只有 DS／RL
        cells.append({
            'r': cell.get('r'), 'c': cell.get('c'),
            'v': cell.get('v', ''),
            't': cell.get('t', 0), 'l': cell.get('l', 0),
            'rr': cell.get('rr', 0), 'b': cell.get('b', 0),
            'code': code,
            'status': st,
            'pit': hall_pit.get(key),
            'dlr_bad': bool(iss.get('dlr_bad')),
            'guest_bad': bool(iss.get('guest_bad')),
            'stand': stand,
            'chair': types.get(key) or '' if key else '',
            'fs': cell.get('fs', 11),
            'fc': cell.get('fc', ''),
            'bold': bool(cell.get('bold')),
            'bg': cell.get('bg', ''),
            'event': bool(key) and _up_norm(code) in event_codes
        })
    cnt = {'total': 0, 'total_all': 0, 'chairs': 0, 'active': 0, 'inactive': 0, 'dlr_bad': 0, 'guest_bad': 0, 'spare': 0, 'new': 0, 'old': 0, 'event': 0}
    for x in cells:
        if x.get('code'):
            cnt['total_all'] += 1
        if not x.get('code') or x.get('stand') or x.get('event'):
            continue          # 企位（DS/RL）同活動區都冇櫈，唔計入新舊櫈
        cnt['chairs'] += 1
        if x.get('chair') == 'new':
            cnt['new'] += 1
        elif x.get('chair') == 'old':
            cnt['old'] += 1
    for x in cells:
        if not x.get('code'):
            continue
        if x.get('event'):
            cnt['event'] += 1
            continue
        cnt['total'] += 1
        if x.get('status') == '停運':
            cnt['inactive'] += 1
            if not x.get('dlr_bad'):
                cnt['spare'] += 1
        else:
            cnt['active'] += 1
        if x.get('dlr_bad'):
            cnt['dlr_bad'] += 1
        if x.get('guest_bad'):
            cnt['guest_bad'] += 1

    _payload = {'status': 'success', 'maxR': layout.get('maxR'), 'maxC': layout.get('maxC'),
                'minR': layout.get('minR', 1), 'minC': layout.get('minC', 1),
                'maxRu': layout.get('maxRu', layout.get('maxR')),
                'maxCu': layout.get('maxCu', layout.get('maxC')),
                'cols': layout.get('cols', []), 'rows': layout.get('rows', []),
                'hlines': layout.get('hlines', []), 'vlines': layout.get('vlines', []),
                'today': _up_today(), 'counts': cnt,
                'pits': layout.get('pits', []), 'extras': layout.get('extras', []),
                'merges': layout.get('merges', []), 'cells': cells,
                'edits': _chair_edits()}
    cache_set('chair_map', _payload, 30)
    return jsonify(_payload)


@app.route('/api/chair_map/bulk', methods=['POST'])
def chair_map_bulk():
    """批量設定：新櫈／舊櫈／取消新舊（同事及管理員）／營運／停運／活動台（管理員）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401

    data = request.json or {}
    action = (data.get('action') or '').strip()
    codes = data.get('codes') or []
    if not isinstance(codes, list):
        codes = [codes]
    codes = [_up_norm(str(c)) for c in codes]
    codes = [c for c in codes if c]
    if not codes:
        return jsonify({'status': 'error', 'message': '缺少台號。'})

    n = 0
    try:
        if action in ('new', 'old', 'none'):
            types = _chair_types()
            typ = '' if action == 'none' else action
            for c in codes:
                if typ:
                    types[c] = typ
                else:
                    types.pop(c, None)
                n += 1
            if not set_setting(_CHAIR_TYPE_KEY, json.dumps(types, ensure_ascii=False)):
                return jsonify({'status': 'error', 'message': '❌ 儲存失敗。'})
            label = {'new': '設為新櫈', 'old': '設為舊櫈', 'none': '取消新舊設定'}[action]

        elif action in ('operating', 'inactive'):
            st = '營運' if action == 'operating' else '停運'
            conn = None
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("UPDATE hall_tables SET status = %s WHERE UPPER(table_code) = ANY(%s)", (st, codes))
                n = c.rowcount
                conn.commit()
            finally:
                if conn:
                    try:
                        release_db_connection(conn)
                    except Exception:
                        pass
            label = '設為' + st

        elif action in ('event_on', 'event_off'):
            cur = _chair_event_tables()
            on = (action == 'event_on')
            for c in codes:
                if on:
                    cur.add(c)
                else:
                    cur.discard(c)
                n += 1
            if not set_setting(_CHAIR_EVENT_KEY, json.dumps(sorted(cur), ensure_ascii=False)):
                return jsonify({'status': 'error', 'message': '❌ 儲存失敗。'})
            label = '標記活動台' if on else '恢復台'

        else:
            return jsonify({'status': 'error', 'message': '❌ 未知操作。'})

        try:
            log_action('櫈況圖批量', '%s %d 台' % (label, n))
        except Exception:
            pass
        return jsonify({'status': 'success', 'count': n,
                        'message': '✅ 已%s %d 台' % (label, n)})
    except Exception as e:
        print('chair_map_bulk Error:', e)
        bust()
    return jsonify({'status': 'error', 'message': '❌ 失敗：%s' % str(e)})


@app.route('/api/chair_map/type', methods=['POST'])
def chair_map_set_type():
    """設定新舊 DLR 椅（存 app_settings）。已登入同事均可使用。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    code = _up_norm(data.get('code'))
    typ = (data.get('type') or '').strip().lower()
    if typ not in ('new', 'old', ''):
        return jsonify({'status': 'error', 'message': '類型錯誤'})
    if not code:
        return jsonify({'status': 'error', 'message': '缺少台號'})
    types = _chair_types()
    if typ:
        types[code] = typ
    else:
        types.pop(code, None)
    bust()
    return jsonify({'status': 'success'} if set_setting(_CHAIR_TYPE_KEY, json.dumps(types, ensure_ascii=False)) else
                   {'status': 'error', 'message': '儲存失敗'})


@app.route('/api/chair_map/edit', methods=['POST'])
def chair_map_edit():
    """儲存櫈況圖手動編輯（文字顏色／底色／粗體／框線／文字／合併）。已登入同事均可使用。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    raw = data.get('edits')
    if not isinstance(raw, dict):
        return jsonify({'status': 'error', 'message': '❌ 資料格式錯誤。'})

    clean = {'cells': {}, 'merges': [], 'unmerge': []}
    cells = raw.get('cells')
    if isinstance(cells, dict):
        for k, v in cells.items():
            if not isinstance(v, dict):
                continue
            k = str(k)
            if not re.match(r'^\d+,\d+$', k):
                continue
            props = {}
            for f in ('fc', 'bg', 'v'):
                if f in v and isinstance(v.get(f), str) and len(v.get(f)) <= 40:
                    props[f] = v.get(f)
            if 'bold' in v:
                props['bold'] = bool(v.get('bold'))
            for f in ('t', 'l', 'rr', 'b'):
                if f in v:
                    props[f] = 1 if v.get(f) else 0
            if props:
                clean['cells'][k] = props
    merges = raw.get('merges')
    if isinstance(merges, list):
        for m in merges[:80]:
            if not isinstance(m, dict):
                continue
            try:
                r = int(m.get('r')); c = int(m.get('c'))
                rs = max(1, min(30, int(m.get('rs') or 1)))
                cs = max(1, min(30, int(m.get('cs') or 1)))
                fs = float(m.get('fs') or 11)
            except Exception:
                continue
            if r < 1 or c < 1:
                continue
            clean['merges'].append({
                'r': r, 'c': c, 'rs': rs, 'cs': cs,
                'v': str(m.get('v') or '')[:60],
                'fc': str(m.get('fc') or '')[:20],
                'bg': str(m.get('bg') or '')[:20],
                'fs': max(6, min(48, fs)),
                'bold': bool(m.get('bold'))
            })
    unmerge = raw.get('unmerge')
    if isinstance(unmerge, list):
        for x in unmerge[:600]:
            x = str(x)
            if re.match(r'^\d+,\d+$', x):
                clean['unmerge'].append(x)
    if len(clean['cells']) > 4000:
        return jsonify({'status': 'error', 'message': '❌ 改動數量過多。'})

    if not set_setting(_CHAIR_EDIT_KEY, json.dumps(clean, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    try:
        log_action('修改櫈況圖', '手動編輯 %d 格、%d 個合併' % (len(clean['cells']), len(clean['merges'])))
    except Exception:
        pass
    bust()
    return jsonify({'status': 'success', 'cells': len(clean['cells']), 'merges': len(clean['merges'])})


@app.route('/api/chair_map/event', methods=['POST'])
def chair_map_event():
    """標記／取消活動台（唔計入台數及 DLR 數量）。已登入同事均可使用。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    data = request.json or {}
    codes = data.get('codes') or []
    if not isinstance(codes, list):
        codes = [codes]
    on = bool(data.get('on'))
    cur = _chair_event_tables()
    for c in codes:
        k = _up_norm(str(c))
        if not k:
            continue
        if on:
            cur.add(k)
        else:
            cur.discard(k)
    if not set_setting(_CHAIR_EVENT_KEY, json.dumps(sorted(cur), ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗。'})
    try:
        log_action('修改活動台', '%s %d 台' % ('加入' if on else '恢復', len(codes)))
    except Exception:
        pass
    bust()
    return jsonify({'status': 'success', 'count': len(cur)})


@app.route('/api/chair_map/import', methods=['POST'])
def chair_map_import():
    """上傳 Excel 版面（已登入同事均可）。解析後存 app_settings['chair_layout']。"""
    global _CHAIR_LAYOUT_CACHE
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'status': 'error', 'message': '❌ 沒有收到檔案。'})
    name = f.filename.lower()
    if not (name.endswith('.xlsx') or name.endswith('.xlsm')):
        return jsonify({'status': 'error', 'message': '❌ 只支援 .xlsx 格式。'})
    data = f.read()
    if len(data) > 8 * 1024 * 1024:
        return jsonify({'status': 'error', 'message': '❌ 檔案太大（上限 8MB）。'})
    try:
        layout = _xlsx_build_layout(data, _chair_base_codes(), _chair_static_layout())
        hit = sum(1 for c in layout.get('cells', []) if c.get('code'))
    except ImportError:
        return jsonify({'status': 'error', 'message': '❌ 伺服器未安裝解析組件（openpyxl），請重新部署。'})
    except Exception as e:
        print('Excel 解析失敗:', e)
        return jsonify({'status': 'error', 'message': '❌ Excel 解析失敗：' + str(e)[:120]})
    if not layout.get('cells'):
        return jsonify({'status': 'error', 'message': '❌ 讀不到任何格仔，請確認 Excel 內容。'})
    if hit == 0:
        return jsonify({'status': 'error', 'message': '❌ 對唔到任何格仔（Excel 位置可能同系統唔同）。請用第一個 Sheet，並確認位置無搬過。'})
    if not set_setting(_CHAIR_LAYOUT_KEY, json.dumps(layout, ensure_ascii=False)):
        return jsonify({'status': 'error', 'message': '❌ 儲存失敗，請稍後再試。'})
    _CHAIR_LAYOUT_CACHE = None
    codes = sum(1 for c in layout['cells'] if c.get('code'))
    try:
        log_action('上傳櫈況版面', '%s（%d 格、%d 台號）' % (f.filename, len(layout['cells']), codes))
    except Exception:
        pass
    bust()
    return jsonify({'status': 'success', 'cells': len(layout['cells']), 'codes': codes,
                    'merges': len(layout['merges']), 'message': '✅ 已套用新版面'})


@app.route('/api/chair_map/reset', methods=['POST'])
def chair_map_reset():
    """還原為 static 預設版面（已登入同事均可）。"""
    global _CHAIR_LAYOUT_CACHE
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    if not set_setting(_CHAIR_LAYOUT_KEY, ''):
        return jsonify({'status': 'error', 'message': '❌ 還原失敗。'})
    _CHAIR_LAYOUT_CACHE = None
    bust()
    return jsonify({'status': 'success', 'message': '✅ 已還原預設版面'})


# ================= 存貨記錄（82倉庫 / CP） =================
INV_ZONES = ('WH', 'CP')


def _inv_actor():
    return session.get('user_name') or session.get('role') or 'unknown'


def _inv_int(v):
    if v is None or v == '':
        return None
    try:
        n = int(float(v))
    except Exception:
        return None
    return max(0, n)


def _inv_str(v, maxlen=200):
    return (str(v) if v is not None else '').strip()[:maxlen]


def _inv_zone_for_loc(loc, current='WH'):
    """由位置名推算倉庫：CP##／白板後 → CP；A1／B12 等 → WH；其它保留原本。"""
    s = (loc or '').strip()
    if not s:
        return current
    if re.match(r'^CP\s*\d{1,2}$', s, re.I) or s.startswith('白板後'):
        return 'CP'
    if re.match(r'^[A-Z]\s*\d{1,2}$', s, re.I):
        return 'WH'
    return current


@app.route('/api/inventory', methods=['GET'])
def inventory_list():
    """存貨清單（已登入同事均可讀）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    _cached = cache_get('inventory')
    if _cached is not None:
        return jsonify(_cached)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, zone, loc, name, qty, COALESCE(unit,''), COALESCE(detail,''),
                            COALESCE(note,''), COALESCE(updated_by,''),
                            COALESCE(to_char(updated_at AT TIME ZONE 'Asia/Macau',
                                             'YYYY-MM-DD HH24:MI'), '')
                     FROM inventory_items
                     WHERE COALESCE(deleted, FALSE) = FALSE
                     ORDER BY zone, loc, sort, id""")
        rows = c.fetchall()
        items = []
        for r in rows:
            items.append({
                'id': r[0], 'zone': r[1] or 'WH', 'loc': r[2] or '', 'name': r[3] or '',
                'qty': r[4], 'unit': r[5], 'detail': r[6], 'note': r[7],
                'updated_by': r[8], 'updated_at': r[9]
            })
        c.execute("""SELECT item_id, action, COALESCE(detail,''), COALESCE(actor,''),
                            COALESCE(to_char(created_at AT TIME ZONE 'Asia/Macau',
                                             'MM-DD HH24:MI'), '')
                     FROM inventory_logs ORDER BY id DESC LIMIT 40""")
        logs = [{'item_id': x[0], 'action': x[1], 'detail': x[2], 'actor': x[3], 'at': x[4]}
                for x in c.fetchall()]
        _payload = {'status': 'success', 'items': items, 'logs': logs, 'total': len(items)}
        cache_set('inventory', _payload, 15)
        return jsonify(_payload)
    except Exception as e:
        return jsonify({'status': 'error', 'message': '❌ 讀取存貨失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/inventory/add', methods=['POST'])
def inventory_add():
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    name = _inv_str(d.get('name'), 120)
    zone = _inv_str(d.get('zone'), 4) or 'WH'
    if zone not in INV_ZONES:
        zone = 'WH'
    if not name:
        return jsonify({'status': 'error', 'message': '❌ 請輸入物品名稱'})
    loc = _inv_str(d.get('loc'), 20) or '未分類'
    zone = _inv_zone_for_loc(loc, zone)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO inventory_items
                     (zone, loc, name, qty, unit, detail, note, sort, updated_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s,
                             COALESCE((SELECT MAX(sort) FROM inventory_items), 0) + 1, %s)
                     RETURNING id""",
                  (zone, loc, name, _inv_int(d.get('qty')), _inv_str(d.get('unit'), 20),
                   _inv_str(d.get('detail'), 120), _inv_str(d.get('note'), 300), _inv_actor()))
        new_id = c.fetchone()[0]
        c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s, %s, %s, %s)",
                  (new_id, 'add', '%s %s／%s' % (zone, loc, name), _inv_actor()))
        conn.commit()
        bust()
        try: log_action('存貨新增', '%s %s／%s' % (zone, loc, name))
        except Exception: pass
        return jsonify({'status': 'success', 'id': new_id, 'message': '✅ 已新增 %s' % name})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 新增失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/inventory/update', methods=['POST'])
def inventory_update():
    """修改物品資料（名稱／位置／數量／單位／附加數量／備註）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    try:
        iid = int(d.get('id'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ 缺少 id'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT zone, name, loc, qty, COALESCE(unit,''), COALESCE(detail,''), COALESCE(note,'')
                     FROM inventory_items WHERE id = %s AND COALESCE(deleted, FALSE) = FALSE""", (iid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該物品（可能已刪除）'})
        name = _inv_str(d['name'], 120) if 'name' in d else row[1]
        loc = _inv_str(d['loc'], 20) if 'loc' in d else row[2]
        qty = _inv_int(d['qty']) if 'qty' in d else row[3]
        unit = _inv_str(d['unit'], 20) if 'unit' in d else row[4]
        detail = _inv_str(d['detail'], 120) if 'detail' in d else row[5]
        note = _inv_str(d['note'], 300) if 'note' in d else row[6]
        if not name:
            name = row[1]
        if not loc:
            loc = row[2] or '未分類'
        zone = _inv_zone_for_loc(loc, row[0] or 'WH')
        c.execute("""UPDATE inventory_items
                     SET name=%s, loc=%s, zone=%s, qty=%s, unit=%s, detail=%s, note=%s,
                         updated_at=NOW(), updated_by=%s
                     WHERE id=%s""", (name, loc, zone, qty, unit, detail, note, _inv_actor(), iid))
        parts = []
        if name != row[1]: parts.append('名稱 %s→%s' % (row[1], name))
        if loc != row[2]: parts.append('位置 %s→%s' % (row[2], loc))
        if qty != row[3]: parts.append('數量 %s→%s' % (row[3], qty))
        if note != row[6]: parts.append('備註更新')
        c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s, %s, %s, %s)",
                  (iid, 'update', '%s：%s' % (name, '、'.join(parts) or '資料更新'), _inv_actor()))
        conn.commit()
        bust()
        return jsonify({'status': 'success', 'message': '✅ 已更新 %s' % name})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 更新失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/inventory/adjust', methods=['POST'])
def inventory_adjust():
    """數量加減（delta 可正可負，最少 0）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    try:
        iid = int(d.get('id'))
        delta = int(d.get('delta', 0))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ 參數錯誤'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT name, qty FROM inventory_items
                     WHERE id = %s AND COALESCE(deleted, FALSE) = FALSE""", (iid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該物品'})
        before = row[1] if row[1] is not None else 0
        after = max(0, before + delta)
        c.execute("""UPDATE inventory_items SET qty=%s, updated_at=NOW(), updated_by=%s WHERE id=%s""",
                  (after, _inv_actor(), iid))
        c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s, %s, %s, %s)",
                  (iid, 'adjust', '%s：%d → %d（%+d）' % (row[0], before, after, delta), _inv_actor()))
        conn.commit()
        bust()
        return jsonify({'status': 'success', 'qty': after, 'message': '✅ %s：%d' % (row[0], after)})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 更新數量失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/inventory/move', methods=['POST'])
def inventory_move():
    """搬移位置（可一次過多件）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    loc = _inv_str(d.get('loc'), 20)
    if not loc:
        return jsonify({'status': 'error', 'message': '❌ 請選擇目標位置'})
    ids = d.get('ids')
    if not isinstance(ids, list):
        ids = [d.get('id')]
    try:
        qty_take = int(d.get('qty')) if d.get('qty') not in (None, '') else None
    except Exception:
        qty_take = None
    if qty_take is not None and qty_take <= 0:
        qty_take = None
    clean = []
    for x in ids:
        try: clean.append(int(x))
        except Exception: pass
    if not clean:
        return jsonify({'status': 'error', 'message': '❌ 缺少 id'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        moved = 0
        split = 0
        for iid in clean:
            c.execute("""SELECT name, loc, zone, qty FROM inventory_items
                         WHERE id=%s AND COALESCE(deleted, FALSE)=FALSE""", (iid,))
            row = c.fetchone()
            if not row:
                continue
            _name, _cur_loc, _cur_zone, _cur_qty = row[0], row[1], row[2], row[3]
            _zone = _inv_zone_for_loc(loc, _cur_zone or 'WH')
            if qty_take is not None and _cur_qty is not None and 0 < qty_take < _cur_qty:
                # 只搬部分：新增一項去新位置，原本位置扣減
                c.execute("""INSERT INTO inventory_items
                             (zone, loc, name, qty, unit, detail, note, sort, updated_by)
                             SELECT %s, %s, name, %s, COALESCE(unit,''), COALESCE(detail,''), COALESCE(note,''),
                                    COALESCE((SELECT MAX(sort) FROM inventory_items), 0) + 1, %s
                             FROM inventory_items WHERE id=%s RETURNING id""",
                          (_zone, loc, qty_take, _inv_actor(), iid))
                _new_id = c.fetchone()[0]
                c.execute("UPDATE inventory_items SET qty=%s, updated_at=NOW(), updated_by=%s WHERE id=%s",
                          (_cur_qty - qty_take, _inv_actor(), iid))
                c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s,'move_part',%s,%s)",
                          (iid, '%s：%s 共 %d，搬 %d 去 %s（剩 %d）' % (_name, _cur_loc or '未分類', _cur_qty, qty_take, loc, _cur_qty - qty_take), _inv_actor()))
                c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s,'move_part',%s,%s)",
                          (_new_id, '%s：由 %s 搬入 %d' % (_name, _cur_loc or '未分類', qty_take), _inv_actor()))
                split += 1
                moved += 1
                continue
            c.execute("""UPDATE inventory_items SET loc=%s, zone=%s, updated_at=NOW(), updated_by=%s
                         WHERE id=%s""", (loc, _zone, _inv_actor(), iid))
            c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s, %s, %s, %s)",
                      (iid, 'move', '%s：%s → %s' % (_name, _cur_loc or '未分類', loc), _inv_actor()))
            moved += 1
        conn.commit()
        bust()
        if split:
            return jsonify({'status': 'success', 'moved': moved, 'split': split,
                            'message': '✅ 已搬 %d 項去 %s（其中 %d 項只搬部分數量）' % (moved, loc, split)})
        return jsonify({'status': 'success', 'moved': moved, 'message': '✅ 已搬 %d 項去 %s' % (moved, loc)})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 搬移失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/api/inventory/delete', methods=['POST'])
def inventory_delete():
    """刪除物品（soft delete，可從稽核紀錄追查）。"""
    if not session.get('logged_in'):
        return jsonify({'status': 'error', 'message': '請先登入'}), 401
    d = request.get_json(silent=True) or {}
    try:
        iid = int(d.get('id'))
    except Exception:
        return jsonify({'status': 'error', 'message': '❌ 缺少 id'})
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name, loc FROM inventory_items WHERE id=%s AND COALESCE(deleted, FALSE)=FALSE", (iid,))
        row = c.fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '❌ 找不到該物品'})
        c.execute("""UPDATE inventory_items SET deleted=TRUE, updated_at=NOW(), updated_by=%s WHERE id=%s""",
                  (_inv_actor(), iid))
        c.execute("INSERT INTO inventory_logs (item_id, action, detail, actor) VALUES (%s, %s, %s, %s)",
                  (iid, 'delete', '%s（%s）' % (row[0], row[1] or '未分類'), _inv_actor()))
        conn.commit()
        bust()
        return jsonify({'status': 'success', 'message': '🗑️ 已刪除 %s' % row[0]})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({'status': 'error', 'message': '❌ 刪除失敗：%s' % e})
    finally:
        if conn:
            try: release_db_connection(conn)
            except Exception: pass


@app.route('/')
def index():
    # 將是否為管理員的狀態傳給前端
    is_admin = (session.get('role') == 'admin')
    return render_template('index.html', is_admin=is_admin, user_name=session.get('user_name', ''))

# 所有函數定義完成後才做資料表遷移（backfill 需要 _record_items）
ensure_shift_items()

if __name__ == '__main__':
    app.run()
