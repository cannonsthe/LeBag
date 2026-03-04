import cv2
import threading
import time
import requests
import numpy as np
import json
import os

from ultralytics import YOLO

# --------------------------
# Configuration
# --------------------------
SERVER_API_URL = "http://localhost:5001"

# STREAM 2 — Phone camera (on-belt live tracking)
# Use the IP Webcam app (Android) or similar. Format: http://<PHONE_IP>:8080/video
# Reads from LEBAG_ANDROID_URL env var (set by launcher/start_lebag.bat) or falls back to the hardcoded value.
_default_stream = "http://10.47.163.60:8080/video"
ANDROID_STREAM_URL = os.environ.get("LEBAG_ANDROID_URL", _default_stream)

# --------------------------
# State Management
# --------------------------
active_assignments = {} # track_id -> passenger_name
id_zones = {} # track_id -> "A" or "B"
unassigned_ids = set() # IDs currently waiting for a name
last_seen_frames = {} # track_id -> frame_count for cleanup
last_known_positions = {} # track_id -> (x1, y1, x2, y2)

# --------------------------
# Background Video Streamer
# --------------------------
class VideoStream:
    def __init__(self, src):
        # For HTTP/RTSP streams, use the FFMPEG backend with a minimal buffer so we
        # always get the LATEST frame rather than a stale buffered one.
        is_network = isinstance(src, str) and src.startswith('http')
        if is_network:
            self.stream = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        else:
            self.stream = cv2.VideoCapture(src)

        # Minimise internal buffer (1 frame) to reduce latency / stuttering
        self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self.stream.isOpened():
            print(f"⚠️ Could not open stream {src}. Falling back to default camera (0).")
            self.stream = cv2.VideoCapture(0)
            self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not self.stream.isOpened():
                print("Error: Could not open any video source.")
        self.grabbed, self.frame = self.stream.read()
        self.stopped = False
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.update, args=(), daemon=True).start()
        return self

    def update(self):
        while not self.stopped:
            grabbed, frame = self.stream.read()
            with self.lock:
                self.grabbed = grabbed
                if grabbed and frame is not None:
                    self.frame = frame
            # Tiny sleep keeps CPU usage low; at 30fps we have ~33ms per frame
            time.sleep(0.005)

    def read(self):
        with self.lock:
            if self.frame is None:
                return self.grabbed, None
            return self.grabbed, self.frame.copy()

    def stop(self):
        self.stopped = True
        self.stream.release()

def pop_name_from_queue():
    """Tries to pop a single passenger name from the server queue."""
    try:
        response = requests.get(f"{SERVER_API_URL}/api/pop_pending", timeout=2)
        if response.status_code == 200:
            data = response.json()
            return data.get("name") # Will be None if queue is empty
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Warning: Could not reach server to pop queue ({e})")
    return None

def trigger_zone_transition(name, zone):
    """Notifies server.py that a bag moved to a new zone (relayed to notification-idea)."""
    try:
        url = f"{SERVER_API_URL}/api/luggage_zone"
        resp = requests.post(url, json={"owner": name, "zone": zone}, timeout=3)
        print(f"📡 [Event] {name} entered Zone {zone} (relay status {resp.status_code})")
    except Exception as e:
        print(f"⚠️  [Event] Zone transition POST failed for {name}: {e}")

def trigger_bag_collected(name):
    """Notifies server.py that a bag has been collected (relayed to notification-idea)."""
    try:
        url = f"{SERVER_API_URL}/api/luggage_collected"
        resp = requests.post(url, json={"owner": name}, timeout=3)
        print(f"✅ [Event] {name}'s Bag was Collected! (relay status {resp.status_code})")
    except Exception as e:
        print(f"⚠️  [Event] Collected POST failed for {name}: {e}")



def send_to_backend(track_id, label):
    url = "http://localhost:5000/api/luggage"
    payload = {"track_id": track_id, "label": label}
    try:
        print(f"\n[HTTP POST] Sending -> Track ID: {track_id}, Label: '{label}' to {url}")
    except Exception as e:
        print(f"Error sending data to backend: {e}")

def main():
    print("=" * 55)
    print("  LeBag YOLO Tracker — Starting")
    print("=" * 55)
    print(f"  Phone stream : {ANDROID_STREAM_URL}")
    print(f"  Server API   : {SERVER_API_URL}")
    print("-" * 55)

    print(f"\n📱 Connecting to phone stream at {ANDROID_STREAM_URL}...")
    vs = VideoStream(ANDROID_STREAM_URL).start()
    time.sleep(2.0)

    grabbed, test_frame = vs.grabbed, vs.frame
    if not grabbed or test_frame is None:
        print("⚠️  Phone stream not reachable! Check IP Webcam app is running and IP is correct.")
        print("   Falling back to local webcam (index 0)...")
    else:
        print("✅ Phone stream connected!\n")

    import torch

    # --- Model Loading ---
    MODEL_FILE = "baggage.pt"
    if not os.path.exists(MODEL_FILE):
        print(f"⚠️  '{MODEL_FILE}' not found. Falling back to pretrained 'yolov8n.pt'.")
        print(f"   (Place baggage.pt in the project root folder for custom luggage detection)")
        MODEL_FILE = "yolov8n.pt"
    print(f"\n🤖 Loading model: {MODEL_FILE}...")

    _original_load = torch.load
    def _patched_load(*args, **kwargs):
        kwargs['weights_only'] = False
        return _original_load(*args, **kwargs)
    torch.load = _patched_load

    model = YOLO(MODEL_FILE)

    if torch.cuda.is_available():
        model.to('cuda')
        print("✅ Using CUDA GPU for tracking.")
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        model.to('mps')
        print("✅ Using Apple MPS for tracking.")
    else:
        print("ℹ️  Using CPU for tracking (no GPU detected).")

    print("\n▶️  Tracking loop started. Press 'q' to quit.\n")

    
    frame_count = 0
    
    while True:
        grabbed, frame = vs.read()
        if not grabbed or frame is None:
            time.sleep(0.01)
            continue
        frame_count += 1
            
        # Get Frame Width for Logical Zones
        height, width = frame.shape[:2]
        zone_divider = width // 2
        
        # Draw logical zone divider and collection line
        annotated_frame = frame.copy()
        cv2.line(annotated_frame, (zone_divider, 0), (zone_divider, height), (0, 0, 255), 2)
        cv2.putText(annotated_frame, "ZONE A", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        cv2.putText(annotated_frame, "ZONE B", (width - 150, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        
        collection_line_y = int(height * 0.95)
        cv2.line(annotated_frame, (0, collection_line_y), (width, collection_line_y), (0, 255, 0), 2)
        cv2.putText(annotated_frame, "COLLECTION ZONE", (50, collection_line_y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        results = model.track(frame,
                              persist=True,
                              # No 'classes' filter — baggage.pt is trained only on
                              # luggage classes, so all detections are relevant.
                              conf=0.20,
                              iou=0.45,
                              tracker="bytetrack.yaml",
                              verbose=False)
        
        
        if results[0].boxes is not None and results[0].boxes.id is not None:
            boxes = results[0].boxes.xyxy.cpu()
            track_ids = results[0].boxes.id.int().cpu().tolist()
            class_ids = results[0].boxes.cls.int().cpu().tolist()
            confs = results[0].boxes.conf.cpu().tolist()
            
            current_frame_ids = set(track_ids)
            
            for box, track_id, cls_id, conf in zip(boxes, track_ids, class_ids, confs):
                # Update last seen and position
                last_seen_frames[track_id] = frame_count
                x1, y1, x2, y2 = map(int, box)
                last_known_positions[track_id] = (x1, y1, x2, y2)
                
                center_x = (x1 + x2) // 2
                
                # Check Zone Status
                current_zone = "A" if center_x <= zone_divider else "B"
                if track_id not in id_zones:
                     id_zones[track_id] = current_zone
                elif id_zones[track_id] != current_zone:
                     # Zone crossed!
                     id_zones[track_id] = current_zone
                     if track_id in active_assignments:
                         trigger_zone_transition(active_assignments[track_id], current_zone)

                # FIFO Assignment Logic
                if track_id not in active_assignments and track_id not in unassigned_ids:
                    # New bag appeared! Ask server for a name
                    name = pop_name_from_queue()
                    if name:
                        active_assignments[track_id] = name
                        print(f"🧳 Assigned Track ID {track_id} -> {name}")
                    else:
                        unassigned_ids.add(track_id)
                elif track_id in unassigned_ids:
                     # Try to ping server again (maybe they just scanned it)
                     if frame_count % 30 == 0:
                         name = pop_name_from_queue()
                         if name:
                             unassigned_ids.remove(track_id)
                             active_assignments[track_id] = name
                             print(f"🧳 Assigned Track ID {track_id} -> {name} (delayed)")

                label_text = active_assignments.get(track_id, "Unknown Owner")
                cls_name = model.names[cls_id].capitalize()
                
                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (255, 144, 30), 3)
                
                if track_id in active_assignments:
                    display_text = f"[{track_id}] {label_text}"
                    color = (0, 255, 0) # Green for assigned
                else:
                    display_text = f"[{track_id}] {cls_name} {conf:.2f}"
                    color = (0, 0, 255) # Red for unassigned
                    
                (text_w, text_h), _ = cv2.getTextSize(display_text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(annotated_frame, (x1, y1 - text_h - 10), (x1 + text_w, y1), color, -1)
                cv2.putText(annotated_frame, display_text, (x1, y1 - 5), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                            
            # Check for vanished bags to trigger collection
            # 90 frames ≈ 3 seconds — gives time to recover tracking through brief
            # stutters or lighting changes without falsely triggering collection.
            vanished_threshold_frames = 90
            for tid in list(active_assignments.keys()):
                if tid not in current_frame_ids and (frame_count - last_seen_frames.get(tid, 0)) > vanished_threshold_frames:
                    
                    x1, y1, x2, y2 = last_known_positions.get(tid, (0, 0, 0, 0))
                    
                    # Edge Rejection Logic
                    if y1 < 50 or x1 < 50 or x2 > width - 50:
                        print(f"🧹 Cleaned up Track ID {tid} (Lost at Edge)")
                    # Collection Trigger Logic
                    elif y2 > collection_line_y:
                        trigger_bag_collected(active_assignments[tid])
                        print(f"✅ Bag Collected: Track ID {tid}")
                    else:
                        print(f"🧹 Cleaned up Track ID {tid} (Lost in mid-frame)")
                        
                    del active_assignments[tid]
                    if tid in id_zones: del id_zones[tid]
                    if tid in last_seen_frames: del last_seen_frames[tid]
                    if tid in last_known_positions: del last_known_positions[tid]
                    
            for tid in list(unassigned_ids):
                if tid not in current_frame_ids and (frame_count - last_seen_frames.get(tid, 0)) > vanished_threshold_frames:
                    print(f"🧹 Cleaned up unassigned Track ID {tid}")
                    unassigned_ids.remove(tid)
                    if tid in id_zones: del id_zones[tid]
                    if tid in last_seen_frames: del last_seen_frames[tid]
                    if tid in last_known_positions: del last_known_positions[tid]

        cv2.imshow("Luggage Tracking", annotated_frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            print("Quitting...")
            break

    vs.stop()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
