import time
import requests
import board
import busio
import RPi.GPIO as GPIO
from digitalio import DigitalInOut
from adafruit_pn532.spi import PN532_SPI

# ==========================================
# CONFIGURATION
# ==========================================
AIN1, AIN2    = 13, 19          # Motor driver GPIO pins (BCM)
CONVEYOR_SPEED = 50             # Motor duty cycle (0–100%)
SERVER_URL    = "http://192.168.137.1:5001/api/nfc_scan"  # ← PC's IP on hotspot

# ==========================================
# PN532 SPI SETUP
# ==========================================
spi    = busio.SPI(board.SCK, board.MOSI, board.MISO)
cs_pin = DigitalInOut(board.D8)
pn532  = PN532_SPI(spi, cs_pin, debug=False)

# ==========================================
# NFC TAG TEXT READER
# ==========================================
def get_ntag_text():
    """Reads NDEF text payload from NTAG213 pages 4–23."""
    try:
        full_buffer = bytearray()
        for page in range(4, 24):
            data = pn532.ntag2xx_read_block(page)
            if data:
                full_buffer.extend(data)

        # Parse NDEF Text Record: find 0x03 (NDEF start) then 'T' record type
        if 0x03 in full_buffer and b'T' in full_buffer:
            t_idx    = full_buffer.find(b'T')
            lang_len = full_buffer[t_idx + 1] & 0x3F
            text_start = t_idx + 2 + lang_len
            text_end   = full_buffer.find(b'\xfe', text_start)
            if text_end != -1:
                return full_buffer[text_start:text_end].decode('utf-8', errors='ignore').strip('\x00').strip()
    except Exception:
        pass
    return None

def is_valid_string(s):
    """Basic sanity check: printable, length ≥ 4, >80% ASCII printable chars."""
    if not s or len(s) < 4:
        return False
    printable = sum(1 for c in s if c.isprintable())
    return (printable / len(s)) > 0.8

# ==========================================
# SEND TO CENTRAL HUB
# ==========================================
def post_to_server(bag_id, uid_str):
    try:
        resp = requests.post(SERVER_URL, json={"bag_id": bag_id, "uid": uid_str}, timeout=2)
        print(f"  └─ 📡 Server HTTP {resp.status_code} — {resp.json().get('message', '')}")
    except Exception as e:
        print(f"  └─ ⚠️  Could not reach server: {e}")

# ==========================================
# MAIN
# ==========================================
last_uid       = None
last_scan_time = 0
seen_bag_ids   = {}   # local 10s cooldown — same pattern as camera_reader.py

try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(AIN1, GPIO.OUT)
    GPIO.setup(AIN2, GPIO.OUT)
    pwm1 = GPIO.PWM(AIN1, 100)
    pwm2 = GPIO.PWM(AIN2, 100)
    pwm1.start(0)
    pwm2.start(0)
    pn532.SAM_configuration()

    pwm1.ChangeDutyCycle(CONVEYOR_SPEED)

    print("=" * 50)
    print("  LeBag NFC Reader — Online")
    print("=" * 50)
    print(f"  Motor  : AIN1={AIN1}, AIN2={AIN2} | Speed={CONVEYOR_SPEED}%")
    print(f"  NFC    : PN532 via SPI (CS=D8)")
    print(f"  Server : {SERVER_URL}")
    print("-" * 50)
    print("  Belt spinning. Waiting for NFC tags...\n")

    while True:
        uid = pn532.read_passive_target(timeout=0.1)

        if uid is not None:
            now = time.time()

            # Only process if it's a new tag, or the same tag re-scanned after 4s
            if uid != last_uid or (now - last_scan_time) > 4:
                bag_text = get_ntag_text()

                if bag_text and is_valid_string(bag_text):
                    uid_str = '-'.join(hex(b) for b in uid)
                    print(f"\n✅ NFC TAG READ")
                    print(f"   Bag ID : {bag_text}")
                    print(f"   UID    : {uid_str}")

                    # Local 10s cooldown — server also deduplicates, but this
                    # avoids hammering the server with repeated scans of the same tag
                    if bag_text not in seen_bag_ids or (now - seen_bag_ids[bag_text]) > 10:
                        seen_bag_ids[bag_text] = now
                        post_to_server(bag_text, uid_str)
                    else:
                        print(f"   (local cooldown — not re-sending)")

                elif bag_text:
                    print(f"\n⚠️  Garbage NFC read dropped: '{bag_text}'")
                else:
                    print(f"\n⚠️  NFC tag detected but no readable text found")

                last_uid       = uid
                last_scan_time = now

        # Expire old local cooldowns
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