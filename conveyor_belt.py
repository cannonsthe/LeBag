import time
import requests
import board
import busio
import RPi.GPIO as GPIO
from digitalio import DigitalInOut
from adafruit_pn532.spi import PN532_SPI

# --- CONFIG (BCM) ---
AIN1, AIN2 = 13, 19
CONVEYOR_SPEED = 50
RUNTIME_SECONDS = 30
SERVER_URL = "http://192.168.1.100:5001/api/nfc_scan" # Update with PC's IP

# PN532 SPI Setup
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
cs_pin = DigitalInOut(board.D8)
pn532 = PN532_SPI(spi, cs_pin, debug=False)

last_uid = None
last_scan_time = 0

def get_ntag_text():
    """Reads NTAG213 pages directly to avoid buffer repetition."""
    try:
        # NTAG213 user data starts at Page 4.
        # We read 20 pages (80 bytes total) to get the full string.
        full_buffer = bytearray()
        for page in range(4, 24):
            # ntag2xx_read_block returns exactly 4 bytes for NTAG tags
            data = pn532.ntag2xx_read_block(page)
            if data:
                full_buffer.extend(data)

        # Look for NDEF Start (0x03) and Text 'T' (0x54)
        if 0x03 in full_buffer:
            start_idx = full_buffer.find(b'\x03')
            # The byte after 0x03 is the total NDEF length

            if b'T' in full_buffer:
                t_idx = full_buffer.find(b'T')
                lang_len = full_buffer[t_idx + 1] & 0x3F
                text_start = t_idx + 2 + lang_len

                # NDEF messages end with 0xFE. We stop exactly there.
                text_end = full_buffer.find(b'\xfe', text_start)

                if text_end != -1:
                    clean_text = full_buffer[text_start:text_end].decode('utf-8', errors='ignore')
                    return clean_text.strip('\x00').strip()
    except Exception:
        return None
    return None

def is_valid_string(s):
    # Minimum sanity check: must have printable characters, length > 3
    if not s or len(s) < 4: return False
    # Check if mostly ascii printable
    printable = sum(1 for c in s if c.isprintable())
    return (printable / len(s)) > 0.8

# --- MAIN LOOP ---
try:
    GPIO.setup(AIN1, GPIO.OUT); GPIO.setup(AIN2, GPIO.OUT)
    pwm1 = GPIO.PWM(AIN1, 100); pwm2 = GPIO.PWM(AIN2, 100)
    pwm1.start(0); pwm2.start(0)
    pn532.SAM_configuration()

    start_obs = time.time()
    pwm1.ChangeDutyCycle(CONVEYOR_SPEED)

    print(f"Baggage System Online. Runtime: {RUNTIME_SECONDS}s")

    while (time.time() - start_obs) < RUNTIME_SECONDS:
        uid = pn532.read_passive_target(timeout=0.1)

        if uid is not None:
            now = time.time()
            if uid != last_uid or (now - last_scan_time) > 4:
                # OPTIONAL: Slow belt for precision
                pwm1.ChangeDutyCycle(CONVEYOR_SPEED // 2)

                bag_text = get_ntag_text()

                if bag_text and is_valid_string(bag_text):
                    uid_str = '-'.join([hex(i) for i in uid])
                    print(f"\n[{now - start_obs:.1f}s] BAGGAGE IDENTIFIED")
                    print(f">> DATA: {bag_text}")
                    print(f">> UID:  {uid_str}")
                    
                    try:
                        resp = requests.post(SERVER_URL, json={"bag_id": bag_text, "uid": uid_str}, timeout=2)
                        print(f">> SERVER HTTP {resp.status_code}")
                    except Exception as e:
                        print(f">> SERVER ERROR: {e}")
                elif bag_text:
                    print(f"\n[{now - start_obs:.1f}s] DROPPED GARBAGE READ: '{bag_text}'")

                last_uid = uid
                last_scan_time = now
                pwm1.ChangeDutyCycle(CONVEYOR_SPEED)

        time.sleep(0.05)

except Exception as e:
    print(f"Error: {e}")

finally:
    pwm1.ChangeDutyCycle(0)
    GPIO.cleanup()