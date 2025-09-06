# kirim.py
import time
import random
import json
import pathlib
import requests
from datetime import datetime

# =========================
# Konfigurasi dasar
# =========================
BASE_DELAY_PRIVATE = 2.0     # detik; aman untuk chat privat/channel
BASE_DELAY_GROUP   = 3.0     # detik; aman untuk grup (<=20/min)
JITTER_MAX         = 0.5     # detik acak tambahan (0..JITTER_MAX)
GLOBAL_MAX_PER_SEC = 25      # jaga di bawah ~30/dtk (buffer)
TIMEOUT_S          = 15      # timeout HTTP
MAX_RETRIES_OTHER  = 3       # retry untuk error non-429 (5xx/timeout)
USE_HTML           = False   # True jika mau parse_mode HTML

DISABLE_WEB_PAGE_PREVIEW = True

# =========================
# Baca file
# =========================
root = pathlib.Path(__file__).parent
tokens_path   = root / "tokens.txt"
chat_ids_path = root / "chat_ids.txt"
message_path  = root / "message.txt"

tokens   = [t.strip() for t in tokens_path.read_text(encoding="utf-8").splitlines() if t.strip()]
chat_ids = [c.strip() for c in chat_ids_path.read_text(encoding="utf-8").splitlines() if c.strip()]
message  = message_path.read_text(encoding="utf-8")

# =========================
# Utilitas
# =========================
_last_ticks = []

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def is_group_chat(chat_id: str) -> bool:
    # Heuristik: chat_id grup/supergroup/channel biasanya negatif (numerik).
    try:
        return int(chat_id) < 0
    except:
        # untuk username channel (@namachannel) kita treat seperti privat (lebih longgar)
        return False

def delay_for_chat(chat_id: str) -> float:
    base = BASE_DELAY_GROUP if is_group_chat(chat_id) else BASE_DELAY_PRIVATE
    return base + random.uniform(0, JITTER_MAX)

def global_throttle():
    """Throttle global agar rata-rata <= GLOBAL_MAX_PER_SEC."""
    now = time.time()
    # buang timestamp yang lebih tua dari 1 detik
    while _last_ticks and (now - _last_ticks[0]) > 1.0:
        _last_ticks.pop(0)
    if len(_last_ticks) >= GLOBAL_MAX_PER_SEC:
        sleep_for = 1.0 - (now - _last_ticks[0]) + 0.01
        print(f"[{now_str()}] ⏳ Throttle global: tidur {sleep_for:.2f}s")
        time.sleep(max(0.0, sleep_for))
    _last_ticks.append(time.time())

def send_with_retry(token: str, chat_id: str, text: str):
    """
    Kirim pesan dengan handling:
    - 429: patuhi retry_after (loop sampai berhasil/gagal non-429)
    - 401/400/403: laporkan jelas
    - 5xx/timeout: retry terbatas (MAX_RETRIES_OTHER) dengan backoff
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": DISABLE_WEB_PAGE_PREVIEW,
    }
    if USE_HTML:
        payload["parse_mode"] = "HTML"

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT_S)
        except requests.exceptions.RequestException as e:
            # Kegagalan jaringan/timeouts → backoff terbatas
            if attempt <= MAX_RETRIES_OTHER:
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.25)
                print(f"[{now_str()}] ⚠️ Network error: {e}. Retry {attempt}/{MAX_RETRIES_OTHER} dalam {backoff:.2f}s")
                time.sleep(backoff)
                continue
            return False, {"error": "network_error", "detail": str(e)}

        # Sukses HTTP
        if resp.status_code == 200:
            return True, None

        # Tangani khusus 429 (rate limit)
        if resp.status_code == 429:
            wait = 1
            try:
                data = resp.json()
                wait = data.get("parameters", {}).get("retry_after", 1)
            except Exception:
                pass
            print(f"[{now_str()}] ⚠️ 429 Rate limit. Disuruh tunggu {wait}s oleh Telegram…")
            time.sleep(wait + 0.25)
            # lanjut loop tanpa menaikkan attempt, karena ini kontrol Telegram
            continue

        # Tangani error lain berbasis JSON Telegram
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}

        code = data.get("error_code", resp.status_code)
        desc = data.get("description", "")

        # Token salah/berganti/akses gagal
        if code in (400, 401, 403):
            if code == 401:
                print(f"[{now_str()}] ❌ 401 Unauthorized (token salah/berubah). Hentikan untuk token ini.")
            elif code == 403:
                print(f"[{now_str()}] ❌ 403 Forbidden (bot diblokir / tidak punya hak di chat tersebut).")
            else:
                print(f"[{now_str()}] ❌ 400 Bad Request: {desc}")
            return False, data

        # 5xx server Telegram → retry terbatas
        if 500 <= code <= 599 and attempt <= MAX_RETRIES_OTHER:
            backoff = min(2 ** attempt, 8) + random.uniform(0, 0.25)
            print(f"[{now_str()}] ⚠️ Server error {code}: {desc}. Retry {attempt}/{MAX_RETRIES_OTHER} dalam {backoff:.2f}s")
            time.sleep(backoff)
            continue

        # Lainnya: gagal
        return False, data

# =========================
# Eksekusi
# =========================
print(f"[{now_str()}] 🚀 Mulai kirim. Token: {len(tokens)} bot, Target: {len(chat_ids)} chat.")

for token in tokens:
    print(f"\n[{now_str()}] ===== Bot ****{token[-6:]} =====")
    for chat_id in chat_ids:
        # throttle global (broadcast besar)
        global_throttle()

        ok, err = send_with_retry(token, chat_id, message)
        if ok:
            print(f"[{now_str()}] ✅ Terkirim ke {chat_id}")
        else:
            # tampilkan ringkas tapi informatif
            if isinstance(err, dict):
                code = err.get("error_code") or err.get("status_code") or err.get("error")
                desc = err.get("description") or err.get("detail") or err.get("text", "")
                print(f"[{now_str()}] ❌ Gagal ke {chat_id}: {code} | {desc}")
            else:
                print(f"[{now_str()}] ❌ Gagal ke {chat_id}: {err}")

        # jeda per chat, sesuai jenis chat + jitter
        d = delay_for_chat(chat_id)
        print(f"[{now_str()}] 💤 Delay {d:.2f}s sebelum target berikutnya…")
        time.sleep(d)

print(f"\n[{now_str()}] 🏁 Selesai.")
