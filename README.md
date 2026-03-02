# LeBag: Dual-Sensor Pipeline Luggage Tracking System

**LeBag** is an advanced dual-sensor computer vision pipeline designed to automate luggage registration and tracking using QR/Barcode scanning and Computer Vision-based Re-Identification (Re-ID).

By seamlessly synchronizing a QR Scanner registry with an AI baggage tracker, the system completely removes manual baggage entry. When a baggage tag is scanned, the program associates the owner's data with a proprietary visual fingerprint of the bag. Further down the belt, the tracking layer instantly recognizes the bag using this mapped signature without needing to scan the tag again!

## 🏗 Architecture & Core Components

This pipeline revolves around a persistent **concurrent-safe JSON Registry (`bag_database.json`)**, uniting several independent models:

### 1. The Registry (`server.py`)
- **Job:** Act as the entry gateway for registering baggage.
- **Workflow:** Look for QR Codes and Barcodes using `pyzbar`. Upon detecting a valid tag (verified by a 3-strike temporal consistency check), the scanner triggers a YOLOv8 pass to isolate the geographic bounding box of the bag in the frame.
- **Extraction:** The cropped bag image is passed through a lightweight `MobileNetV2` Re-ID feature extractor to record a 1280-dimensional visual embedding vector.
- **Save State:** Information from the QR codebase + visual embedding is serialized into `bag_database.json` dynamically. It also hosts a Flask API backend for frontend updates.

### 2. The Re-ID Follower (`tracker.py`)
- **Job:** Constantly monitor downstream areas (like a conveyor belt) and reliably identify registered bags.
- **Workflow:** Actively tracks bags using `YOLOv8` combined with `ByteTrack`. 
- **Auto-Identification:** Captures visual feature embeddings for any new baggage that enters the frame. Re-ID Follower continually compares this embedding iteratively against the known bag gallery (retrieved dynamically from the JSON Registry).
- **Match Strategy:** Using Cosine Similarity with an `85%` threshold check, a match instantly maps the bag to its owner's metadata. Unrecognized bags are tagged `"Unregistered Bag"`.
- **Adaptive Learning:** The longer a bag stays tracked, the model exponentially merges variations in its features (caused by lighting and rotational shifts) using a `0.95` momentum alpha.

### 3. The Dashboard Layer (`lebag` React App)
- Contains a real-time web application to intuitively view tracking logs using React & Vite.

## ⚙️ Prerequisites
- **Python 3.9+**
- **Node.js** (for running the React Dashboard)
- A webcam or IP stream (configured to `tcp://192.168.2.2:5000` via Raspberry Pi, or fallback to default camera `/dev/video0`).

Install Python dependencies via:
```bash
pip install -r requirements.txt
```

## 🚀 Setup & Usage

### 1. Standard Quick Start
The easiest way to boot up the dual-sensor system and React app together is to execute the start shell script:

```bash
chmod +x start_dev.sh
./start_dev.sh
```
This will:
- Fire up the React frontend (accessible typically at `http://localhost:5173`)
- Boot `server.py` in the background (managing Flask and Scanner window)
- Boot `tracker.py` in the foreground window

### 2. Manual Execution

If you wish to debug individually in separate terminals:

**Terminal 1 (Backend & Scanner Registry):**
```bash
python3 server.py
# Scans tags and populates bag_database.json
```

**Terminal 2 (Vision Tracking Component):**
```bash
python3 tracker.py
# Identifies bags natively by reading bag_database.json
```

**Terminal 3 (Web Component):**
```bash
cd lebag
npm install 
npm run dev
```

## 🧠 Technical Under-The-Hood

### File Locking & Concurrency
A critical problem with dual detached scripts accessing a single JSON database is file collision/corruption. This pipeline utilizes the standard library's `fcntl` file locks (`LOCK_EX` for writing strings + `LOCK_SH` for safe streaming) to completely eliminate read/write deadlocks. 

When `server.py` actively pushes a new visual embedding list (which is massive), `tracker.py` halts its polling loop fractions of a millisecond utilizing block locks rather than throwing JSONDecodeErrors. 

### Why MobileNetV2?
`MobileNetV2` acts as our high-dimensional Re-ID feature extractor model because it provides exceptional discrimination maps while operating extremely fast even on low-end edge CPU setups, easily targeting continuous throughput under 200ms latency.
