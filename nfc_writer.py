#!/usr/bin/env python3
"""
nfc_writer.py — Run on Raspberry Pi to write a bag_id to an NFC sticker.

Usage:
  python3 nfc_writer.py LB001

Writes "LB001" as an NDEF Text record to the tag held near the PN532.
"""

import sys
import struct
import board
import busio
from adafruit_pn532.i2c import PN532_I2C

def setup_pn532():
    i2c = busio.I2C(board.SCL, board.SDA)
    pn532 = PN532_I2C(i2c, debug=False)
    ic, ver, rev, support = pn532.firmware_version
    print(f"✅ PN532 Firmware v{ver}.{rev}")
    pn532.SAM_configuration()
    return pn532

def build_ndef_text(text):
    """Build a minimal NDEF Text record for the given string."""
    lang     = b'en'
    lang_len = len(lang)
    payload  = bytes([lang_len]) + lang + text.encode('utf-8')
    # Record: MB=1 ME=1 SR=1 TNF=0x01 | type_len=1 | payload_len | type='T' | payload
    record   = bytes([0xD1, 0x01, len(payload), 0x54]) + payload
    # NDEF TLV: 0x03 | length | record | terminator 0xFE
    ndef     = bytes([0x03, len(record)]) + record + bytes([0xFE])
    return ndef

def write_ndef(pn532, text):
    ndef = build_ndef_text(text)
    # Pad to multiple of 4 bytes (one NTAG page = 4 bytes)
    padded = ndef + b'\x00' * ((4 - len(ndef) % 4) % 4)

    print(f"📝 Writing '{text}' to tag... Hold tag near reader.")
    uid = None
    while uid is None:
        uid = pn532.read_passive_target(timeout=0.5)

    print(f"🏷️  Tag found: UID={uid.hex().upper()}")

    # Write starting at page 4 (user memory on NTAG213/215/216)
    for i, page_num in enumerate(range(4, 4 + len(padded) // 4)):
        chunk = padded[i * 4: i * 4 + 4]
        pn532.ntag2xx_write_block(page_num, chunk)

    print(f"✅ Done! Tag is now programmed with bag ID: '{text}'")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 nfc_writer.py <bag_id>")
        print("Example: python3 nfc_writer.py LB001")
        sys.exit(1)

    bag_id = sys.argv[1].strip()
    pn532  = setup_pn532()
    write_ndef(pn532, bag_id)
