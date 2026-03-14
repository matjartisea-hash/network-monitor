#!/usr/bin/env python3
"""
==============================================
  نظام مراقبة الشبكة — Dude + Render.com
  
  المميزات:
  ✅ يستقبل إشعارات من The Dude مباشرة
  ✅ يضيف الأجهزة تلقائياً عند أول إشعار
  ✅ تنبيهات فورية انقطاع / عودة
  ✅ تقارير يومية / أسبوعية / شهرية
  ✅ أوامر بوت تيليغرام
  ✅ لوحة حالة على المتصفح
  ✅ إحصائيات متقدمة
==============================================
"""

import os, sqlite3, asyncio, threading, schedule, time, logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# عتبة التنبيه عن كثرة الانقطاعات
HIGH_OUTAGE_THRESHOLD = 5   # إذا انقطع جهاز أكثر من 5 مرات في اليوم يرسل تحذير


# ══════════════════════════════════════════
#  قاعدة البيانات
# ══════════════════════════════════════════
DB_FILE = "monitor.db"

def get_db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    db = get_db()
    db.executescript("""
        -- الأجهزة (تُضاف تلقائياً)
        CREATE TABLE IF NOT EXISTS devices (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL UNIQUE,
            ip         TEXT    NOT NULL DEFAULT '',
            location   TEXT    NOT NULL DEFAULT '',
            group_name TEXT    NOT NULL DEFAULT 'عام',
            added_at   TEXT    NOT NULL,
            active     INTEGER NOT NULL DEFAULT 1
        );

        -- الانقطاعات
        CREATE TABLE IF NOT EXISTS outages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device       TEXT NOT NULL,
            ip           TEXT NOT NULL DEFAULT '',
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            duration_sec INTEGER,
            resolved     INTEGER DEFAULT 0
        );

        -- سجل كل الأحداث
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device     TEXT NOT NULL,
            event      TEXT NOT NULL,
            message    TEXT,
            ip         TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- ملاحظات على الأجهزة
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device     TEXT NOT NULL,
            note       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    db.commit()
    db.close()
    logging.info("✅ قاعدة البيانات جاهزة")


# ──────────────────────────────────────────
#  دوال مساعدة
# ──────────────────────────────────────────
def _now(): return datetime.now().isoformat(timespec='seconds')
def _dt(s): return datetime.fromisoformat(s)

def fmt_dur(secs):
    if not secs or secs < 0: return "—"
    if secs < 60:    return f"{int(secs)} ثانية"
    if secs < 3600:  return f"{int(secs)//60} دقيقة"
    if secs < 86400: return f"{int(secs)//3600} ساعة و{(int(secs)%3600)//60} دقيقة"
    return f"{int(secs)//86400} يوم و{(int(secs)%86400)//3600} ساعة"


# ──────────────────────────────────────────
#  إدارة الأجهزة — تسجيل تلقائي
# ──────────────────────────────────────────
def ensure_device(name: str, ip: str = "", location: str = "", group: str = "عام"):
    """يضيف الجهاز تلقائياً إذا لم يكن موجوداً"""
    db = get_db()
    row = db.execute("SELECT id FROM devices WHERE name=?", (name,)).fetchone()
    if not row:
        db.execute(
            "INSERT INTO devices (name,ip,location,group_name,added_at) VALUES (?,?,?,?,?)",
            (name, ip, location, group, _now())
        )
        db.commit()
        logging.info(f"📱 جهاز جديد أُضيف تلقائياً: {name} ({ip})")
        # إشعار بجهاز جديد
        send(f"📱 *جهاز جديد أُضيف للنظام!*\n\n"
             f"🔖 الاسم: *{name}*\n"
             f"🌐 IP: `{ip}`\n"
             f"📍 الموقع: {location or 'غير محدد'}\n"
             f"🕒 {datetime.now().strftime('%H:%M:%S')}")
    elif ip:
        # تحديث IP إذا تغير
        db.execute("UPDATE devices SET ip=? WHERE name=? AND ip=''", (ip, name))
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


# ──────────────────────────────────────────
#  دوال قاعدة البيانات
# ──────────────────────────────────────────
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
    db.close()
    return row[0] or 0

def db_add_note(device, note):
    db = get_db()
    db.execute("INSERT INTO notes (device,note,created_at) VALUES (?,?,?)", (device, note, _now()))
    db.commit(); db.close()

def db_get_notes(device):
    db = get_db()
    rows = db.execute("SELECT note,created_at FROM notes WHERE device=? ORDER BY created_at DESC LIMIT 5", (device,)).fetchall()
    db.close(); return rows


# ══════════════════════════════════════════
#  إرسال تيليغرام
# ══════════════════════════════════════════
def send(text: str):
    async def _send():
        try:
            bot = Bot(token=TELEGRAM_TOKEN)
            for i in range(0, len(text), 4000):
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[i:i+4000], parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Telegram error: {e}")
    try:
        asyncio.run(_send())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_send())


# ══════════════════════════════════════════
#  التقارير
# ══════════════════════════════════════════
def report_daily():
    since   = datetime.now() - timedelta(days=1)
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}

    msg = f"📊 *تقرير الشبكة اليوم*\n📅 {datetime.now().strftime('%Y-%m-%d')}\n{'─'*28}\n\n"

    total_outages = 0
    for d in devices:
        n    = d["name"]
        c    = db_count_outages(n, since)
        avg  = db_avg_duration(n, since)
        total_outages += c
        icon = "🔴" if n in active else "🟢"
        msg += f"{icon} *{n}*"
        if d["location"]: msg += f"  _{d['location']}_"
        msg += f"\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"

    msg += f"📈 إجمالي انقطاعات اليوم: *{total_outages}*\n"

    if active:
        msg += f"\n🔴 *متوقفة الآن ({len(active)}):*\n"
        for r in db_active_outages():
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   • *{r['device']}* — منذ {fmt_dur(secs)}\n"

    send(msg)
    _check_high_outages(since)   # تنبيه إذا في أجهزة مشكلة

def report_weekly():
    since   = datetime.now() - timedelta(days=7)
    devices = get_all_devices()

    msg = (f"📅 *تقرير الأسبوع*\n"
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

    msg = f"📈 *تقرير الشهر* (آخر 30 يوم)\n{'─'*28}\n\n"

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
        msg += "⚠️ *أكثر الأجهزة انقطاعاً (الشهر):*\n"
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
    """تحذير إذا جهاز انقطع كثيراً"""
    for row in db_top_outages(since, limit=5):
        if row["c"] >= HIGH_OUTAGE_THRESHOLD:
            send(
                f"⚠️ *تحذير: جهاز يعاني من مشكلة متكررة!*\n\n"
                f"📡 *{row['device']}*\n"
                f"⚡ انقطع *{row['c']}* مرات خلال آخر 24 ساعة\n"
                f"🔧 يُنصح بمراجعة الجهاز"
            )


# ══════════════════════════════════════════
#  Flask — يستقبل من Dude
# ══════════════════════════════════════════
flask_app = Flask(__name__)

# ── لوحة حالة HTML ──
STATUS_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>حالة الشبكة</title>
<style>
  body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
  h1{color:#38bdf8;text-align:center;margin-bottom:5px}
  .sub{text-align:center;color:#64748b;margin-bottom:30px;font-size:13px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
  .card{background:#1e293b;border-radius:12px;padding:18px;border:1px solid #334155}
  .card.down{border-color:#ef4444;background:#2d1515}
  .card.up{border-color:#22c55e}
  .name{font-size:18px;font-weight:bold;margin-bottom:8px}
  .ip{color:#94a3b8;font-size:13px;margin-bottom:6px}
  .status{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:bold}
  .status.up{background:#14532d;color:#4ade80}
  .status.down{background:#450a0a;color:#f87171}
  .dur{color:#fbbf24;font-size:12px;margin-top:6px}
  .loc{color:#94a3b8;font-size:12px}
  .stats{text-align:center;margin-top:30px;display:flex;justify-content:center;gap:30px}
  .stat{background:#1e293b;padding:15px 30px;border-radius:12px}
  .stat-n{font-size:32px;font-weight:bold;color:#38bdf8}
  .stat-l{font-size:13px;color:#64748b}
</style>
</head>
<body>
<h1>🌐 لوحة مراقبة الشبكة</h1>
<p class="sub">آخر تحديث: {{ now }} — تتجدد كل 30 ثانية</p>
<div class="stats">
  <div class="stat"><div class="stat-n">{{ total }}</div><div class="stat-l">إجمالي الأجهزة</div></div>
  <div class="stat"><div class="stat-n" style="color:#4ade80">{{ up_count }}</div><div class="stat-l">متصلة</div></div>
  <div class="stat"><div class="stat-n" style="color:#f87171">{{ down_count }}</div><div class="stat-l">منقطعة</div></div>
</div>
<br>
<div class="grid">
{% for d in devices %}
<div class="card {{ d.status }}">
  <div class="name">📡 {{ d.name }}</div>
  <div class="ip">🌐 {{ d.ip or '—' }}</div>
  {% if d.location %}<div class="loc">📍 {{ d.location }}</div>{% endif %}
  <div class="loc">🗂 {{ d.group }}</div>
  <br>
  <span class="status {{ d.status }}">{{ '🔴 منقطع' if d.status == 'down' else '🟢 متصل' }}</span>
  {% if d.status == 'down' %}<div class="dur">⏱ منذ {{ d.duration }}</div>{% endif %}
</div>
{% endfor %}
</div>
</body>
</html>
"""

@flask_app.route("/", methods=["GET"])
def dashboard():
    devices   = get_all_devices()
    active_d  = {r["device"]: r for r in db_active_outages()}
    out       = []
    for d in devices:
        name = d["name"]
        is_down = name in active_d
        dur = ""
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
        devices=out, total=total, up_count=total-down,
        down_count=down, now=datetime.now().strftime("%H:%M:%S"))


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    """
    Dude يرسل هنا عند كل حدث.
    
    البيانات المتوقعة:
    {
      "secret":   "mysecret123",
      "device":   "Tower-1",
      "event":    "down",          ← أو "up"
      "ip":       "10.0.0.12",    ← اختياري
      "location": "البرج الأول",  ← اختياري
      "group":    "أبراج",        ← اختياري
      "message":  "Link Down"     ← اختياري
    }
    """
    data = request.json or {}

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

    # ── تسجيل الجهاز تلقائياً ──
    ensure_device(device, ip, location, group)
    db_log_event(device, event, message, ip)

    d = get_device(device)
    dev_ip  = ip or (d["ip"] if d else "—")
    dev_loc = location or (d["location"] if d else "—")

    if event == "down":
        db_open_outage(device, dev_ip)
        send(
            f"🚨 *انقطاع!*\n\n"
            f"📡 *{device}*  |  `{dev_ip}`\n"
            f"📍 {dev_loc or '—'}\n"
            f"💬 {message or 'Link Down'}\n"
            f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        )

    elif event == "up":
        secs = db_close_outage(device)
        dur  = fmt_dur(secs) if secs else "—"
        send(
            f"✅ *عاد للاتصال!*\n\n"
            f"📡 *{device}*  |  `{dev_ip}`\n"
            f"📍 {dev_loc or '—'}\n"
            f"⏱ مدة الانقطاع: *{dur}*\n"
            f"🕒 {datetime.now().strftime('%H:%M:%S')}"
        )

    return jsonify({"ok": True})


@flask_app.route("/api/devices", methods=["GET"])
def api_devices():
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    return jsonify([{
        "name": d["name"], "ip": d["ip"],
        "location": d["location"], "group": d["group_name"],
        "status": "down" if d["name"] in active else "up",
        "added_at": d["added_at"]
    } for d in devices])


# ══════════════════════════════════════════
#  أوامر البوت
# ══════════════════════════════════════════
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "👋 *مرحباً — نظام مراقبة الشبكة*\n\n"
        "📋 *الأوامر:*\n"
        "🔹 /status — حالة الأجهزة الآن\n"
        "🔹 /outages — الأجهزة المتوقفة\n"
        "🔹 /daily — تقرير اليوم\n"
        "🔹 /weekly — تقرير الأسبوع\n"
        "🔹 /monthly — تقرير الشهر\n"
        "🔹 /devices — قائمة كل الأجهزة\n"
        "🔹 /stats — إحصائيات سريعة\n"
        "🔹 /note اسم_الجهاز ملاحظة — إضافة ملاحظة",
        parse_mode="Markdown"
    )

async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    msg     = "📡 *حالة الأجهزة الآن:*\n\n"
    for d in devices:
        icon = "🔴" if d["name"] in active else "🟢"
        msg += f"{icon} *{d['name']}*  `{d['ip'] or '—'}`\n"
    await u.message.reply_text(msg, parse_mode="Markdown")

async def cmd_devices(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    devices = get_all_devices()
    msg     = f"📋 *قائمة الأجهزة ({len(devices)}):*\n\n"
    groups  = {}
    for d in devices:
        g = d["group_name"]
        groups.setdefault(g, []).append(d)
    for g, devs in groups.items():
        msg += f"🗂 *{g}*\n"
        for d in devs:
            msg += f"   • *{d['name']}*  `{d['ip'] or '—'}`"
            if d["location"]: msg += f"  _{d['location']}_"
            msg += "\n"
        msg += "\n"
    await u.message.reply_text(msg, parse_mode="Markdown")

async def cmd_stats(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    since24 = datetime.now() - timedelta(hours=24)
    since7  = datetime.now() - timedelta(days=7)
    devices = get_all_devices()
    active  = db_active_outages()

    total_24 = sum(db_count_outages(d["name"], since24) for d in devices)
    total_7  = sum(db_count_outages(d["name"], since7)  for d in devices)

    msg = (
        f"📊 *إحصائيات سريعة*\n\n"
        f"📡 إجمالي الأجهزة: *{len(devices)}*\n"
        f"🔴 متوقفة الآن: *{len(active)}*\n"
        f"🟢 تعمل الآن: *{len(devices)-len(active)}*\n\n"
        f"⚡ انقطاعات آخر 24 ساعة: *{total_24}*\n"
        f"⚡ انقطاعات آخر 7 أيام: *{total_7}*\n"
    )
    top = db_top_outages(since7, limit=3)
    if top:
        msg += "\n🏆 *أكثر الأجهزة انقطاعاً (أسبوع):*\n"
        for row in top:
            msg += f"   • {row['device']}: {row['c']} انقطاع\n"
    await u.message.reply_text(msg, parse_mode="Markdown")

async def cmd_note(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await u.message.reply_text("الاستخدام: /note اسم_الجهاز الملاحظة")
        return
    device = args[0]
    note   = " ".join(args[1:])
    db_add_note(device, note)
    await u.message.reply_text(f"✅ تم حفظ الملاحظة على *{device}*", parse_mode="Markdown")

async def cmd_outages(u, ctx):
    threading.Thread(target=report_active).start()
async def cmd_daily(u, ctx):
    threading.Thread(target=report_daily).start()
async def cmd_weekly(u, ctx):
    threading.Thread(target=report_weekly).start()
async def cmd_monthly(u, ctx):
    threading.Thread(target=report_monthly).start()


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
        while True: schedule.run_pending(); time.sleep(30)
    threading.Thread(target=tick, daemon=True).start()
    logging.info("📅 التقارير المجدولة تعمل")


# ══════════════════════════════════════════
#  تشغيل البوت
# ══════════════════════════════════════════
def run_bot():
    async def _run():
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        for cmd, fn in [
            ("start",   cmd_start),   ("help",    cmd_start),
            ("status",  cmd_status),  ("outages", cmd_outages),
            ("daily",   cmd_daily),   ("weekly",  cmd_weekly),
            ("monthly", cmd_monthly), ("devices", cmd_devices),
            ("stats",   cmd_stats),   ("note",    cmd_note),
        ]:
            app.add_handler(CommandHandler(cmd, fn))
        await app.run_polling()
    asyncio.run(_run())


# ══════════════════════════════════════════
#  نقطة البداية
# ══════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        handlers=[logging.FileHandler("monitor.log", encoding="utf-8"), logging.StreamHandler()],
    )
    init_db()
    start_scheduler()
    threading.Thread(target=run_bot, daemon=True).start()
    send(
        f"🚀 *النظام يعمل على Render.com*\n\n"
        f"🔗 جاهز لاستقبال إشعارات Dude\n"
        f"📱 الأجهزة تُضاف تلقائياً\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    flask_app.run(host="0.0.0.0", port=PORT)
