#!/usr/bin/env python3
"""
نظام مراقبة الشبكة — Dude + Render.com
نسخة مصلحة — تعمل على Render بدون أخطاء
"""

import os
import sqlite3
import threading
import schedule
import time
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

# ─────────────────────────────────────────
#  إعداداتك
# ─────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8583886234:AAEPcKBCyH0823cO4WYXc9dx0CObYfbo2Zs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1995981496")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "mysecret123")
PORT             = int(os.getenv("PORT", 5000))

# مواعيد التقارير
DAILY_TIME  = "23:00"
WEEKLY_DAY  = "friday"
WEEKLY_TIME = "09:00"
MONTHLY_DAY = 1

# تحذير إذا انقطع الجهاز أكثر من X مرات في اليوم
HIGH_OUTAGE_THRESHOLD = 5


# ══════════════════════════════════════════
#  قاعدة البيانات
# ══════════════════════════════════════════
DB_FILE = "/tmp/monitor.db"   # /tmp يعمل على Render

def get_db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            ip         TEXT    NOT NULL DEFAULT '',
            location   TEXT    NOT NULL DEFAULT '',
            group_name TEXT    NOT NULL DEFAULT 'عام',
            added_at   TEXT    NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS outages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device       TEXT NOT NULL,
            ip           TEXT NOT NULL DEFAULT '',
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            duration_sec INTEGER,
            resolved     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device     TEXT NOT NULL,
            event      TEXT NOT NULL,
            message    TEXT,
            ip         TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device     TEXT NOT NULL,
            note       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()


# ══════════════════════════════════════════
#  دوال مساعدة
# ══════════════════════════════════════════
def _now():
    return datetime.now().isoformat(timespec='seconds')

def _dt(s):
    return datetime.fromisoformat(s)

def fmt_dur(secs):
    if not secs or secs < 0: return "—"
    secs = int(secs)
    if secs < 60:    return f"{secs} ثانية"
    if secs < 3600:  return f"{secs//60} دقيقة"
    if secs < 86400: return f"{secs//3600} ساعة و{(secs%3600)//60} دقيقة"
    return f"{secs//86400} يوم و{(secs%86400)//3600} ساعة"


# ══════════════════════════════════════════
#  إرسال تيليغرام — بدون asyncio (requests مباشر)
# ══════════════════════════════════════════
def send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        for i in range(0, len(text), 4000):
            requests.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text[i:i+4000],
                "parse_mode": "Markdown"
            }, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")


# ══════════════════════════════════════════
#  إدارة الأجهزة
# ══════════════════════════════════════════
def ensure_device(name, ip="", location="", group="عام"):
    db = get_db()
    row = db.execute("SELECT id,ip FROM devices WHERE name=?", (name,)).fetchone()
    if not row:
        db.execute(
            "INSERT INTO devices (name,ip,location,group_name,added_at) VALUES (?,?,?,?,?)",
            (name, ip, location, group, _now())
        )
        db.commit()
        db.close()
        logging.info(f"جهاز جديد: {name} ({ip})")
        send(f"📱 *جهاز جديد أُضيف تلقائياً!*\n\n"
             f"🔖 *{name}*\n"
             f"🌐 IP: `{ip or '—'}`\n"
             f"📍 {location or 'غير محدد'}\n"
             f"🕒 {datetime.now().strftime('%H:%M:%S')}")
    else:
        if ip and not row["ip"]:
            db.execute("UPDATE devices SET ip=? WHERE name=?", (ip, name))
            db.commit()
        db.close()

def get_all_devices():
    db = get_db()
    rows = db.execute("SELECT * FROM devices WHERE active=1 ORDER BY name").fetchall()
    db.close()
    return rows

def get_device(name):
    db = get_db()
    row = db.execute("SELECT * FROM devices WHERE name=?", (name,)).fetchone()
    db.close()
    return row


# ══════════════════════════════════════════
#  دوال قاعدة البيانات
# ══════════════════════════════════════════
def db_open_outage(device, ip):
    db = get_db()
    row = db.execute("SELECT id FROM outages WHERE device=? AND resolved=0", (device,)).fetchone()
    if not row:
        db.execute("INSERT INTO outages (device,ip,started_at) VALUES (?,?,?)", (device, ip, _now()))
        db.commit()
    db.close()

def db_close_outage(device):
    db = get_db()
    row = db.execute("SELECT id,started_at FROM outages WHERE device=? AND resolved=0", (device,)).fetchone()
    if not row:
        db.close()
        return None
    ended = _now()
    secs  = int((_dt(ended) - _dt(row["started_at"])).total_seconds())
    db.execute("UPDATE outages SET ended_at=?,duration_sec=?,resolved=1 WHERE id=?", (ended, secs, row["id"]))
    db.commit()
    db.close()
    return secs

def db_log_event(device, event, message="", ip=""):
    db = get_db()
    db.execute("INSERT INTO events (device,event,message,ip,created_at) VALUES (?,?,?,?,?)",
               (device, event, message, ip, _now()))
    db.commit()
    db.close()

def db_count_outages(device, since_dt):
    db = get_db()
    n = db.execute(
        "SELECT COUNT(*) FROM outages WHERE device=? AND started_at>=?",
        (device, since_dt.isoformat())
    ).fetchone()[0]
    db.close()
    return n

def db_top_outages(since_dt, limit=10):
    db = get_db()
    rows = db.execute(
        "SELECT device, COUNT(*) c FROM outages WHERE started_at>=? GROUP BY device ORDER BY c DESC LIMIT ?",
        (since_dt.isoformat(), limit)
    ).fetchall()
    db.close()
    return rows

def db_active_outages():
    db = get_db()
    rows = db.execute(
        "SELECT device,ip,started_at FROM outages WHERE resolved=0 ORDER BY started_at"
    ).fetchall()
    db.close()
    return rows

def db_avg_duration(device, since_dt):
    db = get_db()
    row = db.execute(
        "SELECT AVG(duration_sec) FROM outages WHERE device=? AND started_at>=? AND resolved=1",
        (device, since_dt.isoformat())
    ).fetchone()
    db.close()
    return row[0] or 0

def db_add_note(device, note):
    db = get_db()
    db.execute("INSERT INTO notes (device,note,created_at) VALUES (?,?,?)", (device, note, _now()))
    db.commit()
    db.close()


# ══════════════════════════════════════════
#  التقارير
# ══════════════════════════════════════════
def report_daily():
    since   = datetime.now() - timedelta(days=1)
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    total   = 0

    msg = f"📊 *تقرير الشبكة اليوم*\n📅 {datetime.now().strftime('%Y-%m-%d')}\n{'─'*28}\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        total += c
        icon  = "🔴" if n in active else "🟢"
        msg  += f"{icon} *{n}*"
        if d["location"]: msg += f"  _{d['location']}_"
        msg  += f"\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg  += "\n\n"

    msg += f"📈 إجمالي انقطاعات اليوم: *{total}*\n"

    active_list = db_active_outages()
    if active_list:
        msg += f"\n🔴 *متوقفة الآن ({len(active_list)}):*\n"
        for r in active_list:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   • *{r['device']}* — منذ {fmt_dur(secs)}\n"

    send(msg)
    _check_high_outages(since)

def report_weekly():
    since   = datetime.now() - timedelta(days=7)
    devices = get_all_devices()
    msg     = (f"📅 *تقرير الأسبوع*\n"
               f"📆 {since.strftime('%Y-%m-%d')} ← {datetime.now().strftime('%Y-%m-%d')}\n"
               f"{'─'*28}\n\n")
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"

    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top):
            msg += f"   {medals[i] if i<10 else '•'} {row['device']} — *{row['c']}* انقطاع\n"
    send(msg)

def report_monthly():
    since   = datetime.now() - timedelta(days=30)
    devices = get_all_devices()
    msg     = f"📈 *تقرير الشهر* (آخر 30 يوم)\n{'─'*28}\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"

    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top):
            msg += f"   {medals[i] if i<10 else '•'} {row['device']} — *{row['c']}* انقطاع\n"

    active = db_active_outages()
    if active:
        msg += "\n🔴 *متوقفة الآن:*\n"
        for r in active:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   ❌ *{r['device']}* (`{r['ip']}`) — {fmt_dur(secs)}\n"
    send(msg)

def report_active():
    active = db_active_outages()
    if not active:
        send("✅ *جميع الأجهزة تعمل بشكل طبيعي* 🟢")
        return
    msg = f"🔴 *الأجهزة المتوقفة الآن ({len(active)}):*\n\n"
    for r in active:
        secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
        msg += f"❌ *{r['device']}*  `{r['ip']}`\n   ⏱ منذ: {fmt_dur(secs)}\n\n"
    send(msg)

def _check_high_outages(since):
    for row in db_top_outages(since, limit=5):
        if row["c"] >= HIGH_OUTAGE_THRESHOLD:
            send(f"⚠️ *تحذير: جهاز يعاني من مشكلة متكررة!*\n\n"
                 f"📡 *{row['device']}*\n"
                 f"⚡ انقطع *{row['c']}* مرات خلال آخر 24 ساعة\n"
                 f"🔧 يُنصح بمراجعة الجهاز")


# ══════════════════════════════════════════
#  التقارير المجدولة
# ══════════════════════════════════════════
def start_scheduler():
    schedule.every().day.at(DAILY_TIME).do(report_daily)
    getattr(schedule.every(), WEEKLY_DAY).at(WEEKLY_TIME).do(report_weekly)
    schedule.every().day.at("08:00").do(
        lambda: report_monthly() if datetime.now().day == MONTHLY_DAY else None
    )
    def tick():
        while True:
            schedule.run_pending()
            time.sleep(30)
    threading.Thread(target=tick, daemon=True).start()


# ══════════════════════════════════════════
#  بوت تيليغرام — يستقبل الأوامر عبر Webhook
# ══════════════════════════════════════════
def handle_telegram_update(update: dict):
    """معالجة أوامر البوت"""
    try:
        msg  = update.get("message", {})
        text = msg.get("text", "").strip()
        if not text: return

        cmd = text.split()[0].lower().replace("/", "").split("@")[0]
        args = text.split()[1:]

        if cmd in ("start", "help"):
            send("👋 *مرحباً — نظام مراقبة الشبكة*\n\n"
                 "📋 *الأوامر:*\n"
                 "🔹 /status — حالة الأجهزة الآن\n"
                 "🔹 /outages — الأجهزة المتوقفة\n"
                 "🔹 /devices — قائمة كل الأجهزة\n"
                 "🔹 /stats — إحصائيات سريعة\n"
                 "🔹 /daily — تقرير اليوم\n"
                 "🔹 /weekly — تقرير الأسبوع\n"
                 "🔹 /monthly — تقرير الشهر\n"
                 "🔹 /note جهاز ملاحظة — إضافة ملاحظة")

        elif cmd == "status":
            devices = get_all_devices()
            active  = {r["device"] for r in db_active_outages()}
            msg_txt = "📡 *حالة الأجهزة الآن:*\n\n"
            if not devices:
                msg_txt += "لا يوجد أجهزة بعد — سيتم إضافتها تلقائياً عند أول إشعار من Dude"
            for d in devices:
                icon     = "🔴" if d["name"] in active else "🟢"
                msg_txt += f"{icon} *{d['name']}*  `{d['ip'] or '—'}`\n"
            send(msg_txt)

        elif cmd == "outages":
            threading.Thread(target=report_active).start()

        elif cmd == "devices":
            devices = get_all_devices()
            msg_txt = f"📋 *قائمة الأجهزة ({len(devices)}):*\n\n"
            groups  = {}
            for d in devices:
                groups.setdefault(d["group_name"], []).append(d)
            for g, devs in groups.items():
                msg_txt += f"🗂 *{g}*\n"
                for d in devs:
                    msg_txt += f"   • *{d['name']}*  `{d['ip'] or '—'}`"
                    if d["location"]: msg_txt += f"  _{d['location']}_"
                    msg_txt += "\n"
                msg_txt += "\n"
            send(msg_txt)

        elif cmd == "stats":
            since24 = datetime.now() - timedelta(hours=24)
            since7  = datetime.now() - timedelta(days=7)
            devices = get_all_devices()
            active  = db_active_outages()
            t24     = sum(db_count_outages(d["name"], since24) for d in devices)
            t7      = sum(db_count_outages(d["name"], since7)  for d in devices)
            msg_txt = (f"📊 *إحصائيات سريعة*\n\n"
                       f"📡 إجمالي الأجهزة: *{len(devices)}*\n"
                       f"🔴 متوقفة الآن: *{len(active)}*\n"
                       f"🟢 تعمل الآن: *{len(devices)-len(active)}*\n\n"
                       f"⚡ انقطاعات آخر 24 ساعة: *{t24}*\n"
                       f"⚡ انقطاعات آخر 7 أيام: *{t7}*\n")
            top = db_top_outages(since7, limit=3)
            if top:
                msg_txt += "\n🏆 *أكثر الأجهزة انقطاعاً (أسبوع):*\n"
                for row in top:
                    msg_txt += f"   • {row['device']}: {row['c']} انقطاع\n"
            send(msg_txt)

        elif cmd == "daily":
            threading.Thread(target=report_daily).start()

        elif cmd == "weekly":
            threading.Thread(target=report_weekly).start()

        elif cmd == "monthly":
            threading.Thread(target=report_monthly).start()

        elif cmd == "note":
            if len(args) >= 2:
                db_add_note(args[0], " ".join(args[1:]))
                send(f"✅ تم حفظ الملاحظة على *{args[0]}*")
            else:
                send("الاستخدام: /note اسم_الجهاز الملاحظة")

    except Exception as e:
        logging.error(f"Bot error: {e}")


# ══════════════════════════════════════════
#  لوحة الحالة HTML
# ══════════════════════════════════════════
STATUS_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>حالة الشبكة</title>
<style>
  *{box-sizing:border-box}
  body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
  h1{color:#38bdf8;text-align:center;margin-bottom:4px}
  .sub{text-align:center;color:#64748b;margin-bottom:24px;font-size:13px}
  .stats{display:flex;justify-content:center;gap:20px;margin-bottom:28px;flex-wrap:wrap}
  .stat{background:#1e293b;padding:14px 28px;border-radius:12px;text-align:center}
  .stat-n{font-size:30px;font-weight:bold;color:#38bdf8}
  .stat-n.red{color:#f87171} .stat-n.green{color:#4ade80}
  .stat-l{font-size:12px;color:#64748b;margin-top:4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
  .card{background:#1e293b;border-radius:12px;padding:16px;border:1px solid #334155}
  .card.down{border-color:#ef4444;background:#1f0f0f}
  .card.up{border-color:#22c55e}
  .name{font-size:16px;font-weight:bold;margin-bottom:6px}
  .ip{color:#94a3b8;font-size:12px;margin-bottom:4px}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:bold;margin-top:8px}
  .badge.up{background:#14532d;color:#4ade80}
  .badge.down{background:#450a0a;color:#f87171}
  .dur{color:#fbbf24;font-size:12px;margin-top:5px}
</style>
</head>
<body>
<h1>🌐 لوحة مراقبة الشبكة</h1>
<p class="sub">آخر تحديث: {{ now }} — تتجدد تلقائياً كل 30 ثانية</p>
<div class="stats">
  <div class="stat"><div class="stat-n">{{ total }}</div><div class="stat-l">إجمالي الأجهزة</div></div>
  <div class="stat"><div class="stat-n green">{{ up_count }}</div><div class="stat-l">متصلة 🟢</div></div>
  <div class="stat"><div class="stat-n red">{{ down_count }}</div><div class="stat-l">منقطعة 🔴</div></div>
</div>
<div class="grid">
{% for d in devices %}
<div class="card {{ d.status }}">
  <div class="name">📡 {{ d.name }}</div>
  <div class="ip">🌐 {{ d.ip or '—' }}</div>
  {% if d.location %}<div class="ip">📍 {{ d.location }}</div>{% endif %}
  <div class="ip">🗂 {{ d.group }}</div>
  <span class="badge {{ d.status }}">{{ '🔴 منقطع' if d.status == 'down' else '🟢 متصل' }}</span>
  {% if d.status == 'down' %}<div class="dur">⏱ منذ {{ d.duration }}</div>{% endif %}
</div>
{% endfor %}
{% if not devices %}
<div style="text-align:center;color:#64748b;padding:40px;grid-column:1/-1">
  لا يوجد أجهزة بعد — سيتم إضافتها تلقائياً عند أول إشعار من Dude
</div>
{% endif %}
</div>
</body>
</html>
"""


# ══════════════════════════════════════════
#  Flask
# ══════════════════════════════════════════
app = Flask(__name__)

@app.route("/", methods=["GET"])
def dashboard():
    devices  = get_all_devices()
    active_d = {r["device"]: r for r in db_active_outages()}
    out      = []
    for d in devices:
        name    = d["name"]
        is_down = name in active_d
        dur     = ""
        if is_down:
            secs = int((datetime.now() - _dt(active_d[name]["started_at"])).total_seconds())
            dur  = fmt_dur(secs)
        out.append({
            "name": name, "ip": d["ip"],
            "location": d["location"], "group": d["group_name"],
            "status": "down" if is_down else "up",
            "duration": dur
        })
    total = len(out)
    down  = sum(1 for x in out if x["status"] == "down")
    return render_template_string(STATUS_HTML,
        devices=out, total=total,
        up_count=total-down, down_count=down,
        now=datetime.now().strftime("%H:%M:%S"))


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    # ── استقبال أوامر البوت من تيليغرام ──
    if "update_id" in data:
        threading.Thread(target=handle_telegram_update, args=(data,)).start()
        return jsonify({"ok": True})

    # ── استقبال إشعارات Dude ──
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    device   = str(data.get("device",   "")).strip()
    event    = str(data.get("event",    "")).strip().lower()
    ip       = str(data.get("ip",       "")).strip()
    location = str(data.get("location", "")).strip()
    group    = str(data.get("group",    "عام")).strip()
    message  = str(data.get("message",  "")).strip()

    if not device or event not in ("down", "up"):
        return jsonify({"error": "invalid data"}), 400

    ensure_device(device, ip, location, group)
    db_log_event(device, event, message, ip)

    d       = get_device(device)
    dev_ip  = ip or (d["ip"] if d else "—")
    dev_loc = location or (d["location"] if d else "—")

    if event == "down":
        db_open_outage(device, dev_ip)
        send(f"🚨 *انقطاع!*\n\n"
             f"📡 *{device}*  |  `{dev_ip}`\n"
             f"📍 {dev_loc or '—'}\n"
             f"💬 {message or 'Link Down'}\n"
             f"🕒 {datetime.now().strftime('%H:%M:%S')}")

    elif event == "up":
        secs = db_close_outage(device)
        send(f"✅ *عاد للاتصال!*\n\n"
             f"📡 *{device}*  |  `{dev_ip}`\n"
             f"📍 {dev_loc or '—'}\n"
             f"⏱ مدة الانقطاع: *{fmt_dur(secs)}*\n"
             f"🕒 {datetime.now().strftime('%H:%M:%S')}")

    return jsonify({"ok": True})


@app.route("/tgwebhook", methods=["POST"])
def tg_webhook():
    """Telegram Webhook endpoint"""
    update = request.json or {}
    threading.Thread(target=handle_telegram_update, args=(update,)).start()
    return jsonify({"ok": True})


@app.route("/api/devices", methods=["GET"])
def api_devices():
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    return jsonify([{
        "name": d["name"], "ip": d["ip"],
        "location": d["location"], "group": d["group_name"],
        "status": "down" if d["name"] in active else "up",
        "added_at": d["added_at"]
    } for d in devices])


@app.route("/setup_webhook", methods=["GET"])
def setup_webhook():
    """اضغط هذا الرابط مرة واحدة لربط البوت"""
    base_url = request.host_url.rstrip("/")
    tg_url   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(tg_url, json={"url": f"{base_url}/tgwebhook"})
    return jsonify(r.json())


# ══════════════════════════════════════════
#  نقطة البداية
# ══════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

init_db()
start_scheduler()

# إرسال رسالة بدء التشغيل في خيط منفصل
def _startup():
    time.sleep(3)
    send(f"🚀 *النظام يعمل على Render.com*\n\n"
         f"🔗 جاهز لاستقبال إشعارات Dude\n"
         f"📱 الأجهزة تُضاف تلقائياً\n"
         f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

threading.Thread(target=_startup, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
