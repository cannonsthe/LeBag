import cv2
import numpy as np
import time
import threading
import requests
from pyzbar.pyzbar import decode, ZBarSymbol

# --- CONFIGURATION ---
PI_STREAM_URL = "tcp://192.168.2.2:5000"
SERVER_API_URL = "http://127.0.0.1:5001/api/camera_scan"
MAX_WIDTH = 800
CONSISTENCY_THRESHOLD = 3 # Frames required to confirm a read

class VideoStreamWidget(object):
    """Background thread to constantly read frames from the VideoCapture, ensuring zero buffer delay."""
    def __init__(self, src=0):
        self.capture = cv2.VideoCapture(src)
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.status, self.frame = self.capture.read()
        self.running = True
        self.thread = threading.Thread(target=self.update, args=())
        self.thread.daemon = True
        self.thread.start()

    def update(self):
        while self.running:
            if self.capture.isOpened():
                (self.status, self.frame) = self.capture.read()
            time.sleep(0.01)

    def read(self):
        return self.status, self.frame

    def release(self):
        self.running = False
        self.thread.join(timeout=1.0)
        self.capture.release()

def process_frame(frame, seen_codes, consistency_counter):
    # 1. Downscale to improve PyZbar performance while retaining enough pixels
    h, w = frame.shape[:2]
    if w > MAX_WIDTH:
        scale = MAX_WIDTH / w
        frame = cv2.resize(frame, (MAX_WIDTH, int(h * scale)))

    # 2. Convert to grayscale (PyZbar only checks luma)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # 3. CLAHE Adaptive Thresholding (brings out QR codes hidden in shadows/low light)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    contrast = clahe.apply(gray)

    # 4. Sharpening Kernel (counteracts motion blur from the conveyor belt)
    kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
    sharp = cv2.filter2D(contrast, -1, kernel)

    # Decode using the optimized, sharp grayscale image
    decoded_objects = decode(sharp, symbols=[ZBarSymbol.QRCODE, ZBarSymbol.EAN13])
    current_frame_codes = set()

    for obj in decoded_objects:
        data = obj.data.decode('utf-8')
        code_type = obj.type
        current_frame_codes.add(data)

        # Draw box on original color frame for display
        x, y, w, h = obj.rect.left, obj.rect.top, obj.rect.width, obj.rect.height
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 2)

        consistency_counter[data] = consistency_counter.get(data, 0) + 1

        if consistency_counter[data] >= CONSISTENCY_THRESHOLD:
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 3)
            cv2.putText(frame, f"{code_type}: {data}", (x, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            if data not in seen_codes:
                # Local 10s cooldown so we don't spam the server
                seen_codes[data] = time.time()
                print(f"✅ [PyZbar] CONFIRMED {code_type}: '{data}'")
                
                # Send to Central Hub
                def send_post(qr_data, qt):
                    try:
                        resp = requests.post(SERVER_API_URL, json={"qr_data": qr_data, "type": qt}, timeout=3)
                        print(f"📤 Server accepted scan (HTTP {resp.status_code})")
                    except Exception as e:
                        print(f"⚠️ Could not reach server at {SERVER_API_URL}: {e}")
                
                threading.Thread(target=send_post, args=(data, code_type), daemon=True).start()

    # Clean up old codes from local debounce
    now = time.time()
    for code in list(seen_codes.keys()):
        if now - seen_codes[code] > 10:
            del seen_codes[code]

    for code in list(consistency_counter.keys()):
        if code not in current_frame_codes:
            consistency_counter[code] = 0

    return frame

def run_scanner():
    print(f"📷 Starting Camera Reader Threading Engine...")
    try:
        stream = VideoStreamWidget(PI_STREAM_URL)
        time.sleep(1.0) # allow buffer to fill
        if not stream.status:
            print("⚠️ PI Stream failed. Falling back to webcam 0...")
            stream.release()
            stream = VideoStreamWidget(0)
    except Exception as e:
        print(f"⚠️ Error initializing stream: {e}. Falling back to webcam 0...")
        stream = VideoStreamWidget(0)

    seen_codes = {}
    consistency_counter = {}

    print("✅ Scanner Active and Optimized! (Press 'q' to quit)")

    while True:
        try:
            status, frame = stream.read()
            if not status or frame is None:
                continue

            # Process the frame and get the annotated version back
            display_frame = process_frame(frame.copy(), seen_codes, consistency_counter)

            cv2.imshow("LeBag Optimized CV Engine", display_frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error in main loop: {e}")

    stream.release()
    cv2.destroyAllWindows()

if __name__ == '__main__':
    run_scanner()
