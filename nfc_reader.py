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
AIN1, AIN2     = 13, 19
CONVEYOR_SPEED = 50
SERVER_URL     = "http://192.168.137.1:5001/api/nfc_scan"  # ← PC's IP on hotspot

# Mifare Classic authentication keys
# NFC Tools (and most Android NFC apps) write NDEF to Mifare Classic using
# the NFC Forum spec keys — NOT the default 0xFF key:
#   Sector 0  (MAD)  → Key A: A0 A1 A2 A3 A4 A5
#   Sectors 1-15     → Key A: D3 F7 D3 F7 D3 F7
# We also try the plain default as a last resort for raw-written cards.
MIFARE_CMD_AUTH_A  = 0x60
MIFARE_CMD_AUTH_B  = 0x61
KEY_DEFAULT        = b'\xFF\xFF\xFF\xFF\xFF\xFF'  # Factory default
KEY_MAD            = b'\xA0\xA1\xA2\xA3\xA4\xA5'  # Sector 0 (MAD) NDEF key
KEY_NDEF_DATA      = b'\xD3\xF7\xD3\xF7\xD3\xF7'  # Sectors 1-15 NDEF key

# ==========================================
# PN532 SPI SETUP
# ==========================================
spi    = busio.SPI(board.SCK, board.MOSI, board.MISO)
cs_pin = DigitalInOut(board.D8)
pn532  = PN532_SPI(spi, cs_pin, debug=False)

# ==========================================
# NTAG213 TEXT READER
# Reads NDEF text from pages 4–23 (80 bytes)
# ==========================================
def parse_ndef_text(buffer):
    """Shared NDEF Text Record parser — works for NTAG213 and Mifare Classic alike.
    Looks for 0x03 (NDEF TLV) then a 'T' Text Record and extracts the payload."""
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
        # Fallback: raw UTF-8 if there's no NDEF wrapper (plain-text write)
        text = bytes(buffer).decode('utf-8', errors='ignore').strip('\x00').strip()
        return text if is_valid_string(text) else None
    except Exception:
        return None

def get_ntag_text():
    """Reads NDEF text payload from NTAG213 pages 4–23."""
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

def get_mifare_text(uid):
    """Reads NDEF text from a Mifare Classic 1K card written with NFC Tools.
    Uses NFC Forum NDEF sector keys (not the default FF key)."""
    READ_PLAN = [
        (0, [1, 2],      KEY_MAD),        # Sector 0 — MAD key
        (1, [4, 5, 6],   KEY_NDEF_DATA),  # Sector 1 — NDEF data key (most likely location)
        (2, [8, 9, 10],  KEY_NDEF_DATA),  # Sector 2 — NDEF data key
    ]

    full_buffer = bytearray()

    try:
        for sector, blocks, key_a in READ_PLAN:
            auth_block = sector * 4 + 3  # Sector trailer block number

            # Try the assigned NDEF key first, fall back to default
            authenticated = False
            for key in (key_a, KEY_DEFAULT):
                authenticated = pn532.mifare_classic_authenticate_block(
                    uid, auth_block, MIFARE_CMD_AUTH_A, key
                )
                if authenticated:
                    break

            if not authenticated:
                print(f"   [Mifare] ⚠️  Auth failed for sector {sector} with all keys")
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
# VALIDATION
# ==========================================
def is_valid_string(s):
    """Sanity check: printable, length ≥ 4, >80% ASCII printable chars."""
    if not s or len(s) < 4:
        return False
    printable = sum(1 for c in s if c.isprintable())
    return (printable / len(s)) > 0.8

# ==========================================
# AUTO-DETECT CARD TYPE AND READ
# NTAG213  → 7-byte UID
# Mifare Classic 1K → 4-byte UID
# ==========================================
def read_bag_id(uid):
    """Auto-detects card type from UID length and reads the bag ID."""
    uid_len = len(uid)

    if uid_len == 7:
        # Almost certainly NTAG21x family
        text = get_ntag_text()
        card_type = "NTAG213"
    elif uid_len == 4:
        # Almost certainly Mifare Classic 1K
        text = get_mifare_text(uid)
        card_type = "Mifare Classic"
    else:
        # Unknown — try both
        text = get_ntag_text() or get_mifare_text(uid)
        card_type = "Unknown"

    return text, card_type

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
seen_bag_ids   = {}   # local 10s cooldown

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
    print(f"  Motor      : AIN1={AIN1}, AIN2={AIN2} | Speed={CONVEYOR_SPEED}%")
    print(f"  NFC        : PN532 via SPI (CS=D8)")
    print(f"  Card types : NTAG213 (7-byte UID) + Mifare Classic 1K (4-byte UID)")
    print(f"  Server     : {SERVER_URL}")
    print("-" * 50)
    print("  Belt spinning. Waiting for NFC tags...\n")

    while True:
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
                        post_to_server(bag_text, uid_str)
                    else:
                        print(f"   (local cooldown — not re-sending)")

                elif bag_text:
                    print(f"\n⚠️  Garbage NFC read dropped [{card_type}]: '{bag_text}'")
                else:
                    print(f"\n⚠️  [{card_type}] tag detected (UID: {uid_str}) — no readable text found")

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