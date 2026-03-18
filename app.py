import os, sqlite3, logging, requests, threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8583886234:AAEPcKBCyH0823cO4WYXc9dx0CObYfbo2Zs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1995981496")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "mysecret123")
PORT             = int(os.getenv("PORT", 5000))
DB_FILE          = "/tmp/monitor.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ══════════════════════════════════════════
#  قاعدة البيانات
# ══════════════════════════════════════════
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
def _now(): return datetime.now().isoformat(timespec='seconds')
def _dt(s): return datetime.fromisoformat(s)

def fmt_dur(secs):
    if not secs or secs < 0: return "—"
    secs = int(secs)
    if secs < 60:    return str(secs) + " ثانية"
    if secs < 3600:  return str(secs//60) + " دقيقة"
    if secs < 86400: return str(secs//3600) + " ساعة و" + str((secs%3600)//60) + " دقيقة"
    return str(secs//86400) + " يوم و" + str((secs%86400)//3600) + " ساعة"


# ══════════════════════════════════════════
#  إرسال تيليغرام
# ══════════════════════════════════════════
def send(text):
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
        for i in range(0, len(text), 4000):
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[i:i+4000],
                "parse_mode": "Markdown"
            }, timeout=10)
    except Exception as e:
        logging.error("Telegram: " + str(e))


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
        send("📱 *جهاز جديد اضيف تلقائياً!*\n\n🔖 *" + name + "*\n🌐 `" + (ip or "—") + "`\n📍 " + (location or "غير محدد"))
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

def db_open_outage(device, ip):
    db = get_db()
    if not db.execute("SELECT id FROM outages WHERE device=? AND resolved=0", (device,)).fetchone():
        db.execute("INSERT INTO outages (device,ip,started_at) VALUES (?,?,?)", (device, ip, _now()))
        db.commit()
    db.close()

def db_close_outage(device):
    db = get_db()
    row = db.execute("SELECT id,started_at FROM outages WHERE device=? AND resolved=0", (device,)).fetchone()
    if not row:
        db.close(); return None
    ended = _now()
    secs  = int((_dt(ended) - _dt(row["started_at"])).total_seconds())
    db.execute("UPDATE outages SET ended_at=?,duration_sec=?,resolved=1 WHERE id=?", (ended, secs, row["id"]))
    db.commit(); db.close()
    return secs

def db_log_event(device, event, message="", ip=""):
    db = get_db()
    db.execute("INSERT INTO events (device,event,message,ip,created_at) VALUES (?,?,?,?,?)",
               (device, event, message, ip, _now()))
    db.commit(); db.close()

def db_count_outages(device, since_dt):
    db = get_db()
    n = db.execute("SELECT COUNT(*) FROM outages WHERE device=? AND started_at>=?",
                   (device, since_dt.isoformat())).fetchone()[0]
    db.close(); return n

def db_top_outages(since_dt, limit=10):
    db = get_db()
    rows = db.execute(
        "SELECT device,COUNT(*) c FROM outages WHERE started_at>=? GROUP BY device ORDER BY c DESC LIMIT ?",
        (since_dt.isoformat(), limit)
    ).fetchall()
    db.close(); return rows

def db_active_outages():
    db = get_db()
    rows = db.execute("SELECT device,ip,started_at FROM outages WHERE resolved=0 ORDER BY started_at").fetchall()
    db.close(); return rows

def db_avg_duration(device, since_dt):
    db = get_db()
    row = db.execute(
        "SELECT AVG(duration_sec) FROM outages WHERE device=? AND started_at>=? AND resolved=1",
        (device, since_dt.isoformat())
    ).fetchone()
    db.close(); return row[0] or 0

def db_add_note(device, note):
    db = get_db()
    db.execute("INSERT INTO notes (device,note,created_at) VALUES (?,?,?)", (device, note, _now()))
    db.commit(); db.close()


# ══════════════════════════════════════════
#  التقارير
# ══════════════════════════════════════════
def report_daily():
    since   = datetime.now() - timedelta(days=1)
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    total   = 0
    today   = datetime.now().strftime('%Y-%m-%d')
    sep     = "─" * 25

    msg = "📊 *تقرير الشبكة اليوم*\n📅 " + today + "\n" + sep + "\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        total += c
        icon  = "🔴" if n in active else "🟢"
        msg  += icon + " *" + n + "*"
        if d["location"]: msg += "  _" + d["location"] + "_"
        msg  += "\n   ⚡ انقطاعات: *" + str(c) + "*"
        if avg > 0: msg += "  |  ⏱ متوسط: " + fmt_dur(avg)
        msg  += "\n\n"

    msg += "📈 اجمالي الانقطاعات: *" + str(total) + "*\n"

    al = db_active_outages()
    if al:
        msg += "\n🔴 *متوقفة الآن (" + str(len(al)) + "):*\n"
        for r in al:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += "   • *" + r["device"] + "* — منذ " + fmt_dur(secs) + "\n"

    send(msg)
    _check_high_outages(since)

def report_weekly():
    since   = datetime.now() - timedelta(days=7)
    devices = get_all_devices()
    sep     = "─" * 25
    msg     = "📅 *تقرير الأسبوع*\n📆 " + since.strftime('%Y-%m-%d') + " ← " + datetime.now().strftime('%Y-%m-%d') + "\n" + sep + "\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        msg += "📡 *" + n + "*\n   ⚡ انقطاعات: *" + str(c) + "*"
        if avg > 0: msg += "  |  ⏱ متوسط: " + fmt_dur(avg)
        msg += "\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *اكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top):
            msg += "   " + (medals[i] if i<10 else "•") + " " + row["device"] + " — *" + str(row["c"]) + "* انقطاع\n"
    send(msg)

def report_monthly():
    since   = datetime.now() - timedelta(days=30)
    devices = get_all_devices()
    sep     = "─" * 25
    msg     = "📈 *تقرير الشهر* (آخر 30 يوم)\n" + sep + "\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        msg += "📡 *" + n + "*\n   ⚡ انقطاعات: *" + str(c) + "*"
        if avg > 0: msg += "  |  ⏱ متوسط: " + fmt_dur(avg)
        msg += "\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *اكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top):
            msg += "   " + (medals[i] if i<10 else "•") + " " + row["device"] + " — *" + str(row["c"]) + "* انقطاع\n"
    al = db_active_outages()
    if al:
        msg += "\n🔴 *متوقفة الآن:*\n"
        for r in al:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += "   ❌ *" + r["device"] + "* (`" + r["ip"] + "`) — " + fmt_dur(secs) + "\n"
    send(msg)

def report_active():
    al = db_active_outages()
    if not al:
        send("✅ *جميع الأجهزة تعمل بشكل طبيعي* 🟢")
        return
    msg = "🔴 *الأجهزة المتوقفة الآن (" + str(len(al)) + "):*\n\n"
    for r in al:
        secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
        msg += "❌ *" + r["device"] + "*  `" + r["ip"] + "`\n   ⏱ منذ: " + fmt_dur(secs) + "\n\n"
    send(msg)

def _check_high_outages(since):
    for row in db_top_outages(since, limit=5):
        if row["c"] >= HIGH_OUTAGE_THRESHOLD:
            send("⚠️ *تحذير: جهاز يعاني من مشكلة متكررة!*\n\n📡 *" + row["device"] + "*\n⚡ انقطع *" + str(row["c"]) + "* مرات خلال آخر 24 ساعة\n🔧 يُنصح بمراجعة الجهاز")


# ══════════════════════════════════════════
#  أوامر البوت
# ══════════════════════════════════════════
def handle_bot(update):
    try:
        text = update.get("message", {}).get("text", "").strip()
        if not text: return
        cmd  = text.split()[0].lower().replace("/","").split("@")[0]
        args = text.split()[1:]

        if cmd in ("start","help"):
            send("👋 *مرحباً — نظام مراقبة الشبكة*\n\n"
                 "📋 *الأوامر:*\n"
                 "🔹 /status — حالة الأجهزة الآن\n"
                 "🔹 /outages — الأجهزة المتوقفة\n"
                 "🔹 /devices — قائمة كل الأجهزة\n"
                 "🔹 /stats — احصائيات سريعة\n"
                 "🔹 /daily — تقرير اليوم\n"
                 "🔹 /weekly — تقرير الأسبوع\n"
                 "🔹 /monthly — تقرير الشهر\n"
                 "🔹 /note جهاز ملاحظة — اضافة ملاحظة")

        elif cmd == "status":
            devices = get_all_devices()
            active  = {r["device"] for r in db_active_outages()}
            msg     = "📡 *حالة الأجهزة الآن:*\n\n"
            if not devices: msg += "لا يوجد أجهزة بعد ⏳"
            for d in devices:
                icon = "🔴" if d["name"] in active else "🟢"
                msg += icon + " *" + d["name"] + "*  `" + (d["ip"] or "—") + "`\n"
            send(msg)

        elif cmd == "outages":  report_active()
        elif cmd == "daily":    report_daily()
        elif cmd == "weekly":   report_weekly()
        elif cmd == "monthly":  report_monthly()

        elif cmd == "devices":
            devices = get_all_devices()
            msg     = "📋 *قائمة الأجهزة (" + str(len(devices)) + "):*\n\n"
            groups  = {}
            for d in devices: groups.setdefault(d["group_name"], []).append(d)
            for g, devs in groups.items():
                msg += "🗂 *" + g + "*\n"
                for d in devs:
                    msg += "   • *" + d["name"] + "*  `" + (d["ip"] or "—") + "`"
                    if d["location"]: msg += "  _" + d["location"] + "_"
                    msg += "\n"
                msg += "\n"
            send(msg)

        elif cmd == "stats":
            since24 = datetime.now() - timedelta(hours=24)
            since7  = datetime.now() - timedelta(days=7)
            devices = get_all_devices()
            active  = db_active_outages()
            t24     = sum(db_count_outages(d["name"], since24) for d in devices)
            t7      = sum(db_count_outages(d["name"], since7)  for d in devices)
            msg     = "📊 *احصائيات سريعة*\n\n"
            msg    += "📡 الأجهزة: *" + str(len(devices)) + "*\n"
            msg    += "🔴 متوقفة: *" + str(len(active)) + "*\n"
            msg    += "🟢 تعمل: *" + str(len(devices)-len(active)) + "*\n\n"
            msg    += "⚡ انقطاعات 24 ساعة: *" + str(t24) + "*\n"
            msg    += "⚡ انقطاعات 7 أيام: *" + str(t7) + "*\n"
            top = db_top_outages(since7, limit=3)
            if top:
                msg += "\n🏆 *اكثر الأجهزة انقطاعاً:*\n"
                for row in top:
                    msg += "   • " + row["device"] + ": " + str(row["c"]) + " انقطاع\n"
            send(msg)

        elif cmd == "note":
            if len(args) >= 2:
                db_add_note(args[0], " ".join(args[1:]))
                send("✅ تم حفظ الملاحظة على *" + args[0] + "*")
            else:
                send("الاستخدام: /note اسم_الجهاز الملاحظة")

    except Exception as e:
        logging.error("Bot: " + str(e))


# ══════════════════════════════════════════
#  لوحة HTML
# ══════════════════════════════════════════
HTML = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8"><meta http-equiv="refresh" content="30">
<title>مراقبة الشبكة</title>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
h1{color:#38bdf8;text-align:center;margin-bottom:4px}
.sub{text-align:center;color:#64748b;font-size:13px;margin-bottom:24px}
.stats{display:flex;justify-content:center;gap:20px;margin-bottom:28px;flex-wrap:wrap}
.stat{background:#1e293b;padding:14px 28px;border-radius:12px;text-align:center}
.stat-n{font-size:30px;font-weight:bold;color:#38bdf8}
.stat-n.red{color:#f87171}.stat-n.green{color:#4ade80}
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
.empty{text-align:center;color:#64748b;padding:40px;grid-column:1/-1}
</style></head>
<body>
<h1>🌐 لوحة مراقبة الشبكة</h1>
<p class="sub">آخر تحديث: NOW — تتجدد كل 30 ثانية</p>
<div class="stats">
  <div class="stat"><div class="stat-n">TOTAL</div><div class="stat-l">اجمالي الأجهزة</div></div>
  <div class="stat"><div class="stat-n green">UPCOUNT</div><div class="stat-l">متصلة 🟢</div></div>
  <div class="stat"><div class="stat-n red">DOWNCOUNT</div><div class="stat-l">منقطعة 🔴</div></div>
</div>
<div class="grid">CARDS</div>
</body></html>"""

def build_dashboard():
    devices  = get_all_devices()
    active_d = {r["device"]: r for r in db_active_outages()}
    cards    = ""
    down_cnt = 0
    for d in devices:
        name    = d["name"]
        is_down = name in active_d
        if is_down: down_cnt += 1
        dur = ""
        if is_down:
            secs = int((datetime.now() - _dt(active_d[name]["started_at"])).total_seconds())
            dur  = fmt_dur(secs)
        status = "down" if is_down else "up"
        badge  = "🔴 منقطع" if is_down else "🟢 متصل"
        dur_html = '<div class="dur">⏱ منذ ' + dur + '</div>' if is_down else ""
        loc_html = '<div class="ip">📍 ' + d["location"] + '</div>' if d["location"] else ""
        cards += '<div class="card ' + status + '"><div class="name">📡 ' + name + '</div>'
        cards += '<div class="ip">🌐 ' + (d["ip"] or "—") + '</div>'
        cards += loc_html
        cards += '<span class="badge ' + status + '">' + badge + '</span>' + dur_html + '</div>'

    total = len(devices)
    if not devices:
        cards = '<div class="empty">لا يوجد أجهزة بعد — ستُضاف تلقائياً عند أول اشعار من Dude</div>'

    html = HTML.replace("NOW", datetime.now().strftime("%I:%M:%S %p"))
    html = html.replace("TOTAL", str(total))
    html = html.replace("UPCOUNT", str(total - down_cnt))
    html = html.replace("DOWNCOUNT", str(down_cnt))
    html = html.replace("CARDS", cards)
    return html


# ══════════════════════════════════════════
#  Flask
# ══════════════════════════════════════════
app = Flask(__name__)

@app.route("/")
def dashboard():
    init_db()
    return build_dashboard()

@app.route("/webhook", methods=["POST","GET"])
def webhook():
    try:
        data = request.json or {}
    except:
        data = {}
    if not data:
        data = request.args.to_dict()
        data.update(request.form.to_dict())
    if not data:
        raw = request.get_data(as_text=True)
        for item in raw.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                data[k.strip()] = v.strip()

    if "update_id" in data:
        threading.Thread(target=handle_bot, args=(data,)).start()
        return jsonify({"ok": True})

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    device   = str(data.get("device",   "")).strip()
    event    = str(data.get("event",    "")).strip().lower()
    ip       = str(data.get("ip",       "")).strip()
    location = str(data.get("location", "")).strip()
    group    = str(data.get("group",    "عام")).strip()
    message  = str(data.get("message",  "")).strip()

    if not device or event not in ("down","up"):
        return jsonify({"error": "invalid"}), 400

    ensure_device(device, ip, location, group)
    db_log_event(device, event, message, ip)
    d       = get_device(device)
    dev_ip  = ip or (d["ip"] if d else "—")
    dev_loc = location or (d["location"] if d else "—")

    if event == "down":
        db_open_outage(device, dev_ip)
        send("🚨 *انقطاع!*\n\n📡 *" + device + "*  |  `" + dev_ip + "`\n📍 " + (dev_loc or "—") + "\n💬 " + (message or "Link Down") + "\n🕒 " + datetime.now().strftime('%I:%M:%S %p'))
    else:
        secs = db_close_outage(device)
        send("✅ *عاد للاتصال!*\n\n📡 *" + device + "*  |  `" + dev_ip + "`\n📍 " + (dev_loc or "—") + "\n⏱ مدة الانقطاع: *" + fmt_dur(secs) + "*\n🕒 " + datetime.now().strftime('%I:%M:%S %p'))

    return jsonify({"ok": True})

@app.route("/tgwebhook", methods=["POST"])
def tg_webhook():
    threading.Thread(target=handle_bot, args=(request.json or {},)).start()
    return jsonify({"ok": True})

@app.route("/setup_webhook")
def setup_webhook():
    base = request.host_url.rstrip("/")
    r = requests.post(
        "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/setWebhook",
        json={"url": base + "/tgwebhook"}
    )
    return jsonify(r.json())

@app.route("/ping")
def ping():
    return "ok", 200

@app.route("/api/devices")
def api_devices():
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    return jsonify([{
        "name": d["name"], "ip": d["ip"],
        "status": "down" if d["name"] in active else "up"
    } for d in devices])


# ══════════════════════════════════════════
#  التشغيل
# ══════════════════════════════════════════
init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(report_daily,   'cron', hour=23, minute=0)
scheduler.add_job(report_weekly,  'cron', day_of_week='fri', hour=9, minute=0)
scheduler.add_job(report_monthly, 'cron', day=1, hour=8, minute=0)
scheduler.start()

send("🚀 *النظام يعمل على Render.com*\n\n🔗 جاهز لاستقبال اشعارات Dude\n📱 الأجهزة تُضاف تلقائياً\n🕒 " + datetime.now().strftime('%Y-%m-%d %I:%M:%S %p'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
