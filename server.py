import cv2
import numpy as np
import time
import threading
import json
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
from pyzbar.pyzbar import decode, ZBarSymbol

# --- CONFIGURATION ---
# ┌─────────────────────────────────────────────────────────────────────┐
# │  STREAM 1 (Pre-belt, ~2 min out): Raspberry Pi Camera OR NFC scan  │
# │  Pi camera streams QR detection here. NFC tags bypass this and     │
# │  POST directly to /api/nfc_scan from nfc_reader.py on the Pi.      │
# │  Set to the Pi's IP — e.g. "tcp://192.168.1.XXX:5000"             │
# └─────────────────────────────────────────────────────────────────────┘
PI_STREAM_URL = "tcp://192.168.2.2:5000"  # ← Pi camera TCP stream (pre-belt)

# ┌─────────────────────────────────────────────────────────────────────┐
# │  STREAM 2 (On-belt): Phone camera — handled by tracker.py          │
# │  Set ANDROID_STREAM_URL in tracker.py to your phone's IP Webcam    │
# │  URL e.g. "http://192.168.1.XXX:8080/video"                        │
# └─────────────────────────────────────────────────────────────────────┘
SERVER_PORT = 5001
NOTIFICATION_SERVER_URL = "http://localhost:3000"  # notification/tele.js
BAG_DB_FILE = os.path.join(os.path.dirname(__file__), "bag_database.json")

# Owner name → Telegram Chat ID (fallback when chat_id not stored in DB)
NAME_TO_CHAT_ID = {
    "Marcus":  "526465552",
    "Leonard": "576404494",
    "Ashok":   "1103762109",
    "Balaji":  "940148369",
    "YiBin":   "5554044576",
}

# --- FLASK APP SETUP ---
app = Flask(__name__)
CORS(app)

# Shared State
pending_queue = []
bags = []
bag_id_counter = 1
seen_codes = set()
lock = threading.Lock()

# ==========================================
# BAG DATABASE HELPER
# ==========================================

def load_bag_database():
    """Load bag_database.json; returns {} on error."""
    try:
        with open(BAG_DB_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️  Could not load bag_database.json: {e}")
        return {}

# ==========================================
# NOTIFICATION HELPERS
# ==========================================

def trigger_notification(bag_id, owner, chat_id):
    """Fire the '2 minutes away' Telegram alert and host the live tracking URL."""
    if not chat_id:
        print(f"⚠️  No chat_id for '{owner}' — skipping Telegram notification.")
        return
    try:
        resp = requests.post(
            f"{NOTIFICATION_SERVER_URL}/api/trigger_notification",
            json={"bag_id": bag_id, "chat_id": chat_id, "owner": owner},
            timeout=5
        )
        print(f"🔔 Notification sent for '{owner}' → chat {chat_id} (HTTP {resp.status_code})")
    except Exception as e:
        print(f"⚠️  Could not reach notification server: {e}")

# ==========================================
# FLASK API ENDPOINTS
# ==========================================

@app.route('/api/nfc_scan', methods=['POST'])
def nfc_scan():
    """
    Called by nfc_reader.py on the Raspberry Pi when a tag is scanned.
    body: { "bag_id": "LB001", "uid": "AABBCCDD" }
    Runs the same pipeline as a QR scan.
    """
    data   = request.json
    bag_id = (data or {}).get('bag_id', '').strip()
    uid    = (data or {}).get('uid', '')

    if not bag_id:
        return jsonify({"error": "Missing bag_id"}), 400

    print(f"\n📎 [NFC] Tag scanned — Bag ID: '{bag_id}'  UID: {uid}")

    bag_db    = load_bag_database()
    bag_entry = bag_db.get(bag_id, {})

    owner    = bag_entry.get("owner", "Unknown")
    bag_type = bag_entry.get("type", bag_id)
    flight   = bag_entry.get("flight", "Unknown")
    chat_id  = bag_entry.get("chat_id", "")

    # Name → chat_id fallback
    if not chat_id:
        chat_id = NAME_TO_CHAT_ID.get(owner, "")

    # Fallback: parse CSV embedded in bag_id itself (e.g. "Marcus,Suitcase,SQ421")
    if owner == "Unknown" and "," in bag_id:
        parts = bag_id.split(",")
        if len(parts) >= 3:
            owner    = parts[0].strip()
            bag_type = parts[1].strip()
            flight   = parts[2].strip()
            if not chat_id:
                chat_id = NAME_TO_CHAT_ID.get(owner, "")

    print(f"📎 [NFC] Resolved → Owner: {owner} | Chat ID: {chat_id or 'N/A'}")

    with lock:
        pending_queue.append(owner)
        print(f"➕ [NFC] Queued '{owner}' for tracker (Total: {len(pending_queue)})")

    add_bag(owner, bag_type, flight)

    threading.Thread(
        target=trigger_notification,
        args=(bag_id, owner, chat_id),
        daemon=True
    ).start()

    return jsonify({"message": "NFC scan processed", "owner": owner, "flight": flight}), 200

@app.route('/enroll', methods=['POST'])
def enroll_bag_external():
    """Endpoint for the Pi to register a bag directly if needed."""
    data = request.json
    if not data or 'name' not in data:
        return jsonify({"error": "Missing 'name' field"}), 400
    with lock:
        pending_queue.append(data['name'])
        print(f"📥 [API] Added '{data['name']}' to pending queue (Total: {len(pending_queue)})")
    return jsonify({"message": "Bag added to queue", "queue_size": len(pending_queue)}), 200

@app.route('/api/pop_pending', methods=['GET'])
def pop_pending():
    """Endpoint for tracker.py to pop the next passenger name."""
    with lock:
        if pending_queue:
            name = pending_queue.pop(0)
            print(f"📤 [API] Popped '{name}' from pending queue (Remaining: {len(pending_queue)})")
            return jsonify({"name": name}), 200
        else:
            return jsonify({"name": None}), 200

@app.route('/api/new_bag', methods=['POST'])
def new_bag():
    """Endpoint to manually add a bag (kept for frontend compatibility)"""
    data = request.json
    if not data or 'owner' not in data or 'type' not in data or 'flight' not in data:
        return jsonify({"error": "Missing required fields"}), 400
    add_bag(data['owner'], data['type'], data['flight'])
    return jsonify({"message": "Bag added successfully"}), 201

@app.route('/api/bags', methods=['GET'])
def get_bags():
    """Endpoint for React frontend to poll"""
    with lock:
        return jsonify(bags), 200

@app.route('/api/luggage_zone', methods=['POST'])
def luggage_zone():
    """Relay zone change from tracker.py → notification server."""
    data = request.json
    if not data or 'owner' not in data or 'zone' not in data:
        return jsonify({"error": "Missing owner or zone"}), 400

    owner = data['owner']
    zone  = data['zone']
    print(f"📍 [Relay] Zone update: {owner} → Zone {zone}")

    try:
        resp = requests.post(
            f"{NOTIFICATION_SERVER_URL}/api/zone_update",
            json={"owner": owner, "zone": zone},
            timeout=5
        )
        print(f"✅ [Relay] Zone forwarded (HTTP {resp.status_code})")
    except Exception as e:
        print(f"⚠️  [Relay] Zone relay failed: {e}")

    return jsonify({"message": "Zone relayed"}), 200

@app.route('/api/luggage_collected', methods=['POST'])
def luggage_collected():
    """Relay bag-collected event from tracker.py → notification server."""
    data = request.json
    if not data or 'owner' not in data:
        return jsonify({"error": "Missing owner"}), 400

    owner = data['owner']
    print(f"✅ [Relay] Bag collected: {owner}")

    try:
        resp = requests.post(
            f"{NOTIFICATION_SERVER_URL}/api/bag_collected",
            json={"owner": owner},
            timeout=5
        )
        print(f"✅ [Relay] Collected forwarded (HTTP {resp.status_code})")
    except Exception as e:
        print(f"⚠️  [Relay] Collected relay failed: {e}")

    return jsonify({"message": "Collection relayed"}), 200

# ==========================================
# BAG LIST HELPER
# ==========================================

def add_bag(owner, bag_type, flight):
    """Helper to safely add a bag to the in-memory list."""
    global bag_id_counter
    with lock:
        new_bag_entry = {
            "id": bag_id_counter,
            "owner": owner,
            "type": bag_type,
            "flight": flight,
            "timestamp": datetime.now().isoformat()
        }
        bags.insert(0, new_bag_entry)
        bag_id_counter += 1
        print(f"✅ New Bag Added: {new_bag_entry}")

# ==========================================
# FLASK THREAD
# ==========================================

def run_server():
    print(f"🚀 LeBag Server Backend Running on http://localhost:{SERVER_PORT}")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)

# ==========================================
# QR SCANNER LOOP
# ==========================================

def run_scanner():
    global seen_codes

    print(f"📷 Scanner Connecting to {PI_STREAM_URL}...")
    try:
        cap = cv2.VideoCapture(PI_STREAM_URL)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            print("⚠️ Could not open stream. Falling back to webcam (0).")
            cap = cv2.VideoCapture(0)
            current_stream_mode = "LOCAL"
        else:
            current_stream_mode = "TCP"
    except Exception as e:
        print(f"⚠️ Error: {e}. Falling back to webcam (0).")
        cap = cv2.VideoCapture(0)
        current_stream_mode = "LOCAL"

    last_reconnect_time = time.time()
    reconnect_thread = None
    test_cap_result = []
    consistency_counter = {}
    CONSISTENCY_THRESHOLD = 5

    print("📷 Scanner Running... Looking for QR Codes (Press 'q' to quit)")

    while True:
        try:
            # Background reconnect to primary stream
            if current_stream_mode == "LOCAL" and time.time() - last_reconnect_time > 5.0:
                if reconnect_thread is None or not reconnect_thread.is_alive():
                    if not test_cap_result:
                        print(f"🔄 Attempting to reconnect to {PI_STREAM_URL}...")
                        def _try_reconnect():
                            temp_cap = cv2.VideoCapture(PI_STREAM_URL)
                            if temp_cap.isOpened():
                                test_cap_result.append(temp_cap)
                            else:
                                temp_cap.release()
                        reconnect_thread = threading.Thread(target=_try_reconnect, daemon=True)
                        reconnect_thread.start()
                        last_reconnect_time = time.time()

            if test_cap_result:
                print("✅ Reconnected to primary stream!")
                cap.release()
                cap = test_cap_result.pop(0)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                current_stream_mode = "TCP"

            # Drain buffer for lowest latency
            for _ in range(4):
                cap.grab()

            ret, frame = cap.read()
            if not ret:
                if current_stream_mode == "TCP":
                    print("⚠️ Stream lost! Falling back to webcam.")
                    cap.release()
                    cap = cv2.VideoCapture(0)
                    current_stream_mode = "LOCAL"
                    last_reconnect_time = time.time()
                else:
                    time.sleep(0.1)
                continue

            decoded_objects = decode(frame, symbols=[ZBarSymbol.QRCODE, ZBarSymbol.EAN13])
            current_frame_codes = set()

            for obj in decoded_objects:
                data = obj.data.decode('utf-8')
                code_type = obj.type
                current_frame_codes.add(data)

                x, y, w, h = obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 2)

                consistency_counter[data] = consistency_counter.get(data, 0) + 1

                if consistency_counter[data] >= CONSISTENCY_THRESHOLD:
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
                    cv2.putText(frame, f"{code_type}: {data}", (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

                    if data not in seen_codes:
                        print(f"✅ CONFIRMED NEW {code_type}: {data}")
                        seen_codes.add(data)

                        # --- Look up bag info from database ---
                        bag_db    = load_bag_database()
                        bag_entry = bag_db.get(data, {})

                        owner    = bag_entry.get("owner", "Unknown")
                        bag_type = bag_entry.get("type", data)
                        flight   = bag_entry.get("flight", "Unknown")
                        chat_id  = bag_entry.get("chat_id", "")

                        # Fallback: resolve chat_id by name if not in DB entry
                        if not chat_id:
                            chat_id = NAME_TO_CHAT_ID.get(owner, "")

                        # Fallback: parse owner/type/flight embedded in QR value
                        if owner == "Unknown":
                            if "," in data:
                                parts = data.split(",")
                                if len(parts) >= 3:
                                    owner    = parts[0].strip()
                                    bag_type = parts[1].strip()
                                    flight   = parts[2].strip()
                            elif ":" in data:
                                parts = data.split(":")
                                if len(parts) >= 2:
                                    owner = parts[1].strip()
                            # Try name→chat_id again after parsing
                            if not chat_id:
                                chat_id = NAME_TO_CHAT_ID.get(owner, "")

                        print(f"📥 Mapped '{data}' → Owner: {owner} | Chat ID: {chat_id or 'N/A'}")

                        with lock:
                            pending_queue.append(owner)
                            print(f"➕ Queued '{owner}' for tracker (Total: {len(pending_queue)})")

                        add_bag(owner, bag_type, flight)

                        # --- Fire Telegram + open live website link ---
                        threading.Thread(
                            target=trigger_notification,
                            args=(data, owner, chat_id),
                            daemon=True
                        ).start()

            for code in list(consistency_counter.keys()):
                if code not in current_frame_codes:
                    consistency_counter[code] = 0

        except Exception as e:
            print(f"Error in scanner loop: {e}")

        cv2.imshow("LeBag Scanner", frame)
        if cv2.waitKey(1) == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == '__main__':
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    run_scanner()