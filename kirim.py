# kirim_multi.py
import time
import random
import pathlib
import requests
from datetime import datetime

# =========================
# Konfigurasi dasar
# =========================
BASE_DELAY_PRIVATE = 2.0
BASE_DELAY_GROUP   = 3.0
JITTER_MAX         = 0.5
GLOBAL_MAX_PER_SEC = 25
TIMEOUT_S          = 15
USE_HTML           = False
DISABLE_WEB_PAGE_PREVIEW = True

# =========================
# Baca file
# =========================
root = pathlib.Path(__file__).parent
tokens   = [t.strip() for t in (root / "tokens.txt").read_text(encoding="utf-8").splitlines() if t.strip()]
chat_ids = [c.strip() for c in (root / "chat_ids.txt").read_text(encoding="utf-8").splitlines() if c.strip()]

# Pisahkan pesan berdasarkan baris kosong
raw_messages = (root / "messages.txt").read_text(encoding="utf-8")
messages = [m.strip() for m in raw_messages.split("\n\n") if m.strip()]

# =========================
# Utilitas
# =========================
_last_ticks = []

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def is_group_chat(chat_id: str) -> bool:
    try:
        return int(chat_id) < 0
    except:
        return False

def delay_for_chat(chat_id: str) -> float:
    base = BASE_DELAY_GROUP if is_group_chat(chat_id) else BASE_DELAY_PRIVATE
    return base + random.uniform(0, JITTER_MAX)

def global_throttle():
    now = time.time()
    while _last_ticks and (now - _last_ticks[0]) > 1.0:
        _last_ticks.pop(0)
    if len(_last_ticks) >= GLOBAL_MAX_PER_SEC:
        sleep_for = 1.0 - (now - _last_ticks[0]) + 0.01
        print(f"[{now_str()}] ⏳ Throttle global: tidur {sleep_for:.2f}s")
        time.sleep(max(0.0, sleep_for))
    _last_ticks.append(time.time())

def send_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": DISABLE_WEB_PAGE_PREVIEW,
    }
    if USE_HTML:
        payload["parse_mode"] = "HTML"

    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT_S)
    except Exception as e:
        return False, {"error": "network_error", "detail": str(e)}

    if resp.status_code == 200:
        return True, None

    try:
        data = resp.json()
    except Exception:
        data = {"status_code": resp.status_code, "text": resp.text}

    return False, data

# =========================
# Eksekusi
# =========================
print(f"[{now_str()}] 🚀 Start send. {len(tokens)} bot, {len(chat_ids)} chats, {len(messages)} templates.")

for token in tokens:
    print(f"\n[{now_str()}] ===== Bot ****{token[-6:]} =====")
    for chat_id in chat_ids:
        global_throttle()

        # Pilih pesan acak
        text = random.choice(messages)

        ok, err = send_message(token, chat_id, text)
        if ok:
            print(f"[{now_str()}] ✅ Sent to {chat_id}")
        else:
            code = err.get("error_code") or err.get("status_code") or err.get("error")
            desc = err.get("description") or err.get("detail") or err.get("text", "")
            print(f"[{now_str()}] ❌ Fail to {chat_id}: {code} | {desc}")

        d = delay_for_chat(chat_id)
        print(f"[{now_str()}] 💤 Delay {d:.2f}s…")
        time.sleep(d)

print(f"\n[{now_str()}] 🏁 Done.")
