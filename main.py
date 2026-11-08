import os
import json
import time
import psutil
import signal
import threading
import subprocess
from datetime import datetime, timedelta
import telebot
from telebot import types

# ============================================================
# AYARLAR
# ============================================================
# Railway'de: Variables sekmesinden BOT_TOKEN ekle, kodda token yazma.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "BOT_TOKEN_BURAYA")

# Railway'de: Variables sekmesinden ADMIN_IDS="123456789,987654321" ekleyebilirsin.
_admin_ids_env = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in _admin_ids_env.split(",") if x.strip()] or [8721726129]

# İzlenecek/yönetilecek servisler (kendi botların)
# "cmd" ile başlatılır, loglar logs/<isim>.log dosyasına yazılır
MANAGED_SERVICES = {
    "nebula": {"cmd": ["python3", "Nebul1.py"], "cwd": "/root/nebula_hosting"},
    "zadamar": {"cmd": ["python3", "smm_bot30.py"], "cwd": "/root/zadamar_bot"},
    # yeni servis eklemek icin buraya "isim": {"cmd": [...], "cwd": "..."} ekle
}

# Dosya tarayıcısının erişebileceği kök dizin (güvenlik için dışına çıkamaz)
FILES_BASE_DIR = "/root"

# Telegram'dan .py dosyası gönderince buraya kaydedilir ve otomatik başlatılır
UPLOADED_SCRIPTS_DIR = "uploaded_scripts"

# Alarm varsayılanları (yüzde)
DEFAULT_THRESHOLDS = {"cpu": 85, "ram": 85, "disk": 90}
ALERT_CHECK_INTERVAL = 60  # saniye
ALERT_COOLDOWN = 600  # aynı uyarı tekrar gönderilmeden önce beklenecek saniye

RUNTIME_DIR = "runtime"
LOGS_DIR = "logs"
PIDS_FILE = os.path.join(RUNTIME_DIR, "pids.json")
CONFIG_FILE = os.path.join(RUNTIME_DIR, "config.json")
DYNAMIC_SERVICES_FILE = os.path.join(RUNTIME_DIR, "dynamic_services.json")

for d in (RUNTIME_DIR, LOGS_DIR, UPLOADED_SCRIPTS_DIR):
    os.makedirs(d, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# upload_dir: kullanıcı dosya yüklerken hedef klasörü tutar
session = {}  # user_id -> {"upload_dir": "/root/..."}

# ============================================================
# YETKİ KONTROLÜ
# ============================================================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def admin_only(func):
    def wrapper(message_or_call, *args, **kwargs):
        uid = message_or_call.from_user.id
        if not is_admin(uid):
            if isinstance(message_or_call, types.CallbackQuery):
                bot.answer_callback_query(message_or_call.id, "Yetkin yok!", show_alert=True)
            else:
                bot.reply_to(message_or_call, "Bu botu kullanma yetkin yok!")
            return
        return func(message_or_call, *args, **kwargs)
    return wrapper

# ============================================================
# CONFIG (eşik değerleri + alarm aç/kapa) - JSON'da saklanır
# ============================================================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"thresholds": dict(DEFAULT_THRESHOLDS), "alerts_enabled": True}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

# ============================================================
# DİNAMİK SERVİSLER (Telegram'dan yüklenen .py dosyaları)
# ============================================================
def load_dynamic_services():
    if os.path.exists(DYNAMIC_SERVICES_FILE):
        try:
            with open(DYNAMIC_SERVICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_dynamic_services(svcs):
    with open(DYNAMIC_SERVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(svcs, f, ensure_ascii=False, indent=2)

def get_all_services():
    """Sabit (koddaki) + dinamik (Telegram'dan yüklenen) servisleri birleştirir."""
    all_svcs = dict(MANAGED_SERVICES)
    all_svcs.update(load_dynamic_services())
    return all_svcs

def safe_service_name(filename):
    name = os.path.splitext(filename)[0]
    name = "".join(c for c in name if c.isalnum() or c in ("_", "-")).strip()
    return name or f"script_{int(time.time())}"

# ============================================================
# PID TAKİBİ (bot yeniden başlasa bile servis durumu korunur)
# ============================================================
def load_pids():
    if os.path.exists(PIDS_FILE):
        try:
            with open(PIDS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_pids(pids):
    with open(PIDS_FILE, "w", encoding="utf-8") as f:
        json.dump(pids, f, ensure_ascii=False, indent=2)

def is_pid_alive(pid):
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False

# ============================================================
# SERVİS YÖNETİMİ
# ============================================================
def service_status(name):
    pids = load_pids()
    pid = pids.get(name)
    if pid and is_pid_alive(pid):
        return "running", pid
    return "stopped", None

def start_service(name):
    all_svcs = get_all_services()
    if name not in all_svcs:
        return False, "Tanımsız servis."
    status, _ = service_status(name)
    if status == "running":
        return False, "Zaten çalışıyor."
    svc = all_svcs[name]
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n===== BAŞLATILDI {datetime.now()} =====\n")
            proc = subprocess.Popen(
                svc["cmd"],
                cwd=svc.get("cwd"),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        pids = load_pids()
        pids[name] = proc.pid
        save_pids(pids)
        return True, f"Başlatıldı. PID: {proc.pid}"
    except Exception as e:
        return False, f"Hata: {e}"

def stop_service(name, timeout=8):
    status, pid = service_status(name)
    if status != "running":
        return False, "Zaten durmuş."
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            psutil.Process(pid).terminate()
        except Exception:
            pass
    waited = 0
    while is_pid_alive(pid) and waited < timeout:
        time.sleep(0.5)
        waited += 0.5
    if is_pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
    pids = load_pids()
    pids.pop(name, None)
    save_pids(pids)
    return True, "Durduruldu."

def restart_service(name):
    stop_service(name)
    time.sleep(1)
    return start_service(name)

def tail_log(name, n=40):
    log_path = os.path.join(LOGS_DIR, f"{name}.log")
    if not os.path.exists(log_path):
        return "Log bulunamadı."
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-n:]) or "Log boş."
    except Exception as e:
        return f"Log okunamadı: {e}"

# ============================================================
# SİSTEM DURUMU
# ============================================================
def format_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def get_system_status():
    cpu = psutil.cpu_percent(interval=0.6)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot_time
    uptime_str = str(timedelta(seconds=int(uptime.total_seconds())))
    load1, load5, load15 = (os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0))

    text = (
        "<b>📊 SİSTEM DURUMU</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥 CPU: <b>%{cpu:.1f}</b> (load: {load1:.2f}, {load5:.2f}, {load15:.2f})\n"
        f"🧠 RAM: <b>%{ram.percent}</b> ({format_bytes(ram.used)} / {format_bytes(ram.total)})\n"
        f"💾 Disk: <b>%{disk.percent}</b> ({format_bytes(disk.used)} / {format_bytes(disk.total)})\n"
        f"🌐 Ağ: ↓{format_bytes(net.bytes_recv)} ↑{format_bytes(net.bytes_sent)}\n"
        f"⏱ Uptime: {uptime_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    return text

# ============================================================
# MENÜLER
# ============================================================
def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Sistem Durumu", callback_data="sys_status"),
        types.InlineKeyboardButton("⚙️ Servisler", callback_data="services_menu"),
    )
    kb.add(
        types.InlineKeyboardButton("📁 Dosyalar", callback_data=f"files_menu:{FILES_BASE_DIR}"),
        types.InlineKeyboardButton("🔔 Alarm Ayarları", callback_data="alerts_menu"),
    )
    kb.add(types.InlineKeyboardButton("📤 Script Yükle ve Başlat", callback_data="upload_script"))
    return kb

def services_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for name in get_all_services():
        status, _ = service_status(name)
        icon = "🟢" if status == "running" else "🔴"
        tag = " (yüklendi)" if name in load_dynamic_services() else ""
        kb.add(types.InlineKeyboardButton(f"{icon} {name}{tag}", callback_data=f"service_panel:{name}"))
    kb.add(types.InlineKeyboardButton("⬅️ Ana Menü", callback_data="main_menu"))
    return kb

def service_panel_kb(name):
    status, _ = service_status(name)
    is_dynamic = name in load_dynamic_services()
    kb = types.InlineKeyboardMarkup(row_width=2)
    if status == "running":
        kb.add(
            types.InlineKeyboardButton("⏹ Durdur", callback_data=f"svc_stop:{name}"),
            types.InlineKeyboardButton("🔁 Restart", callback_data=f"svc_restart:{name}"),
        )
    else:
        kb.add(types.InlineKeyboardButton("▶️ Başlat", callback_data=f"svc_start:{name}"))
    kb.add(types.InlineKeyboardButton("📄 Son Loglar", callback_data=f"svc_log:{name}"))
    if is_dynamic:
        kb.add(types.InlineKeyboardButton("🗑 Sil", callback_data=f"svc_delete:{name}"))
    kb.add(types.InlineKeyboardButton("⬅️ Servisler", callback_data="services_menu"))
    return kb

def safe_join(base, rel):
    # path traversal koruması
    target = os.path.normpath(os.path.join(base, rel))
    if not target.startswith(os.path.normpath(FILES_BASE_DIR)):
        return FILES_BASE_DIR
    return target

def files_menu_kb(path):
    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        entries = sorted(os.listdir(path))
    except Exception:
        entries = []
    for entry in entries[:40]:
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            kb.add(types.InlineKeyboardButton(f"📁 {entry}", callback_data=f"files_menu:{full}"))
        else:
            kb.add(types.InlineKeyboardButton(f"📄 {entry}", callback_data=f"file_panel:{full}"))
    nav = []
    if os.path.normpath(path) != os.path.normpath(FILES_BASE_DIR):
        parent = os.path.dirname(path.rstrip("/"))
        nav.append(types.InlineKeyboardButton("⬆️ Üst Dizin", callback_data=f"files_menu:{parent}"))
    nav.append(types.InlineKeyboardButton("⬆️ Buraya Yükle", callback_data=f"upload_here:{path}"))
    kb.add(*nav)
    kb.add(types.InlineKeyboardButton("⬅️ Ana Menü", callback_data="main_menu"))
    return kb

def file_panel_kb(path):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("⬇️ İndir", callback_data=f"file_download:{path}"))
    kb.add(types.InlineKeyboardButton("⬅️ Geri", callback_data=f"files_menu:{os.path.dirname(path)}"))
    return kb

def alerts_menu_kb():
    th = config["thresholds"]
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton(f"CPU %{th['cpu']} ➖", callback_data="th_dec:cpu"),
        types.InlineKeyboardButton("➕", callback_data="th_inc:cpu"),
        types.InlineKeyboardButton(" ", callback_data="noop"),
    )
    kb.add(
        types.InlineKeyboardButton(f"RAM %{th['ram']} ➖", callback_data="th_dec:ram"),
        types.InlineKeyboardButton("➕", callback_data="th_inc:ram"),
        types.InlineKeyboardButton(" ", callback_data="noop"),
    )
    kb.add(
        types.InlineKeyboardButton(f"Disk %{th['disk']} ➖", callback_data="th_dec:disk"),
        types.InlineKeyboardButton("➕", callback_data="th_inc:disk"),
        types.InlineKeyboardButton(" ", callback_data="noop"),
    )
    toggle_text = "🔕 Alarmları Kapat" if config["alerts_enabled"] else "🔔 Alarmları Aç"
    kb.add(types.InlineKeyboardButton(toggle_text, callback_data="th_toggle"))
    kb.add(types.InlineKeyboardButton("⬅️ Ana Menü", callback_data="main_menu"))
    return kb

# ============================================================
# KOMUTLAR
# ============================================================
@bot.message_handler(commands=["start"])
@admin_only
def cmd_start(message):
    bot.send_message(message.chat.id, "<b>VDS Yönetim Botu</b>\n\nBir seçenek seç:", reply_markup=main_menu())

@bot.message_handler(commands=["status"])
@admin_only
def cmd_status(message):
    bot.send_message(message.chat.id, get_system_status())

# ============================================================
# CALLBACK'LER
# ============================================================
@bot.callback_query_handler(func=lambda c: True)
@admin_only
def handle_callback(call):
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "noop":
        return

    if data == "main_menu":
        bot.edit_message_text("<b>VDS Yönetim Botu</b>\n\nBir seçenek seç:", call.message.chat.id, call.message.message_id, reply_markup=main_menu())
        return

    if data == "sys_status":
        bot.edit_message_text(get_system_status(), call.message.chat.id, call.message.message_id, reply_markup=main_menu())
        return

    if data == "services_menu":
        bot.edit_message_text("<b>⚙️ Servisler</b>\n\nYönetmek istediğin servisi seç:", call.message.chat.id, call.message.message_id, reply_markup=services_menu_kb())
        return

    if data.startswith("service_panel:"):
        name = data.split(":", 1)[1]
        status, pid = service_status(name)
        txt = f"<b>{name}</b>\nDurum: {'🟢 Çalışıyor (PID ' + str(pid) + ')' if status == 'running' else '🔴 Durmuş'}"
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=service_panel_kb(name))
        return

    if data.startswith("svc_start:"):
        name = data.split(":", 1)[1]
        ok, msg = start_service(name)
        bot.answer_callback_query(call.id, msg, show_alert=not ok)
        status, pid = service_status(name)
        txt = f"<b>{name}</b>\nDurum: {'🟢 Çalışıyor (PID ' + str(pid) + ')' if status == 'running' else '🔴 Durmuş'}"
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=service_panel_kb(name))
        return

    if data.startswith("svc_stop:"):
        name = data.split(":", 1)[1]
        ok, msg = stop_service(name)
        bot.answer_callback_query(call.id, msg, show_alert=not ok)
        txt = f"<b>{name}</b>\nDurum: 🔴 Durmuş"
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=service_panel_kb(name))
        return

    if data.startswith("svc_restart:"):
        name = data.split(":", 1)[1]
        ok, msg = restart_service(name)
        bot.answer_callback_query(call.id, msg, show_alert=not ok)
        status, pid = service_status(name)
        txt = f"<b>{name}</b>\nDurum: {'🟢 Çalışıyor (PID ' + str(pid) + ')' if status == 'running' else '🔴 Durmuş'}"
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=service_panel_kb(name))
        return

    if data.startswith("svc_log:"):
        name = data.split(":", 1)[1]
        log_text = tail_log(name, 40)
        if len(log_text) > 3500:
            log_text = log_text[-3500:]
        bot.send_message(call.message.chat.id, f"<b>📄 {name} - son loglar</b>\n<pre>{telebot.util.escape(log_text)}</pre>")
        return

    if data.startswith("files_menu:"):
        path = data.split(":", 1)[1]
        path = safe_join(FILES_BASE_DIR, os.path.relpath(path, FILES_BASE_DIR)) if path != FILES_BASE_DIR else FILES_BASE_DIR
        bot.edit_message_text(f"<b>📁 {path}</b>", call.message.chat.id, call.message.message_id, reply_markup=files_menu_kb(path))
        return

    if data.startswith("file_panel:"):
        path = data.split(":", 1)[1]
        size = os.path.getsize(path) if os.path.exists(path) else 0
        bot.edit_message_text(f"<b>📄 {os.path.basename(path)}</b>\nBoyut: {format_bytes(size)}", call.message.chat.id, call.message.message_id, reply_markup=file_panel_kb(path))
        return

    if data.startswith("file_download:"):
        path = data.split(":", 1)[1]
        try:
            size = os.path.getsize(path)
            if size > 49 * 1024 * 1024:
                bot.send_message(call.message.chat.id, "Dosya 49MB üzeri, Telegram ile gönderilemiyor.")
                return
            with open(path, "rb") as f:
                bot.send_document(call.message.chat.id, f, visible_file_name=os.path.basename(path))
        except Exception as e:
            bot.send_message(call.message.chat.id, f"Hata: {e}")
        return

    if data.startswith("upload_here:"):
        path = data.split(":", 1)[1]
        session[call.from_user.id] = {"upload_dir": path}
        bot.send_message(call.message.chat.id, f"Şimdi bir dosya gönder, <code>{path}</code> içine kaydedilecek.")
        return

    if data == "upload_script":
        session[call.from_user.id] = {"upload_script": True}
        bot.send_message(call.message.chat.id, "Şimdi çalıştırmak istediğin <b>.py</b> dosyasını gönder.\n\nKaydedilip otomatik başlatılacak, Servisler menüsünden yönetebilirsin.")
        return

    if data.startswith("svc_delete:"):
        name = data.split(":", 1)[1]
        dyn = load_dynamic_services()
        if name not in dyn:
            bot.answer_callback_query(call.id, "Bu servis silinemez.", show_alert=True)
            return
        stop_service(name)
        script_path = dyn[name].get("cmd", [None, None])[1]
        dyn.pop(name, None)
        save_dynamic_services(dyn)
        if script_path and os.path.exists(script_path):
            try:
                os.remove(script_path)
            except Exception:
                pass
        bot.edit_message_text("<b>⚙️ Servisler</b>\n\nYönetmek istediğin servisi seç:", call.message.chat.id, call.message.message_id, reply_markup=services_menu_kb())
        return

    if data == "alerts_menu":
        bot.edit_message_text("<b>🔔 Alarm Ayarları</b>\n\nEşik değerleri (bu değer aşılınca bildirim gelir):", call.message.chat.id, call.message.message_id, reply_markup=alerts_menu_kb())
        return

    if data.startswith("th_inc:") or data.startswith("th_dec:"):
        key = data.split(":", 1)[1]
        delta = 5 if data.startswith("th_inc:") else -5
        config["thresholds"][key] = max(10, min(99, config["thresholds"][key] + delta))
        save_config(config)
        bot.edit_message_text("<b>🔔 Alarm Ayarları</b>\n\nEşik değerleri (bu değer aşılınca bildirim gelir):", call.message.chat.id, call.message.message_id, reply_markup=alerts_menu_kb())
        return

    if data == "th_toggle":
        config["alerts_enabled"] = not config["alerts_enabled"]
        save_config(config)
        bot.edit_message_text("<b>🔔 Alarm Ayarları</b>\n\nEşik değerleri (bu değer aşılınca bildirim gelir):", call.message.chat.id, call.message.message_id, reply_markup=alerts_menu_kb())
        return

# ============================================================
# DOSYA YÜKLEME (admin bir klasörü "Buraya Yükle" ile seçtiyse)
# ============================================================
@bot.message_handler(content_types=["document"])
@admin_only
def handle_upload(message):
    uid = message.from_user.id
    user_session = session.get(uid, {})

    # Durum 1: "Script Yükle ve Başlat" ile .py bekleniyor
    if user_session.get("upload_script"):
        session.pop(uid, None)
        filename = message.document.file_name or ""
        if not filename.lower().endswith(".py"):
            bot.reply_to(message, "Sadece .py dosyası kabul edilir!")
            return
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            dest = os.path.join(UPLOADED_SCRIPTS_DIR, filename)
            with open(dest, "wb") as f:
                f.write(downloaded)

            name = safe_service_name(filename)
            dyn = load_dynamic_services()
            dyn[name] = {"cmd": ["python3", dest], "cwd": None}
            save_dynamic_services(dyn)

            ok, msg = start_service(name)
            status, pid = service_status(name)
            txt = (
                f"<b>{filename}</b> kaydedildi.\n"
                f"Servis adı: <code>{name}</code>\n"
                f"Durum: {'🟢 Çalışıyor (PID ' + str(pid) + ')' if status == 'running' else '🔴 Başlatılamadı: ' + msg}"
            )
            bot.reply_to(message, txt, reply_markup=service_panel_kb(name))
        except Exception as e:
            bot.reply_to(message, f"Hata: {e}")
        return

    # Durum 2: Dosyalar menüsünden bir klasöre normal dosya yükleme
    target_dir = user_session.get("upload_dir")
    if not target_dir:
        bot.reply_to(message, "Önce Dosyalar menüsünden bir klasör seçip 'Buraya Yükle' butonuna bas, ya da ana menüden 'Script Yükle ve Başlat' kullan.")
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        dest = os.path.join(target_dir, message.document.file_name)
        with open(dest, "wb") as f:
            f.write(downloaded)
        bot.reply_to(message, f"Yüklendi: <code>{dest}</code>")
    except Exception as e:
        bot.reply_to(message, f"Hata: {e}")
    finally:
        session.pop(uid, None)

# ============================================================
# ARKA PLAN ALARM İZLEYİCİSİ
# ============================================================
_last_alert = {"cpu": 0, "ram": 0, "disk": 0}

def alert_loop():
    while True:
        try:
            if config.get("alerts_enabled", True):
                cpu = psutil.cpu_percent(interval=1)
                ram = psutil.virtual_memory().percent
                disk = psutil.disk_usage("/").percent
                now = time.time()
                checks = {"cpu": cpu, "ram": ram, "disk": disk}
                for key, value in checks.items():
                    threshold = config["thresholds"][key]
                    if value >= threshold and (now - _last_alert[key]) > ALERT_COOLDOWN:
                        _last_alert[key] = now
                        label = {"cpu": "CPU", "ram": "RAM", "disk": "Disk"}[key]
                        for admin_id in ADMIN_IDS:
                            try:
                                bot.send_message(admin_id, f"⚠️ <b>Uyarı:</b> {label} kullanımı %{value:.1f} (eşik: %{threshold})")
                            except Exception:
                                pass
        except Exception as e:
            print(f"Alert loop hatası: {e}")
        time.sleep(ALERT_CHECK_INTERVAL)

# ============================================================
# ÇALIŞTIR
# ============================================================
def main():
    if BOT_TOKEN == "BOT_TOKEN_BURAYA":
        print("BOT_TOKEN'i gir!")
        return
    print("=" * 50)
    print("VDS Yönetim Botu başlatılıyor...")
    print("=" * 50)
    t = threading.Thread(target=alert_loop, daemon=True)
    t.start()
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    main()
