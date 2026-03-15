import os, sqlite3, logging, requests, threading, schedule, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

# ─────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8583886234:AAEPcKBCyH0823cO4WYXc9dx0CObYfbo2Zs")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1995981496")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",   "mysecret123")
PORT             = int(os.getenv("PORT", 5000))
TZ               = os.getenv("TZ", "Asia/Riyadh")

# ── إعدادات الباقات ──
PACKAGES = {
    "100":    {"name": "أبو 100",   "quota_mb": 300,   "price": 100,  "commission": 10},
    "200":    {"name": "أبو 200",   "quota_mb": 700,   "price": 200,  "commission": 20},
    "500":    {"name": "أبو 500",   "quota_mb": 1500,  "price": 400,  "commission": 40},
    "100M":   {"name": "أبو 100M",  "quota_mb": 100,   "price": 100,  "commission": 10},
    "1000RY": {"name": "أبو 1000",  "quota_mb": 4000,  "price": 1000, "commission": 50},
}

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
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            count100 INTEGER DEFAULT 0,
            count200 INTEGER DEFAULT 0,
            count500 INTEGER DEFAULT 0,
            count100m INTEGER DEFAULT 0,
            count1000 INTEGER DEFAULT 0,
            unused100 INTEGER DEFAULT 0,
            unused200 INTEGER DEFAULT 0,
            unused500 INTEGER DEFAULT 0,
            unused100m INTEGER DEFAULT 0,
            unused1000 INTEGER DEFAULT 0,
            active_users INTEGER DEFAULT 0,
            rx_intr1 INTEGER DEFAULT 0,
            tx_intr1 INTEGER DEFAULT 0,
            rx_ppp1 INTEGER DEFAULT 0,
            tx_ppp1 INTEGER DEFAULT 0,
            rx_ppp2 INTEGER DEFAULT 0,
            tx_ppp2 INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS daily_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            pkg_100 INTEGER DEFAULT 0,
            pkg_200 INTEGER DEFAULT 0,
            pkg_500 INTEGER DEFAULT 0,
            pkg_100m INTEGER DEFAULT 0,
            pkg_1000 INTEGER DEFAULT 0,
            total_sales INTEGER DEFAULT 0,
            rx_bytes INTEGER DEFAULT 0,
            tx_bytes INTEGER DEFAULT 0
        );
    """)
    db.commit()
    db.close()


# ──────────────────────────────────────────
def _now(): return datetime.now().isoformat(timespec='seconds')
def _dt(s): return datetime.fromisoformat(s)
def _today(): return datetime.now().strftime('%Y-%m-%d')

def fmt_dur(secs):
    if not secs or secs < 0: return "—"
    secs = int(secs)
    if secs < 60:    return f"{secs} ثانية"
    if secs < 3600:  return f"{secs//60} دقيقة"
    if secs < 86400: return f"{secs//3600} ساعة و{(secs%3600)//60} دقيقة"
    return f"{secs//86400} يوم و{(secs%86400)//3600} ساعة"

def fmt_bytes(b):
    if not b: return "0 B"
    b = int(b)
    if b < 1024**2:  return f"{b/1024:.1f} KB"
    if b < 1024**3:  return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


# ══════════════════════════════════════════
#  إرسال تيليغرام
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
#  إدارة الأجهزة
# ══════════════════════════════════════════
def ensure_device(name, ip="", location="", group="عام"):
    db = get_db()
    row = db.execute("SELECT id,ip FROM devices WHERE name=?", (name,)).fetchone()
    if not row:
        db.execute("INSERT INTO devices (name,ip,location,group_name,added_at) VALUES (?,?,?,?,?)",
                   (name, ip, location, group, _now()))
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


# ══════════════════════════════════════════
#  حفظ إحصائيات الشبكة
# ══════════════════════════════════════════
def save_stats(data):
    db = get_db()
    db.execute("""
        INSERT INTO stats (recorded_at,count100,count200,count500,count100m,count1000,
            unused100,unused200,unused500,unused100m,unused1000,active_users,
            rx_intr1,tx_intr1,rx_ppp1,tx_ppp1,rx_ppp2,tx_ppp2)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        _now(),
        int(data.get("count100",0)), int(data.get("count200",0)),
        int(data.get("count500",0)), int(data.get("count100m",0)),
        int(data.get("count1000",0)),
        int(data.get("unused100",0)), int(data.get("unused200",0)),
        int(data.get("unused500",0)), int(data.get("unused100m",0)),
        int(data.get("unused1000",0)),
        int(data.get("active",0)),
        int(data.get("rx_intr1",0)), int(data.get("tx_intr1",0)),
        int(data.get("rx_ppp1",0)),  int(data.get("tx_ppp1",0)),
        int(data.get("rx_ppp2",0)),  int(data.get("tx_ppp2",0)),
    ))
    db.commit()
    db.close()

def get_latest_stats():
    db = get_db()
    row = db.execute("SELECT * FROM stats ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    return dict(row) if row else {}

def get_today_sales():
    db = get_db()
    today = _today()
    # أول إحصائية اليوم
    first = db.execute("SELECT * FROM stats WHERE recorded_at LIKE ? ORDER BY id ASC LIMIT 1",
                       (f"{today}%",)).fetchone()
    # آخر إحصائية اليوم
    last  = db.execute("SELECT * FROM stats WHERE recorded_at LIKE ? ORDER BY id DESC LIMIT 1",
                       (f"{today}%",)).fetchone()
    db.close()
    if not first or not last:
        return {}
    # الكروت المباعة = الفرق بين آخر وأول إحصائية
    sold = {
        "100":    max(0, last["count100"]  - first["count100"]  + last["unused100"]  - first["unused100"]),
        "200":    max(0, last["count200"]  - first["count200"]  + last["unused200"]  - first["unused200"]),
        "500":    max(0, last["count500"]  - first["count500"]  + last["unused500"]  - first["unused500"]),
        "100M":   max(0, last["count100m"] - first["count100m"] + last["unused100m"] - first["unused100m"]),
        "1000RY": max(0, last["count1000"] - first["count1000"] + last["unused1000"] - first["unused1000"]),
        "rx": max(0, last["rx_intr1"] - first["rx_intr1"]),
        "tx": max(0, last["tx_intr1"] - first["tx_intr1"]),
    }
    return sold

def get_best_sales_day():
    db = get_db()
    row = db.execute("SELECT MAX(total_sales) FROM daily_sales").fetchone()
    db.close()
    return row[0] or 0


# ══════════════════════════════════════════
#  حساب المبيعات والأرباح
# ══════════════════════════════════════════
def calc_sales_report(sold):
    total_cards    = 0
    total_revenue  = 0
    total_commission = 0
    lines = []

    for pkg_id, pkg in PACKAGES.items():
        count = sold.get(pkg_id, 0)
        if count == 0: continue
        revenue    = count * pkg["price"]
        commission = count * pkg["commission"]
        net        = revenue - commission
        total_cards     += count
        total_revenue   += revenue
        total_commission += commission
        lines.append(
            f"💳 *{pkg['name']}*\n"
            f"   الكروت: *{count}*  |  الإجمالي: *{revenue:,} ريال*\n"
            f"   العمولة: *{commission:,}*  |  الصافي: *{net:,} ريال*\n"
        )

    net_total = total_revenue - total_commission
    return lines, total_cards, total_revenue, total_commission, net_total


# ══════════════════════════════════════════
#  التقارير
# ══════════════════════════════════════════
def report_daily():
    since   = datetime.now() - timedelta(days=1)
    devices = get_all_devices()
    active  = {r["device"] for r in db_active_outages()}
    sold    = get_today_sales()
    stats   = get_latest_stats()
    total_out = 0

    msg = f"📡 *تقرير الشبكة اليوم*\n📅 {_today()}\n{'━'*25}\n\n"

    # ── استهلاك الشبكة ──
    rx = sold.get("rx", 0)
    tx = sold.get("tx", 0)
    msg += f"*استهلاك الشبكة*\n"
    msg += f"   ⬇️ التحميل: *{fmt_bytes(rx)}*\n"
    msg += f"   ⬆️ الرفع: *{fmt_bytes(tx)}*\n\n"
    msg += "━"*25 + "\n\n"

    # ── المبيعات ──
    lines, total_cards, total_rev, total_comm, net = calc_sales_report(sold)
    for line in lines:
        msg += line + "\n"

    msg += "━"*25 + "\n\n"
    msg += f"📊 *الإجمالي*\n"
    msg += f"   عدد الكروت: *{total_cards}*\n"
    msg += f"   المبيعات: *{total_rev:,} ريال*\n"
    msg += f"   العمولة: *{total_comm:,} ريال*\n"
    msg += f"   💰 صافي الربح: *{net:,} ريال*\n\n"

    # ── انقطاعات الأجهزة ──
    msg += "━"*25 + "\n\n"
    msg += "📡 *حالة الأجهزة*\n\n"
    for d in devices[:10]:  # أول 10 أجهزة
        n    = d["name"]
        c    = db_count_outages(n, since)
        total_out += c
        icon = "🔴" if n in active else "🟢"
        msg += f"{icon} *{n}*  ⚡ {c} انقطاع\n"

    msg += f"\n📈 إجمالي الانقطاعات: *{total_out}*\n"

    # ── أجهزة متوقفة ──
    al = db_active_outages()
    if al:
        msg += f"\n🔴 *متوقفة الآن ({len(al)}):*\n"
        for r in al:
            secs = int((datetime.now() - _dt(r["started_at"])).total_seconds())
            msg += f"   • *{r['device']}* — منذ {fmt_dur(secs)}\n"

    send(msg)
    _check_sales_alert(sold, net)

def _check_sales_alert(sold, net_today):
    best = get_best_sales_day()
    if not best: return

    rx = sold.get("rx", 0)
    tx = sold.get("tx", 0)
    total = rx + tx

    if net_today >= best:
        # رقم قياسي جديد
        msg = (
            f"😍 *تنبيه — مبيعات مرتفعة!*\n\n"
            f"🏆 رقم قياسي جديد!\n"
            f"💰 صافي اليوم: *{net_today:,} ريال*\n\n"
            f"📊 *تفاصيل الاستهلاك:*\n"
            f"   ⬇️ التحميل: {fmt_bytes(rx)}\n"
            f"   ⬆️ الرفع: {fmt_bytes(tx)}\n"
            f"   📦 الإجمالي: {fmt_bytes(total)}\n\n"
        )
        lines, total_c, total_r, total_comm, net = calc_sales_report(sold)
        msg += "".join(lines)
        msg += f"\n💰 *الإجمالي: {total_r:,} ريال*\n"
        msg += f"✂️ *بعد العمولة: {net:,} ريال*"
        send(msg)

    elif net_today < best * 0.5:
        # مبيعات منخفضة (أقل من 50% من الرقم القياسي)
        msg = (
            f"😡 *تنبيه — مبيعات منخفضة!*\n\n"
            f"💰 صافي اليوم: *{net_today:,} ريال*\n"
            f"📉 أعلى يوم: *{best:,} ريال*\n\n"
            f"📊 *تفاصيل الاستهلاك:*\n"
            f"   ⬇️ التحميل: {fmt_bytes(rx)}\n"
            f"   ⬆️ الرفع: {fmt_bytes(tx)}\n"
            f"   📦 الإجمالي: {fmt_bytes(total)}\n\n"
        )
        lines, total_c, total_r, total_comm, net = calc_sales_report(sold)
        msg += "".join(lines)
        msg += f"\n💰 *الإجمالي: {total_r:,} ريال*\n"
        msg += f"✂️ *بعد العمولة: {net:,} ريال*"
        send(msg)

    # حفظ أفضل يوم
    if net_today > best:
        db = get_db()
        db.execute("INSERT INTO daily_sales (date,total_sales,rx_bytes,tx_bytes) VALUES (?,?,?,?)",
                   (_today(), net_today, rx, tx))
        db.commit()
        db.close()

def report_weekly():
    since = datetime.now() - timedelta(days=7)
    devices = get_all_devices()
    msg = (f"📅 *تقرير الأسبوع*\n"
           f"📆 {since.strftime('%Y-%m-%d')} ← {datetime.now().strftime('%Y-%m-%d')}\n"
           f"{'━'*25}\n\n")
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        avg = db_avg_duration(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*"
        if avg > 0: msg += f"  |  ⏱ متوسط: {fmt_dur(avg)}"
        msg += "\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top[:5]):
            msg += f"   {medals[i] if i<5 else '•'} {row['device']} — *{row['c']}* انقطاع\n"
    send(msg)

def report_monthly():
    since = datetime.now() - timedelta(days=30)
    devices = get_all_devices()
    msg = f"📈 *تقرير الشهر* (آخر 30 يوم)\n{'━'*25}\n\n"
    for d in devices:
        n   = d["name"]
        c   = db_count_outages(n, since)
        msg += f"📡 *{n}*\n   ⚡ انقطاعات: *{c}*\n\n"
    top = db_top_outages(since)
    if top:
        medals = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]
        msg += "⚠️ *أكثر الأجهزة انقطاعاً:*\n"
        for i, row in enumerate(top[:5]):
            msg += f"   {medals[i] if i<5 else '•'} {row['device']} — *{row['c']}* انقطاع\n"
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

def report_sales():
    sold  = get_today_sales()
    stats = get_latest_stats()
    lines, total_c, total_r, total_comm, net = calc_sales_report(sold)

    rx = sold.get("rx", 0)
    tx = sold.get("tx", 0)

    msg = f"💳 *تقرير المبيعات اليوم*\n📅 {_today()}\n{'━'*25}\n\n"
    msg += f"*استهلاك الشبكة*\n"
    msg += f"   ⬇️ التحميل: *{fmt_bytes(rx)}*\n"
    msg += f"   ⬆️ الرفع: *{fmt_bytes(tx)}*\n\n"
    msg += "━"*25 + "\n\n"
    for line in lines:
        msg += line + "\n"
    msg += "━"*25 + "\n\n"
    msg += f"📊 *الإجمالي*\n"
    msg += f"   عدد الكروت: *{total_c}*\n"
    msg += f"   المبيعات: *{total_r:,} ريال*\n"
    msg += f"   العمولة: *{total_comm:,} ريال*\n"
    msg += f"   💰 صافي الربح: *{net:,} ريال*\n\n"

    if stats:
        msg += "━"*25 + "\n\n"
        msg += f"👥 *المتصلون الآن:* {stats.get('active_users', 0)}\n\n"
        msg += f"📶 *الكروت المتبقية:*\n"
        for pkg_id, pkg in PACKAGES.items():
            key = f"unused{pkg_id.lower().replace('ry','')}"
            unused = stats.get(key, stats.get(f"unused{pkg_id.lower()}", 0))
            msg += f"   • {pkg['name']}: *{unused}* كرت\n"

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
                 "📋 *الأوامر:*\n"
                 "🔹 /status — حالة الأجهزة\n"
                 "🔹 /outages — المتوقفة الآن\n"
                 "🔹 /devices — قائمة الأجهزة\n"
                 "🔹 /stats — إحصائيات الشبكة\n"
                 "🔹 /sales — تقرير المبيعات\n"
                 "🔹 /daily — تقرير اليوم\n"
                 "🔹 /weekly — تقرير الأسبوع\n"
                 "🔹 /monthly — تقرير الشهر")

        elif cmd == "status":
            devices = get_all_devices()
            active  = {r["device"] for r in db_active_outages()}
            stats   = get_latest_stats()
            msg     = "📡 *حالة الأجهزة الآن:*\n\n"
            if not devices: msg += "لا يوجد أجهزة بعد ⏳\n"
            for d in devices:
                icon = "🔴" if d["name"] in active else "🟢"
                msg += f"{icon} *{d['name']}*  `{d['ip'] or '—'}`\n"
            if stats:
                msg += f"\n👥 المتصلون: *{stats.get('active_users', 0)}*\n"
                rx = stats.get("rx_intr1", 0)
                tx = stats.get("tx_intr1", 0)
                msg += f"📊 الاستهلاك الكلي: ⬇️{fmt_bytes(rx)} ⬆️{fmt_bytes(tx)}\n"
            send(msg)

        elif cmd == "outages":  report_active()
        elif cmd == "daily":    report_daily()
        elif cmd == "weekly":   report_weekly()
        elif cmd == "monthly":  report_monthly()
        elif cmd == "sales":    report_sales()

        elif cmd == "devices":
            devices = get_all_devices()
            msg     = f"📋 *الأجهزة ({len(devices)}):*\n\n"
            groups  = {}
            for d in devices: groups.setdefault(d["group_name"], []).append(d)
            for g, devs in groups.items():
                msg += f"🗂 *{g}*\n"
                for d in devs:
                    msg += f"   • *{d['name']}*  `{d['ip'] or '—'}`\n"
                msg += "\n"
            send(msg)

        elif cmd == "stats":
            stats   = get_latest_stats()
            since24 = datetime.now() - timedelta(hours=24)
            devices = get_all_devices()
            active  = db_active_outages()
            t24     = sum(db_count_outages(d["name"], since24) for d in devices)
            msg     = f"📊 *إحصائيات الشبكة*\n\n"
            msg    += f"📡 الأجهزة: *{len(devices)}*\n"
            msg    += f"🔴 متوقفة: *{len(active)}*\n"
            msg    += f"⚡ انقطاعات 24 ساعة: *{t24}*\n\n"
            if stats:
                msg += f"👥 المتصلون الآن: *{stats.get('active_users', 0)}*\n\n"
                msg += f"📶 *الكروت المتبقية:*\n"
                for pkg_id, pkg in PACKAGES.items():
                    key = f"unused{pkg_id.lower()}"
                    unused = stats.get(key, 0)
                    msg += f"   • {pkg['name']}: *{unused}* كرت\n"
                rx = stats.get("rx_intr1", 0)
                tx = stats.get("tx_intr1", 0)
                msg += f"\n📊 *الاستهلاك الكلي:*\n"
                msg += f"   ⬇️ {fmt_bytes(rx)}  ⬆️ {fmt_bytes(tx)}\n"
                msg += f"   📦 {fmt_bytes(rx+tx)}\n"
            send(msg)

    except Exception as e:
        logging.error(f"Bot error: {e}")


# ══════════════════════════════════════════
#  لوحة HTML
# ══════════════════════════════════════════
HTML = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8"><meta http-equiv="refresh" content="60">
<title>مراقبة الشبكة</title>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
h1{color:#38bdf8;text-align:center;margin-bottom:4px;font-size:22px}
.sub{text-align:center;color:#64748b;font-size:12px;margin-bottom:20px}
.stats{display:flex;justify-content:center;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat{background:#1e293b;padding:10px 20px;border-radius:10px;text-align:center;min-width:100px}
.sn{font-size:24px;font-weight:bold;color:#38bdf8}
.sn.r{color:#f87171}.sn.g{color:#4ade80}.sn.y{color:#fbbf24}
.sl{font-size:11px;color:#64748b;margin-top:2px}
.section{background:#1e293b;border-radius:10px;padding:16px;margin-bottom:16px}
.section h2{color:#38bdf8;font-size:16px;margin:0 0 12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.card{background:#0f172a;border-radius:8px;padding:12px;border:1px solid #334155}
.card.down{border-color:#ef4444;background:#1f0f0f}
.card.up{border-color:#22c55e}
.name{font-size:14px;font-weight:bold;margin-bottom:4px}
.info{color:#94a3b8;font-size:11px;margin-bottom:2px}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:bold;margin-top:4px}
.badge.up{background:#14532d;color:#4ade80}
.badge.down{background:#450a0a;color:#f87171}
.dur{color:#fbbf24;font-size:11px;margin-top:3px}
.pkg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.pkg{background:#0f172a;border-radius:8px;padding:12px;border:1px solid #334155;text-align:center}
.pkg-name{color:#38bdf8;font-weight:bold;font-size:14px;margin-bottom:6px}
.pkg-num{font-size:22px;font-weight:bold;color:#4ade80}
.pkg-sub{font-size:11px;color:#64748b;margin-top:2px}
</style></head>
<body>
<h1>🌐 لوحة مراقبة الشبكة</h1>
<p class="sub">آخر تحديث: {{now}} — تتجدد كل دقيقة</p>

<div class="stats">
  <div class="stat"><div class="sn">{{total_dev}}</div><div class="sl">الأجهزة</div></div>
  <div class="stat"><div class="sn g">{{up_dev}}</div><div class="sl">متصلة 🟢</div></div>
  <div class="stat"><div class="sn r">{{down_dev}}</div><div class="sl">منقطعة 🔴</div></div>
  <div class="stat"><div class="sn y">{{active_users}}</div><div class="sl">متصل الآن</div></div>
</div>

<div class="section">
  <h2>📶 الباقات المتبقية</h2>
  <div class="pkg-grid">{{pkg_cards}}</div>
</div>

<div class="section">
  <h2>📊 استهلاك الشبكة</h2>
  <div class="stats" style="margin:0">
    <div class="stat"><div class="sn" style="font-size:18px">{{rx_total}}</div><div class="sl">⬇️ تحميل</div></div>
    <div class="stat"><div class="sn" style="font-size:18px">{{tx_total}}</div><div class="sl">⬆️ رفع</div></div>
    <div class="stat"><div class="sn" style="font-size:18px">{{rx_ppp1}}</div><div class="sl">خط 1</div></div>
    <div class="stat"><div class="sn" style="font-size:18px">{{rx_ppp2}}</div><div class="sl">خط 2</div></div>
  </div>
</div>

<div class="section">
  <h2>📡 حالة الأجهزة</h2>
  <div class="grid">{{device_cards}}</div>
</div>
</body></html>"""

def build_dashboard():
    devices  = get_all_devices()
    active_d = {r["device"]: r for r in db_active_outages()}
    stats    = get_latest_stats()

    down_cnt = 0
    dev_cards = ""
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
        dev_cards += f"""<div class="card {status}">
  <div class="name">📡 {name}</div>
  <div class="info">🌐 {d['ip'] or '—'}</div>
  <span class="badge {status}">{badge}</span>{dur_html}
</div>"""

    pkg_cards = ""
    for pkg_id, pkg in PACKAGES.items():
        key    = f"unused{pkg_id.lower()}"
        unused = stats.get(key, 0) if stats else 0
        total  = stats.get(f"count{pkg_id.lower()}", 0) if stats else 0
        used   = total - unused
        pkg_cards += f"""<div class="pkg">
  <div class="pkg-name">{pkg['name']}</div>
  <div class="pkg-num">{unused}</div>
  <div class="pkg-sub">متبقي من {total}</div>
  <div class="pkg-sub" style="color:#f87171">مستخدم: {used}</div>
</div>"""

    total    = len(devices)
    active_u = stats.get("active_users", 0) if stats else 0
    rx_total = fmt_bytes(stats.get("rx_intr1", 0)) if stats else "—"
    tx_total = fmt_bytes(stats.get("tx_intr1", 0)) if stats else "—"
    rx_ppp1  = fmt_bytes(stats.get("rx_ppp1", 0))  if stats else "—"
    rx_ppp2  = fmt_bytes(stats.get("rx_ppp2", 0))  if stats else "—"

    html = HTML.replace("{{now}}",         datetime.now().strftime("%I:%M:%S %p"))
    html = html.replace("{{total_dev}}",   str(total))
    html = html.replace("{{up_dev}}",      str(total - down_cnt))
    html = html.replace("{{down_dev}}",    str(down_cnt))
    html = html.replace("{{active_users}}",str(active_u))
    html = html.replace("{{pkg_cards}}",   pkg_cards)
    html = html.replace("{{device_cards}}",dev_cards)
    html = html.replace("{{rx_total}}",    rx_total)
    html = html.replace("{{tx_total}}",    tx_total)
    html = html.replace("{{rx_ppp1}}",     rx_ppp1)
    html = html.replace("{{rx_ppp2}}",     rx_ppp2)
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
        send(f"🚨 *انقطاع!*\n\n📡 *{device}*  |  `{dev_ip}`\n📍 {dev_loc or '—'}\n💬 {message or 'Link Down'}\n🕒 {datetime.now().strftime('%I:%M:%S %p')}")
    else:
        secs = db_close_outage(device)
        send(f"✅ *عاد للاتصال!*\n\n📡 *{device}*  |  `{dev_ip}`\n📍 {dev_loc or '—'}\n⏱ مدة الانقطاع: *{fmt_dur(secs)}*\n🕒 {datetime.now().strftime('%I:%M:%S %p')}")

    return jsonify({"ok": True})


@app.route("/stats", methods=["POST","GET"])
def receive_stats():
    """يستقبل إحصائيات المايكروتك كل 5 دقائق"""
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

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    save_stats(data)
    return jsonify({"ok": True})


@app.route("/tgwebhook", methods=["POST"])
def tg_webhook():
    threading.Thread(target=handle_bot, args=(request.json or {},)).start()
    return jsonify({"ok": True})

@app.route("/setup_webhook")
def setup_webhook():
    base = request.host_url.rstrip("/")
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                      json={"url": f"{base}/tgwebhook"})
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

send(f"🚀 *النظام يعمل*\n\n"
     f"🔗 جاهز لاستقبال إشعارات Dude\n"
     f"📊 جاهز لاستقبال إحصائيات المايكروتك\n"
     f"🕒 {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
