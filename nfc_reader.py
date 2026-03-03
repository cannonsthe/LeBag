import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import board
import busio
import RPi.GPIO as GPIO
from digitalio import DigitalInOut
from adafruit_pn532.spi import PN532_SPI

# ==========================================
# CONFIGURATION (all set by start_pi.sh)
# ==========================================
AIN1, AIN2       = 13, 19
CONVEYOR_SPEED   = int(os.environ.get('LEBAG_SPEED', '50'))
CONVEYOR_ENABLED = os.environ.get('LEBAG_CONVEYOR', '1') != '0'
RUNTIME_SECONDS  = int(os.environ.get('LEBAG_RUNTIME', '0'))   # 0 = run forever
NFC_SERVER_PORT  = int(os.environ.get('LEBAG_NFC_PORT', '5002'))

# ==========================================
# SCAN QUEUE — thread-safe for HTTP server
# ==========================================
scan_queue = []
scan_lock  = threading.Lock()

def push_scan(bag_id, uid_str, card_type):
    """Add a confirmed scan to the queue for the PC to pick up."""
    with scan_lock:
        scan_queue.append({
            "bag_id":    bag_id,
            "uid":       uid_str,
            "card_type": card_type,
            "time":      time.time()
        })

# ==========================================
# LIGHTWEIGHT HTTP SERVER
# PC polls GET /api/nfc_scan to drain the queue.
# Pi doesn't need to know the PC's IP at all.
# ==========================================
class NfcHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/nfc_scan':
            with scan_lock:
                payload = list(scan_queue)
                scan_queue.clear()
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Suppress default access logs to keep terminal clean

def start_http_server():
    server = HTTPServer(('0.0.0.0', NFC_SERVER_PORT), NfcHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

# ==========================================
# MIFARE CLASSIC AUTH KEYS (NFC Forum NDEF)
# ==========================================
MIFARE_CMD_AUTH_A = 0x60
MIFARE_CMD_AUTH_B = 0x61
KEY_DEFAULT       = b'\xFF\xFF\xFF\xFF\xFF\xFF'
KEY_MAD           = b'\xA0\xA1\xA2\xA3\xA4\xA5'  # Sector 0 MAD key
KEY_NDEF_DATA     = b'\xD3\xF7\xD3\xF7\xD3\xF7'  # Sectors 1–15 NDEF key

# ==========================================
# PN532 SPI SETUP
# ==========================================
spi    = busio.SPI(board.SCK, board.MOSI, board.MISO)
cs_pin = DigitalInOut(board.D8)
pn532  = PN532_SPI(spi, cs_pin, debug=False)

# ==========================================
# NDEF TEXT PARSER (shared for NTAG + Mifare)
# ==========================================
def parse_ndef_text(buffer):
    try:
        if 0x03 in buffer and b'T' in buffer:
            t_idx      = buffer.find(b'T')
            lang_len   = buffer[t_idx + 1] & 0x3F
            text_start = t_idx + 2 + lang_len
            text_end   = buffer.find(b'\xfe', text_start)
            if text_end != -1:
                text = buffer[text_start:text_end].decode('utf-8', errors='ignore').strip('\x00').strip()
                if text:
                    return text
        text = bytes(buffer).decode('utf-8', errors='ignore').strip('\x00').strip()
        return text if is_valid_string(text) else None
    except Exception:
        return None

# ==========================================
# NTAG213 READER
# ==========================================
def get_ntag_text():
    try:
        full_buffer = bytearray()
        for page in range(4, 24):
            data = pn532.ntag2xx_read_block(page)
            if data:
                full_buffer.extend(data)
        return parse_ndef_text(full_buffer)
    except Exception:
        pass
    return None

# ==========================================
# MIFARE CLASSIC 1K READER
# ==========================================
def get_mifare_text(uid):
    READ_PLAN = [
        (0, [1, 2],     KEY_MAD),
        (1, [4, 5, 6],  KEY_NDEF_DATA),
        (2, [8, 9, 10], KEY_NDEF_DATA),
    ]
    full_buffer = bytearray()
    try:
        for sector, blocks, key_a in READ_PLAN:
            auth_block = sector * 4 + 3
            authenticated = False
            for key in (key_a, KEY_DEFAULT):
                authenticated = pn532.mifare_classic_authenticate_block(
                    uid, auth_block, MIFARE_CMD_AUTH_A, key
                )
                if authenticated:
                    break
            if not authenticated:
                print(f"   [Mifare] ⚠️  Auth failed for sector {sector}")
                continue
            for block_num in blocks:
                data = pn532.mifare_classic_read_block(block_num)
                if data:
                    full_buffer.extend(data)
                    printable = bytes(data).decode('utf-8', errors='replace').strip('\x00').strip()
                    print(f"   [Mifare] Block {block_num:02d}: {printable!r}")
        if full_buffer:
            return parse_ndef_text(full_buffer)
    except Exception as e:
        print(f"   [Mifare] Read error: {e}")
    return None

# ==========================================
# CARD TYPE AUTO-DETECT
# ==========================================
def read_bag_id(uid):
    uid_len = len(uid)
    if uid_len == 7:
        return get_ntag_text(), "NTAG213"
    elif uid_len == 4:
        return get_mifare_text(uid), "Mifare Classic"
    else:
        return get_ntag_text() or get_mifare_text(uid), "Unknown"

# ==========================================
# VALIDATION
# ==========================================
def is_valid_string(s):
    if not s or len(s) < 4:
        return False
    printable = sum(1 for c in s if c.isprintable())
    return (printable / len(s)) > 0.8

# ==========================================
# MAIN
# ==========================================
last_uid       = None
last_scan_time = 0
seen_bag_ids   = {}

try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(AIN1, GPIO.OUT)
    GPIO.setup(AIN2, GPIO.OUT)
    pwm1 = GPIO.PWM(AIN1, 100)
    pwm2 = GPIO.PWM(AIN2, 100)
    pwm1.start(0)
    pwm2.start(0)
    pn532.SAM_configuration()

    if CONVEYOR_ENABLED:
        pwm1.ChangeDutyCycle(CONVEYOR_SPEED)

    http_server = start_http_server()

    print("=" * 54)
    print("  LeBag NFC Reader — Online")
    print("=" * 54)
    print(f"  Motor      : {'ON @ ' + str(CONVEYOR_SPEED) + '%' if CONVEYOR_ENABLED else 'OFF'}")
    print(f"  NFC server : http://0.0.0.0:{NFC_SERVER_PORT}/api/nfc_scan")
    print(f"  Card types : NTAG213 + Mifare Classic 1K")
    print(f"  Runtime    : {'∞ (until Ctrl+C)' if RUNTIME_SECONDS == 0 else str(RUNTIME_SECONDS) + 's'}")
    print("-" * 54)
    print("  PC polls this Pi for NFC scans (no PC IP needed here)")
    print("  Waiting for NFC tags...\n")

    start_time = time.time()

    while True:
        # Check runtime limit
        if RUNTIME_SECONDS > 0 and (time.time() - start_time) >= RUNTIME_SECONDS:
            print(f"\n⏱️  Runtime of {RUNTIME_SECONDS}s reached. Stopping.")
            break

        uid = pn532.read_passive_target(timeout=0.1)

        if uid is not None:
            now = time.time()
            if uid != last_uid or (now - last_scan_time) > 4:
                uid_str = '-'.join(hex(b) for b in uid)
                bag_text, card_type = read_bag_id(uid)

                if bag_text and is_valid_string(bag_text):
                    print(f"\n✅ NFC TAG READ [{card_type}]")
                    print(f"   Bag ID : {bag_text}")
                    print(f"   UID    : {uid_str}")

                    if bag_text not in seen_bag_ids or (now - seen_bag_ids[bag_text]) > 10:
                        seen_bag_ids[bag_text] = now
                        push_scan(bag_text, uid_str, card_type)
                        print(f"   → Queued for PC to collect")
                    else:
                        print(f"   (local cooldown — not re-queuing)")

                elif bag_text:
                    print(f"\n⚠️  Garbage read dropped [{card_type}]: '{bag_text}'")
                else:
                    print(f"\n⚠️  [{card_type}] tag detected — no readable text (UID: {uid_str})")

                last_uid       = uid
                last_scan_time = now

        # Expire local cooldowns
        now = time.time()
        seen_bag_ids = {k: v for k, v in seen_bag_ids.items() if now - v < 10}

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n🛑 Stopped by user.")

except Exception as e:
    print(f"\n❌ Fatal error: {e}")

finally:
    pwm1.ChangeDutyCycle(0)
    GPIO.cleanup()
    print("✅ GPIO cleaned up.")