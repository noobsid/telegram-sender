# kirim_loop_multi.py
import time
import random
import pathlib
import requests
from datetime import datetime

# =========================
# KONFIGURASI
# =========================

# Jadwal putaran
RUN_FOREVER         = True      # True = kirim berulang sesuai interval; False = sekali jalan
SEND_EVERY_SECONDS  = 600       # jeda antar PUTARAN (contoh: 600 = 10 menit)
JITTER_ROUND_MAX    = 10        # jitter (0..N detik) ditambahkan di akhir setiap putaran

# Rate limit & pengiriman
BASE_DELAY_PRIVATE  = 2.0       # delay per chat privat/channel (detik)
BASE_DELAY_GROUP    = 3.0       # delay per chat grup/supergroup (detik)
JITTER_PER_CHAT_MAX = 0.5       # jitter 0..N detik di antara tiap chat
GLOBAL_MAX_PER_SEC  = 25        # throttle global (buffer di bawah ~30/dtk)
TIMEOUT_S           = 15        # timeout HTTP requests
MAX_RETRIES_OTHER   = 3         # retry untuk error non-429 (5xx/timeout)

# Format pesan
USE_HTML                   = False   # True kalau mau parse_mode HTML
DISABLE_WEB_PAGE_PREVIEW   = True    # True untuk matikan preview link

# =========================
# BACA FILE
# =========================
root = pathlib.Path(__file__).parent

def read_lines(path: pathlib.Path):
    if not path.exists():
        return []
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

def read_messages(path: pathlib.Path):
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.split("\n\n") if b.strip()]
    return blocks

tokens     = read_lines(root / "tokens.txt")
raw_targets= read_lines(root / "chat_ids.txt")  # bisa -100..., @username, t.me/...
messages   = read_messages(root / "messages.txt")

if not tokens:
    raise SystemExit("tokens.txt kosong atau tidak ditemukan.")
if not raw_targets:
    raise SystemExit("chat_ids.txt kosong atau tidak ditemukan.")
if not messages:
    raise SystemExit("messages.txt kosong atau tidak ditemukan (butuh minimal 1 template).")

# =========================
# UTILITAS
# =========================
_last_ticks = []

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def is_group_chat(chat_id: int) -> bool:
    # supergroup/channel biasanya id negatif (sering -100xxxxxxxxxx)
    try:
        return int(chat_id) < 0
    except Exception:
        return False

def delay_for_chat(chat_id: int) -> float:
    base = BASE_DELAY_GROUP if is_group_chat(chat_id) else BASE_DELAY_PRIVATE
    return base + random.uniform(0, JITTER_PER_CHAT_MAX)

def global_throttle():
    """Throttle global agar rata-rata <= GLOBAL_MAX_PER_SEC."""
    now = time.time()
    while _last_ticks and (now - _last_ticks[0]) > 1.0:
        _last_ticks.pop(0)
    if len(_last_ticks) >= GLOBAL_MAX_PER_SEC:
        sleep_for = 1.0 - (now - _last_ticks[0]) + 0.01
        print(f"[{now_str()}] ⏳ Throttle global: tidur {sleep_for:.2f}s")
        time.sleep(max(0.0, sleep_for))
    _last_ticks.append(time.time())

def http_get_json(url, params=None, timeout=TIMEOUT_S):
    try:
        r = requests.get(url, params=params or {}, timeout=timeout)
        return True, r.json()
    except Exception as e:
        return False, {"error": "network_error", "detail": str(e)}

def resolve_chat_id(token: str, identifier: str):
    """
    identifier bisa:
      - chat_id numerik (return int-nya)
      - @username
      - t.me/<username>
    Perlu: bot punya akses ke chat tsb (sudah diundang / admin, dll.)
    """
    ident = identifier.strip()

    # Sudah numerik?
    try:
        return int(ident)
    except ValueError:
        pass

    # Normalisasi t.me/... -> @username
    if ident.startswith("https://t.me/") or ident.startswith("http://t.me/") or ident.startswith("t.me/"):
        uname = ident.split("/", 1)[-1]
        if uname and not uname.startswith("@"):
            ident = "@" + uname

    # Kalau belum ada @, tambahkan
    if not ident.startswith("@"):
        ident = "@" + ident

    url = f"https://api.telegram.org/bot{token}/getChat"
    ok, data = http_get_json(url, params={"chat_id": ident})
    if ok and data.get("ok") and data.get("result") and "id" in data["result"]:
        return int(data["result"]["id"])
    return None

def send_with_retry(token: str, chat_id: int, text: str):
    """
    Kirim pesan dengan handling:
    - 429: patuhi retry_after (loop sampai lolos rate limit)
    - 401/403/400: kembalikan error jelas
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
            if attempt <= MAX_RETRIES_OTHER:
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.25)
                print(f"[{now_str()}] ⚠️ Network error: {e}. Retry {attempt}/{MAX_RETRIES_OTHER} dalam {backoff:.2f}s")
                time.sleep(backoff)
                continue
            return False, {"error": "network_error", "detail": str(e)}

        if resp.status_code == 200:
            return True, None

        # 429 Too Many Requests
        if resp.status_code == 429:
            wait = 1
            try:
                data = resp.json()
                wait = data.get("parameters", {}).get("retry_after", 1)
            except Exception:
                pass
            print(f"[{now_str()}] ⚠️ 429 Rate limit. Tunggu {wait}s…")
            time.sleep(wait + 0.25)
            continue

        # Error lain
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}

        code = data.get("error_code", resp.status_code)
        desc = data.get("description", "")

        # 400/401/403 → kembalikan jelas
        if code in (400, 401, 403):
            if code == 401:
                print(f"[{now_str()}] ❌ 401 Unauthorized (token salah/berubah).")
            elif code == 403:
                print(f"[{now_str()}] ❌ 403 Forbidden (bot diblokir / tidak punya hak di chat).")
            else:
                print(f"[{now_str()}] ❌ 400 Bad Request: {desc}")
            return False, data

        # 5xx → retry terbatas
        if 500 <= code <= 599 and attempt <= MAX_RETRIES_OTHER:
            backoff = min(2 ** attempt, 8) + random.uniform(0, 0.25)
            print(f"[{now_str()}] ⚠️ Server error {code}: {desc}. Retry {attempt}/{MAX_RETRIES_OTHER} dalam {backoff:.2f}s")
            time.sleep(backoff)
            continue

        return False, data

def get_bot_identity(token: str):
    url = f"https://api.telegram.org/bot{token}/getMe"
    ok, data = http_get_json(url, timeout=10)
    if ok and data.get("ok"):
        res = data.get("result", {})
        return res.get("username"), res.get("id")
    return None, None

# =========================
# LOGIKA PUTARAN
# =========================
def resolve_all_targets_for_token(token: str, targets: list[str]) -> list[int]:
    """Resolve semua target menjadi chat_id numerik untuk token ini."""
    resolved = []
    for t in targets:
        # Jika numerik, langsung pakai
        try:
            cid = int(t)
            resolved.append(cid)
            continue
        except ValueError:
            pass

        cid = resolve_chat_id(token, t)
        if cid is None:
            print(f"[{now_str()}] ⚠️ Tidak bisa resolve '{t}'. Pastikan bot sudah punya akses & username benar.")
            continue
        resolved.append(cid)
    return resolved

def send_one_round():
    """Kirim 1 putaran ke semua chat untuk setiap bot."""
    print(f"\n[{now_str()}] 🔁 Mulai 1 putaran.")

    for token in tokens:
        uname, uid = get_bot_identity(token)
        if uname:
            print(f"[{now_str()}] Bot aktif: @{uname} (id {uid})")
        else:
            print(f"[{now_str()}] ⚠️ Gagal getMe untuk token ****{token[-6:]}. Lanjut coba kirim…")

        # Resolve target untuk token ini (karena akses/keanggotaan bisa beda per bot)
        resolved_targets = resolve_all_targets_for_token(token, raw_targets)
        if not resolved_targets:
            print(f"[{now_str()}] ⚠️ Tidak ada target yang bisa dipakai untuk token ****{token[-6:]}.\n")
            continue

        print(f"[{now_str()}] ===== Bot ****{token[-6:]} | Target valid: {len(resolved_targets)} =====")
        for chat_id in resolved_targets:
            global_throttle()
            text = random.choice(messages)  # ambil salah satu template

            ok, err = send_with_retry(token, chat_id, text)
            if ok:
                print(f"[{now_str()}] ✅ Terkirim ke {chat_id}")
            else:
                code = (err or {}).get("error_code") or (err or {}).get("status_code") or (err or {}).get("error")
                desc = (err or {}).get("description") or (err or {}).get("detail") or (err or {}).get("text", "")
                print(f"[{now_str()}] ❌ Gagal ke {chat_id}: {code} | {desc}")

            d = delay_for_chat(chat_id)
            print(f"[{now_str()}] 💤 Delay {d:.2f}s sebelum target berikutnya…")
            time.sleep(d)

    print(f"[{now_str()}] ✅ Putaran selesai.")

# =========================
# MAIN LOOP
# =========================
print(f"[{now_str()}] 🚀 Start. Bot: {len(tokens)}, Target (raw): {len(raw_targets)}, Templates: {len(messages)}")

if RUN_FOREVER:
    while True:
        send_one_round()
        jitter = random.uniform(0, JITTER_ROUND_MAX)
        sleep_for = SEND_EVERY_SECONDS + jitter
        print(f"[{now_str()}] ⏲️ Menunggu {sleep_for:.1f}s hingga putaran berikutnya…")
        time.sleep(sleep_for)
else:
    send_one_round()
    print(f"[{now_str()}] 🏁 Selesai (mode sekali jalan).")
