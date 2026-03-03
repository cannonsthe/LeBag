import time
import threading
import json
import os
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

# --- CONFIGURATION ---
SERVER_PORT = 5001
NOTIFICATION_SERVER_URL = "http://localhost:3000"
BAG_DB_FILE = os.path.join(os.path.dirname(__file__), "bag_database.json")

# Pi NFC server — PC polls this instead of Pi POSTing to PC
# Set via LEBAG_PI_IP env var (from start_lebag.bat) or config.env
_pi_ip = os.environ.get('LEBAG_PI_IP', '')
PI_NFC_URL = f"http://{_pi_ip}:5002/api/nfc_scan" if _pi_ip else None

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
lock = threading.Lock()

# Robustness Engine State
recent_scans = {}
current_scan_candidates = []
robustness_timer_active = False

# ==========================================
# BAG DATABASE HELPER
# ==========================================

def load_bag_database():
    """Load bag_database.json; returns {} on error."""
    try:
        if not os.path.exists(BAG_DB_FILE):
             return {}
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
# ROBUSTNESS ENGINE
# ==========================================

def execute_bag_processing(bag_id, raw_data, source):
    """Process the winning bag_id after the robustness window closes."""
    
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
                
    # Fallback: parse colon embedded
    if owner == "Unknown" and ":" in bag_id:
        parts = bag_id.split(":")
        if len(parts) >= 2:
            owner = parts[1].strip()
            if not chat_id:
                chat_id = NAME_TO_CHAT_ID.get(owner, "")

    print(f"✅ [WINNER] Source: {source} | Bag ID: '{bag_id}' → Owner: {owner} | Chat ID: {chat_id or 'N/A'}")

    with lock:
        # Final safety check for 10s deduplication (just in case another thread snuck in)
        if bag_id in recent_scans and time.time() - recent_scans[bag_id] < 10:
            return
        
        recent_scans[bag_id] = time.time()
        
        pending_queue.append(owner)
        print(f"➕ Queued '{owner}' for tracker (Total: {len(pending_queue)})")

    add_bag(owner, bag_type, flight)

    threading.Thread(
        target=trigger_notification,
        args=(bag_id, owner, chat_id),
        daemon=True
    ).start()

def process_candidates():
    """Timer callback to choose the best scan from the 1.0s window."""
    global current_scan_candidates, robustness_timer_active
    
    with lock:
        candidates = current_scan_candidates.copy()
        current_scan_candidates.clear()
        robustness_timer_active = False
        
    if not candidates:
        return
        
    best_candidate = None
    best_score = -1
    bag_db = load_bag_database()
    
    for c in candidates:
        bid = c["bag_id"]
        score = 0
        if bid in bag_db: score += 100
        if "," in bid or ":" in bid: score += 50
        if bid.isalnum(): score += 10
        if len(bid) >= 4: score += 5
        
        if score > best_score:
            best_score = score
            best_candidate = c
            
    if best_candidate and best_score > 0:
        execute_bag_processing(best_candidate["bag_id"], best_candidate["raw_data"], best_candidate["source"])
    elif best_candidate:
        print(f"⚠️  [ROBUSTNESS] Dumped garbage scan: '{best_candidate['bag_id']}'")

# ==========================================
# FLASK API ENDPOINTS
# ==========================================

@app.route('/api/nfc_scan', methods=['POST'])
def nfc_scan():
    global robustness_timer_active
    data   = request.json
    bag_id = (data or {}).get('bag_id', '').strip()
    
    if not bag_id:
        return jsonify({"error": "Missing bag_id"}), 400
        
    print(f"📎 [NFC] Raw scan received: '{bag_id}'")
        
    with lock:
        if bag_id in recent_scans and time.time() - recent_scans[bag_id] < 10:
            print(f"  └─ 🔁 [NFC] Duplicate ignored (within 10s cooldown): '{bag_id}'")
            return jsonify({"message": "Duplicate ignored"}), 200
            
        current_scan_candidates.append({"bag_id": bag_id, "source": "nfc", "raw_data": data})
        print(f"  └─ ⏳ [NFC] Queued. Waiting 1.0s for other sensors...") 
        
        if not robustness_timer_active:
            robustness_timer_active = True
            threading.Timer(1.0, process_candidates).start()
            
    return jsonify({"message": "Scan queued for robustness check"}), 202

@app.route('/api/camera_scan', methods=['POST'])
def camera_scan():
    global robustness_timer_active
    data   = request.json
    bag_id = (data or {}).get('qr_data', '').strip()
    
    if not bag_id:
        return jsonify({"error": "Missing qr_data"}), 400
        
    print(f"📷 [CAM] Raw scan received: '{bag_id}'")
        
    with lock:
        if bag_id in recent_scans and time.time() - recent_scans[bag_id] < 10:
            print(f"  └─ 🔁 [CAM] Duplicate ignored (within 10s cooldown): '{bag_id}'")
            return jsonify({"message": "Duplicate ignored"}), 200
            
        current_scan_candidates.append({"bag_id": bag_id, "source": "camera", "raw_data": data})
        print(f"  └─ ⏳ [CAM] Queued. Waiting 1.0s for other sensors...")
        
        if not robustness_timer_active:
            robustness_timer_active = True
            threading.Timer(1.0, process_candidates).start()
            
    return jsonify({"message": "Scan queued for robustness check"}), 202

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

def poll_pi_nfc():
    """Background thread: polls the Pi's NFC HTTP server every 500ms.
    Feeds results into the same robustness engine as camera scans."""
    global robustness_timer_active
    if not PI_NFC_URL:
        print("⚠️  [NFC Poller] PI_IP not set — NFC polling disabled.")
        print("   Set LEBAG_PI_IP env var or re-run via start_lebag.bat")
        return
    print(f"🔌 [NFC Poller] Polling Pi at {PI_NFC_URL}")
    while True:
        try:
            resp = requests.get(PI_NFC_URL, timeout=1)
            if resp.status_code == 200:
                scans = resp.json()
                for scan in scans:
                    bag_id = scan.get('bag_id', '').strip()
                    if not bag_id:
                        continue
                    print(f"📎 [NFC] Received from Pi: '{bag_id}'")
                    with lock:
                        if bag_id in recent_scans and time.time() - recent_scans[bag_id] < 10:
                            print(f"  └─ 🔁 [NFC] Duplicate ignored: '{bag_id}'")
                            continue
                        current_scan_candidates.append({
                            "bag_id": bag_id, "source": "nfc", "raw_data": scan
                        })
                        print(f"  └─ ⏳ [NFC] Queued. Waiting 1.0s for other sensors...")
                        if not robustness_timer_active:
                            robustness_timer_active = True
                            threading.Timer(1.0, process_candidates).start()
        except requests.exceptions.ConnectionError:
            pass  # Pi not up yet — keep retrying silently
        except Exception as e:
            print(f"⚠️  [NFC Poller] Error: {e}")
        time.sleep(0.5)

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
# ENTRY POINT
# ==========================================

def run_server():
    print("=" * 55)
    print("  LeBag Central Hub — Starting")
    print("=" * 55)
    print(f"  Port              : {SERVER_PORT}")
    print(f"  Notification URL  : {NOTIFICATION_SERVER_URL}")
    print(f"  Bag DB            : {BAG_DB_FILE}")
    print(f"  Pi NFC polling    : {PI_NFC_URL or 'DISABLED (set LEBAG_PI_IP)'}")
    print("-" * 55)
    print(f"  API endpoints:")
    print(f"    POST /api/nfc_scan    — NFC scan from Pi (legacy push)")
    print(f"    POST /api/camera_scan — QR scan from camera_reader")
    print(f"    GET  /api/pop_pending — tracker.py polls this")
    print("-" * 55 + "\n")

    # Start background NFC poller
    threading.Thread(target=poll_pi_nfc, daemon=True).start()

    print(f"🚀 LeBag Central Hub running on http://0.0.0.0:{SERVER_PORT}")
    app.run(host='0.0.0.0', port=SERVER_PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    run_server()