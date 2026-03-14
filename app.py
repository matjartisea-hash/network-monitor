import os, sqlite3, logging, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8583886234:AAEPcKBCyH0823cO4WYXc9dx0CObYfbo2Zs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1995981496")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "mysecret123")
HIGH_OUTAGE_THRESHOLD = 5

DB_FILE = "/tmp/monitor.db"

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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ip TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            group_name TEXT NOT NULL DEFAULT 'عام',
            added_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS outages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_sec INTEGER,
            resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            event TEXT NOT NULL,
            message TEXT,
            ip TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()

def _now(): return datetime.now().isoformat(timespec='seconds')
def _dt(s): return datetime.fromisoformat(s)

def fmt_dur(secs):
    if not secs or secs < 0: return "—"
    secs = int(secs)
    if secs < 60:    return f"{secs} ثانية"
    if secs < 3600:  return f"{secs//60} دقيقة"
    if secs < 86400: return f"{secs//3600} ساعة و{(secs%3600)//60} دقيقة"
    return f"{secs//86400} يوم و{(secs%86400)//3600} ساعة"

# ══════════════════════════════════════════
#  تيليغرام
# ══════════════════════════════════════════
def send(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        for i in range(0, len(text), 4000):
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[i:i+4000],
                "parse_mode": "Markdown"
            }, timeout=10)
    except Exception as e:
        logging.error(f"Telegram: {e}")

# ══════════════════════════════════════════
#  الأجهزة
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
        send(f"📱 *جهاز جديد أُضيف تلقائياً!*\n\n🔖 *{name}*\n🌐 `{ip or '—'}`\n📍 {location or 'غير محدد'}")
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
#  الانقطاعات
# ══════════════════════════════════════════
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
    row = db.execute("SELECT AVG(duration_sec) FROM outages WHERE device=? AND started_at>=? AND resolved=1",
                     (device, since_dt.isoformat())).fetchone()
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
    msg     = f"📊 *تقرير الشبكة اليوم*\n📅 {datetime.now().strftime('%Y-%m-%d')}\n{'─'*25}\n\n"
    for d in devices:
        n = d["name"]; c = db_count_outages(n, since); avg = db_avg_duration(n, since)
        total += c
        icon   = "🔴" if n in active else "🟢"
        msg   += f"{icon} *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg   += "\n\n"
    msg += f"📈 الإجمالي: *{total}* انقطاع\n"
    al = db_active_outages()
    if al:
        msg += f"\n🔴 *متوقفة الآن ({len(al)}):*\n"
        for r in al:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   • *{r['device']}* — منذ {fmt_dur(secs)}\n"
    send(msg)
    for row in db_top_outages(since, 5):
        if row["c"] >= HIGH_OUTAGE_THRESHOLD:
            send(f"⚠️ *تحذير!*\n📡 *{row['device']}* انقطع *{row['c']}* مرات اليوم!")

def report_weekly():
    since = datetime.now() - timedelta(days=7)
    devices = get_all_devices()
    msg = f"📅 *تقرير الأسبوع*\n📆 {since.strftime('%Y-%m-%d')} ← {datetime.now().strftime('%Y-%m-%d')}\n{'─'*25}\n\n"
    for d in devices:
        n = d["name"]; c = db_count_outages(n, since); avg = db_avg_duration(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i,row in enumerate(top): msg += f"   {medals[i] if i<10 else '•'} {row['device']} — *{row['c']}* انقطاع\n"
    send(msg)

def report_monthly():
    since = datetime.now() - timedelta(days=30)
    devices = get_all_devices()
    msg = f"📈 *تقرير الشهر* (آخر 30 يوم)\n{'─'*25}\n\n"
    for d in devices:
        n = d["name"]; c = db_count_outages(n, since); avg = db_avg_duration(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i,row in enumerate(top): msg += f"   {medals[i] if i<10 else '•'} {row['device']} — *{row['c']}* انقطاع\n"
    al = db_active_outages()
    if al:
        msg += "\n🔴 *متوقفة الآن:*\n"
        for r in al:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   ❌ *{r['device']}* — {fmt_dur(secs)}\n"
    send(msg)

def report_active():
    al = db_active_outages()
    if not al:
        send("✅ *جميع الأجهزة تعمل بشكل طبيعي* 🟢"); return
    msg = f"🔴 *الأجهزة المتوقفة الآن ({len(al)}):*\n\n"
    for r in al:
        secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
        msg += f"❌ *{r['device']}*  `{r['ip']}`\n   ⏱ منذ: {fmt_dur(secs)}\n\n"
    send(msg)

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
                 "🔹 /status — حالة الأجهزة\n"
                 "🔹 /outages — المتوقفة الآن\n"
                 "🔹 /devices — قائمة الأجهزة\n"
                 "🔹 /stats — إحصائيات\n"
                 "🔹 /daily — تقرير اليوم\n"
                 "🔹 /weekly — تقرير الأسبوع\n"
                 "🔹 /monthly — تقرير الشهر\n"
                 "🔹 /note جهاز ملاحظة")
        elif cmd == "status":
            devices = get_all_devices()
            active  = {r["device"] for r in db_active_outages()}
            msg     = "📡 *حالة الأجهزة:*\n\n"
            if not devices: msg += "لا يوجد أجهزة بعد ⏳"
            for d in devices:
                msg += f"{'🔴' if d['name'] in active else '🟢'} *{d['name']}*  `{d['ip'] or '—'}`\n"
            send(msg)
        elif cmd == "outages":   report_active()
        elif cmd == "daily":     report_daily()
        elif cmd == "weekly":    report_weekly()
        elif cmd == "monthly":   report_monthly()
        elif cmd == "devices":
            devices = get_all_devices()
            msg     = f"📋 *الأجهزة ({len(devices)}):*\n\n"
            groups  = {}
            for d in devices: groups.setdefault(d["group_name"],[]).append(d)
            for g,devs in groups.items():
                msg += f"🗂 *{g}*\n"
                for d in devs:
                    msg += f"   • *{d['name']}*  `{d['ip'] or '—'}`"
                    if d["location"]: msg += f"  _{d['location']}_"
                    msg += "\n"
                msg += "\n"
            send(msg)
        elif cmd == "stats":
            s24 = datetime.now()-timedelta(hours=24); s7 = datetime.now()-timedelta(days=7)
            devices = get_all_devices(); active = db_active_outages()
            t24 = sum(db_count_outages(d["name"],s24) for d in devices)
            t7  = sum(db_count_outages(d["name"],s7)  for d in devices)
            msg = (f"📊 *إحصائيات*\n\n"
                   f"📡 الأجهزة: *{len(devices)}*\n"
                   f"🔴 متوقفة: *{len(active)}*\n"
                   f"🟢 تعمل: *{len(devices)-len(active)}*\n\n"
                   f"⚡ انقطاعات 24 ساعة: *{t24}*\n"
                   f"⚡ انقطاعات 7 أيام: *{t7}*")
            top = db_top_outages(s7,3)
            if top:
                msg += "\n\n🏆 *الأكثر انقطاعاً:*\n"
                for r in top: msg += f"   • {r['device']}: {r['c']} انقطاع\n"
            send(msg)
        elif cmd == "note":
            if len(args) >= 2:
                db_add_note(args[0]," ".join(args[1:]))
                send(f"✅ تم حفظ الملاحظة على *{args[0]}*")
            else:
                send("الاستخدام: /note اسم_الجهاز الملاحظة")
    except Exception as e:
        logging.error(f"Bot: {e}")

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
.stats{display:flex;justify-content:center;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:#1e293b;padding:12px 24px;border-radius:12px;text-align:center}
.sn{font-size:28px;font-weight:bold;color:#38bdf8}
.sn.r{color:#f87171}.sn.g{color:#4ade80}
.sl{font-size:12px;color:#64748b;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.card{background:#1e293b;border-radius:10px;padding:14px;border:1px solid #334155}
.card.down{border-color:#ef4444;background:#1f0f0f}
.card.up{border-color:#22c55e}
.name{font-size:15px;font-weight:bold;margin-bottom:5px}
.info{color:#94a3b8;font-size:12px;margin-bottom:3px}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:bold;margin-top:6px}
.badge.up{background:#14532d;color:#4ade80}
.badge.down{background:#450a0a;color:#f87171}
.dur{color:#fbbf24;font-size:12px;margin-top:4px}
.empty{text-align:center;color:#64748b;padding:40px;grid-column:1/-1}
</style></head>
<body>
<h1>🌐 لوحة مراقبة الشبكة</h1>
<p class="sub">آخر تحديث: {{now}} — تتجدد كل 30 ثانية</p>
<div class="stats">
  <div class="stat"><div class="sn">{{total}}</div><div class="sl">إجمالي</div></div>
  <div class="stat"><div class="sn g">{{up}}</div><div class="sl">متصلة 🟢</div></div>
  <div class="stat"><div class="sn r">{{down}}</div><div class="sl">منقطعة 🔴</div></div>
</div>
<div class="grid">
{{cards}}
{{empty}}
</div></body></html>"""

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
        dur_html = f'<div class="dur">⏱ منذ {dur}</div>' if is_down else ""
        loc_html = f'<div class="info">📍 {d["location"]}</div>' if d["location"] else ""
        cards += f"""<div class="card {status}">
  <div class="name">📡 {name}</div>
  <div class="info">🌐 {d['ip'] or '—'}</div>
  {loc_html}
  <div class="info">🗂 {d['group_name']}</div>
  <span class="badge {status}">{badge}</span>
  {dur_html}
</div>"""
    total = len(devices)
    empty = '<div class="empty">لا يوجد أجهزة بعد — ستُضاف تلقائياً عند أول إشعار من Dude ⏳</div>' if not devices else ""
    html  = HTML.replace("{{now}}",   datetime.now().strftime("%I:%M:%S %p"))
    html  = html.replace("{{total}}", str(total))
    html  = html.replace("{{up}}",    str(total - down_cnt))
    html  = html.replace("{{down}}",  str(down_cnt))
    html  = html.replace("{{cards}}", cards)
    html  = html.replace("{{empty}}", empty)
    return html

# ══════════════════════════════════════════
#  Flask
# ══════════════════════════════════════════
app = Flask(__name__)

@app.route("/")
def dashboard():
    return build_dashboard()

@app.route("/webhook", methods=["POST", "GET"])
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

    # أوامر تيليغرام
    if "update_id" in data:
        handle_bot(data)
        return jsonify({"ok": True})

    # إشعارات Dude
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
    d      = get_device(device)
    dev_ip = ip or (d["ip"] if d else "—")
    dev_loc= location or (d["location"] if d else "—")

    if event == "down":
        db_open_outage(device, dev_ip)
        send(f"🚨 *انقطاع!*\n\n📡 *{device}*  |  `{dev_ip}`\n📍 {dev_loc or '—'}\n💬 {message or 'Link Down'}\n🕒 {datetime.now().strftime('%I:%M:%S %p')}")
    else:
        secs = db_close_outage(device)
        send(f"✅ *عاد للاتصال!*\n\n📡 *{device}*  |  `{dev_ip}`\n📍 {dev_loc or '—'}\n⏱ مدة الانقطاع: *{fmt_dur(secs)}*\n🕒 {datetime.now().strftime('%I:%M:%S %p')}")

    return jsonify({"ok": True})

@app.route("/tgwebhook", methods=["POST"])
def tg_webhook():
    handle_bot(request.json or {})
    return jsonify({"ok": True})

@app.route("/setup_webhook")
def setup_webhook():
    base = request.host_url.rstrip("/")
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": f"{base}/tgwebhook"}
    )
    return jsonify(r.json())

@app.route("/api/devices")
def api_devices():
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    return jsonify([{"name":d["name"],"ip":d["ip"],"status":"down" if d["name"] in active else "up"} for d in devices])

@app.route("/ping")
def ping():
    return "ok", 200

# ══════════════════════════════════════════
#  التشغيل
# ══════════════════════════════════════════
init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(report_daily,   'cron', hour=23, minute=0)
scheduler.add_job(report_weekly,  'cron', day_of_week='fri', hour=9, minute=0)
scheduler.add_job(report_monthly, 'cron', day=1, hour=8, minute=0)
scheduler.start()

send(f"🚀 *النظام يعمل على Render.com*\n\n🔗 جاهز لاستقبال إشعارات Dude\n📱 الأجهزة تُضاف تلقائياً\n🕒 {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
