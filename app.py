import os, logging, requests, threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8583886234:AAEPcKBCyH0823cO4WYXc9dx0CObYfbo2Zs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1995981496")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "mysecret123")
PORT             = int(os.getenv("PORT", 5000))

SUPABASE_URL     = os.getenv("SUPABASE_URL",     "https://rlkoxhtayeylugxqynna.supabase.co")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY",     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJsa294aHRheWV5bHVneHF5bm5hIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM3NzQyNTcsImV4cCI6MjA4OTM1MDI1N30.f2AWuKUwWnEkYVlcgLxuMX2MvbBa0zMwZB8rl4RNr3w")

HIGH_OUTAGE_THRESHOLD = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ══════════════════════════════════════════
#  Supabase REST API
# ══════════════════════════════════════════
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def sb_get(table, params=""):
    url = SUPABASE_URL + "/rest/v1/" + table + "?" + params
    r = requests.get(url, headers=sb_headers(), timeout=10)
    return r.json() if r.ok else []

def sb_post(table, data):
    url = SUPABASE_URL + "/rest/v1/" + table
    r = requests.post(url, headers=sb_headers(), json=data, timeout=10)
    return r.json() if r.ok else None

def sb_patch(table, params, data):
    url = SUPABASE_URL + "/rest/v1/" + table + "?" + params
    r = requests.patch(url, headers=sb_headers(), json=data, timeout=10)
    return r.ok

def sb_delete(table, params):
    url = SUPABASE_URL + "/rest/v1/" + table + "?" + params
    r = requests.delete(url, headers=sb_headers(), timeout=10)
    return r.ok

def init_db():
    """إنشاء الجداول في Supabase عبر SQL"""
    sql = """
    CREATE TABLE IF NOT EXISTS devices (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        ip TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT 'عام',
        added_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS outages (
        id SERIAL PRIMARY KEY,
        device TEXT NOT NULL,
        ip TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        ended_at TEXT,
        duration_sec INTEGER,
        resolved INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
        device TEXT NOT NULL,
        event TEXT NOT NULL,
        message TEXT,
        ip TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notes (
        id SERIAL PRIMARY KEY,
        device TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    url = SUPABASE_URL + "/rest/v1/rpc/exec_sql"
    # نتحقق من الاتصال فقط
    r = requests.get(SUPABASE_URL + "/rest/v1/devices?limit=1", headers=sb_headers(), timeout=10)
    if r.status_code == 200:
        logging.info("Supabase متصل")
    else:
        logging.warning("Supabase: " + str(r.status_code))


# ══════════════════════════════════════════
#  دوال مساعدة
# ══════════════════════════════════════════
def _now(): return datetime.now().isoformat(timespec='seconds')
def _dt(s): return datetime.fromisoformat(s.replace("Z",""))

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
    rows = sb_get("devices", "name=eq." + name + "&limit=1")
    if not rows:
        sb_post("devices", {
            "name": name, "ip": ip,
            "location": location, "group_name": group,
            "added_at": _now(), "active": 1
        })
        send("📱 *جهاز جديد اضيف تلقائياً!*\n\n🔖 *" + name + "*\n🌐 `" + (ip or "—") + "`\n📍 " + (location or "غير محدد"))
    elif ip and not rows[0].get("ip"):
        sb_patch("devices", "name=eq." + name, {"ip": ip})

def get_all_devices():
    return sb_get("devices", "active=eq.1&order=name")

def get_device(name):
    rows = sb_get("devices", "name=eq." + name + "&limit=1")
    return rows[0] if rows else None

def db_open_outage(device, ip):
    existing = sb_get("outages", "device=eq." + device + "&resolved=eq.0&limit=1")
    if not existing:
        sb_post("outages", {
            "device": device, "ip": ip,
            "started_at": _now(), "resolved": 0
        })

def db_close_outage(device):
    rows = sb_get("outages", "device=eq." + device + "&resolved=eq.0&limit=1")
    if not rows: return None
    row    = rows[0]
    ended  = _now()
    secs   = int((_dt(ended) - _dt(row["started_at"])).total_seconds())
    sb_patch("outages", "id=eq." + str(row["id"]), {
        "ended_at": ended, "duration_sec": secs, "resolved": 1
    })
    return secs

def db_log_event(device, event, message="", ip=""):
    sb_post("events", {
        "device": device, "event": event,
        "message": message, "ip": ip, "created_at": _now()
    })

def db_count_outages(device, since_dt):
    rows = sb_get("outages", "device=eq." + device + "&started_at=gte." + since_dt.isoformat() + "&select=id")
    return len(rows)

def db_top_outages(since_dt, limit=10):
    rows = sb_get("outages", "started_at=gte." + since_dt.isoformat() + "&select=device")
    counts = {}
    for r in rows:
        d = r["device"]
        counts[d] = counts.get(d, 0) + 1
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [{"device": d, "c": c} for d, c in sorted_counts]

def db_active_outages():
    return sb_get("outages", "resolved=eq.0&order=started_at")

def db_avg_duration(device, since_dt):
    rows = sb_get("outages", "device=eq." + device + "&started_at=gte." + since_dt.isoformat() + "&resolved=eq.1&select=duration_sec")
    if not rows: return 0
    vals = [r["duration_sec"] for r in rows if r.get("duration_sec")]
    return sum(vals)/len(vals) if vals else 0

def db_add_note(device, note):
    sb_post("notes", {"device": device, "note": note, "created_at": _now()})


# ══════════════════════════════════════════
#  التقارير
# ══════════════════════════════════════════
def report_daily():
    since   = datetime.now() - timedelta(days=1)
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    total   = 0
    sep     = "─" * 25

    msg = "📊 *تقرير الشبكة اليوم*\n📅 " + datetime.now().strftime('%Y-%m-%d') + "\n" + sep + "\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        total += c
        icon  = "🔴" if n in active else "🟢"
        msg  += icon + " *" + n + "*"
        if d.get("location"): msg += "  _" + d["location"] + "_"
        msg  += "\n   ⚡ انقطاعات: *" + str(c) + "*"
        if avg > 0: msg += "  |  ⏱ متوسط: " + fmt_dur(avg)
        msg  += "\n\n"

    msg += "📈 اجمالي: *" + str(total) + "* انقطاع\n"

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
            send("⚠️ *تحذير: جهاز يعاني من مشكلة متكررة!*\n\n📡 *" + row["device"] + "*\n⚡ انقطع *" + str(row["c"]) + "* مرات خلال آخر 24 ساعة")


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
                 "🔹 /devices — قائمة الأجهزة\n"
                 "🔹 /stats — احصائيات سريعة\n"
                 "🔹 /daily — تقرير اليوم\n"
                 "🔹 /weekly — تقرير الأسبوع\n"
                 "🔹 /monthly — تقرير الشهر\n"
                 "🔹 /note جهاز ملاحظة")

        elif cmd == "status":
            devices = get_all_devices()
            active  = {r["device"] for r in db_active_outages()}
            msg     = "📡 *حالة الأجهزة الآن:*\n\n"
            if not devices: msg += "لا يوجد أجهزة بعد ⏳"
            for d in devices:
                icon = "🔴" if d["name"] in active else "🟢"
                msg += icon + " *" + d["name"] + "*  `" + (d.get("ip") or "—") + "`\n"
            send(msg)

        elif cmd == "outages":  report_active()
        elif cmd == "daily":    report_daily()
        elif cmd == "weekly":   report_weekly()
        elif cmd == "monthly":  report_monthly()

        elif cmd == "devices":
            devices = get_all_devices()
            msg     = "📋 *قائمة الأجهزة (" + str(len(devices)) + "):*\n\n"
            groups  = {}
            for d in devices: groups.setdefault(d.get("group_name","عام"), []).append(d)
            for g, devs in groups.items():
                msg += "🗂 *" + g + "*\n"
                for d in devs:
                    msg += "   • *" + d["name"] + "*  `" + (d.get("ip") or "—") + "`\n"
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
        loc_html = '<div class="ip">📍 ' + d.get("location","") + '</div>' if d.get("location") else ""
        cards += '<div class="card ' + status + '"><div class="name">📡 ' + name + '</div>'
        cards += '<div class="ip">🌐 ' + (d.get("ip") or "—") + '</div>'
        cards += loc_html
        cards += '<span class="badge ' + status + '">' + badge + '</span>' + dur_html + '</div>'

    total = len(devices)
    if not devices:
        cards = '<div class="empty">لا يوجد أجهزة بعد</div>'

    html = HTML.replace("NOW",      datetime.now().strftime("%I:%M:%S %p"))
    html = html.replace("TOTAL",    str(total))
    html = html.replace("UPCOUNT",  str(total - down_cnt))
    html = html.replace("DOWNCOUNT",str(down_cnt))
    html = html.replace("CARDS",    cards)
    return html


# ══════════════════════════════════════════
#  Flask
# ══════════════════════════════════════════
app = Flask(__name__)

@app.route("/")
def dashboard():
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
    dev_ip  = ip or (d.get("ip") if d else "—")
    dev_loc = location or (d.get("location") if d else "—")

    if event == "down":
        db_open_outage(device, dev_ip)
        send("🚨 *انقطاع!*\n\n📡 *" + device + "*  |  `" + dev_ip + "`\n📍 " + (dev_loc or "—") + "\n💬 " + (message or "Link Down") + "\n🕒 " + datetime.now().strftime('%I:%M:%S %p'))
    else:
        secs = db_close_outage(device)
        if secs is not None:
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


# ══════════════════════════════════════════
#  التشغيل
# ══════════════════════════════════════════
init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(report_daily,   'cron', hour=23, minute=0)
scheduler.add_job(report_weekly,  'cron', day_of_week='fri', hour=9, minute=0)
scheduler.add_job(report_monthly, 'cron', day=1, hour=8, minute=0)
scheduler.start()

send("🚀 *النظام يعمل مع Supabase*\n\n"
     "💾 البيانات محفوظة دائماً\n"
     "🔗 جاهز لاستقبال اشعارات Dude\n"
     "🕒 " + datetime.now().strftime('%Y-%m-%d %I:%M:%S %p'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
