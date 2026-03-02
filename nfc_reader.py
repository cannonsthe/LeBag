#!/usr/bin/env python3
"""
nfc_reader.py — *** RASPBERRY PI ONLY — DO NOT RUN ON WINDOWS ***

This script uses hardware-specific libraries (board, busio, adafruit_pn532)
that only work on a Raspberry Pi. Running it on Windows WILL fail.

DEPLOYMENT STEPS:
  1. Copy this file to your Raspberry Pi 3B
  2. On the Pi, run: pip3 install adafruit-circuitpython-pn532 requests
  3. Set SERVER_IP below to your PC's IP address (the machine running server.py)
  4. Run on Pi: python3 nfc_reader.py

FLOW:
  NFC tag scanned → reads "LB001" text → POST /api/nfc_scan to PC
  PC server.py handles DB lookup + Telegram notification

Wiring (PN532 HAT / breakout → Pi 3B):
  VCC  → 3.3V  (Pin 1)
  GND  → GND   (Pin 6)
  SDA  → GPIO2 (Pin 3)
  SCL  → GPIO3 (Pin 5)
"""

import time
import board
import busio
import requests
from adafruit_pn532.i2c import PN532_I2C

# ── CONFIGURATION ─────────────────────────────────────────────────
SERVER_IP   = "192.168.1.100"   # ← Change to your PC's IP running server.py
SERVER_PORT = 5001
COOLDOWN    = 3.0               # Seconds to wait before re-reading the same tag
# ──────────────────────────────────────────────────────────────────

API_URL = f"http://{SERVER_IP}:{SERVER_PORT}/api/nfc_scan"

def setup_pn532():
    i2c = busio.I2C(board.SCL, board.SDA)
    pn532 = PN532_I2C(i2c, debug=False)
    ic, ver, rev, support = pn532.firmware_version
    print(f"✅ PN532 found — Firmware v{ver}.{rev}")
    pn532.SAM_configuration()
    return pn532

def read_ndef_text(pn532):
    """
    Read the first NDEF Text record from the tag.
    NFC tags written with 'LB001' style bag IDs use NDEF Text records.
    Returns the text string, or None if unreadable.
    """
    uid = pn532.read_passive_target(timeout=0.5)
    if uid is None:
        return None, None

    uid_str = uid.hex().upper()

    # Read page 4 onwards (NDEF data starts at page 4 on NTAG213/215/216)
    try:
        data = bytearray()
        for page in range(4, 16):          # Read enough pages to cover NDEF header + text
            block = pn532.ntag2xx_read_block(page)
            if block:
                data.extend(block)

        # NDEF Text record structure:
        # TLV: 0x03 (NDEF) | length | 0xD1 0x01 | text_len | 0x54 | lang_len | lang | text
        i = 0
        while i < len(data):
            if data[i] == 0x03:             # NDEF TLV tag
                ndef_len = data[i + 1]
                ndef_payload = data[i + 2: i + 2 + ndef_len]
                # Skip TNF + type length + payload length + type ('T' = 0x54)
                if len(ndef_payload) > 5 and ndef_payload[3] == 0x54:
                    lang_len = ndef_payload[4] & 0x3F
                    text_start = 5 + lang_len
                    text = ndef_payload[text_start:].decode('utf-8', errors='ignore').strip('\x00')
                    return uid_str, text
                break
            i += 1
    except Exception as e:
        print(f"⚠️  NDEF read error: {e}")

    # Fallback: use UID as the bag_id if NDEF unreadable
    return uid_str, uid_str

def post_to_server(bag_id, uid):
    """Send the scanned bag_id to server.py."""
    try:
        resp = requests.post(API_URL, json={"bag_id": bag_id, "uid": uid}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Server accepted: {data.get('message', 'OK')} (owner: {data.get('owner', '?')})")
        else:
            print(f"⚠️  Server returned {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"❌ Could not reach server at {API_URL}: {e}")

def main():
    print(f"🔌 Connecting to PN532 over I2C...")
    pn532 = setup_pn532()
    print(f"📡 Will send scans to {API_URL}")
    print("📎 Hold an NFC tag near the reader...\n")

    last_uid    = None
    last_time   = 0

    while True:
        uid_str, bag_id = read_ndef_text(pn532)

        if bag_id is None:
            time.sleep(0.1)
            continue

        now = time.time()

        # Cooldown — ignore same tag within COOLDOWN seconds
        if uid_str == last_uid and (now - last_time) < COOLDOWN:
            time.sleep(0.1)
            continue

        last_uid  = uid_str
        last_time = now

        print(f"🏷️  Tag detected! UID={uid_str}  Bag ID='{bag_id}'")
        post_to_server(bag_id, uid_str)

if __name__ == "__main__":
    main()
