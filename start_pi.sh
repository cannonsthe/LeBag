#!/bin/bash
# ================================================
#   LeBag Pi — Interactive Startup
# ================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"
STREAM_PORT=5000

# --- Load config.env if present ---
if [ -f "$CONFIG_FILE" ]; then
    export $(grep -v '^#' "$CONFIG_FILE" | grep -v '^$' | xargs)
fi

PI_DETECTED_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "================================================"
echo "  LeBag Pi — Startup"
echo "================================================"
echo "  This Pi's IP : $PI_DETECTED_IP"
echo "  NFC server   : http://$PI_DETECTED_IP:5002/api/nfc_scan"
echo "  Camera stream: tcp://$PI_DETECTED_IP:$STREAM_PORT"
echo ""
echo "  (PC just needs to know this Pi's IP — Pi doesn't"
echo "   need to know the PC's IP anymore)"
echo ""

# -----------------------------------------------
# Conveyor belt toggle
# -----------------------------------------------
DEFAULT_CONVEYOR=${CONVEYOR_ENABLED:-1}
if [ "$DEFAULT_CONVEYOR" = "1" ]; then
    read -p "  Run conveyor belt? (Y/n): " BELT_CHOICE
    BELT_CHOICE="${BELT_CHOICE:-y}"
else
    read -p "  Run conveyor belt? (y/N): " BELT_CHOICE
    BELT_CHOICE="${BELT_CHOICE:-n}"
fi

if [[ "$BELT_CHOICE" =~ ^[Yy]$ ]]; then
    export LEBAG_CONVEYOR=1

    # Belt speed
    DEFAULT_SPEED=${CONVEYOR_SPEED:-50}
    read -p "  Conveyor speed (0-100) [default: $DEFAULT_SPEED]: " SPEED_INPUT
    SPEED_INPUT="${SPEED_INPUT:-$DEFAULT_SPEED}"
    if [ "$SPEED_INPUT" -lt 0 ] 2>/dev/null; then SPEED_INPUT=0; fi
    if [ "$SPEED_INPUT" -gt 100 ] 2>/dev/null; then SPEED_INPUT=100; fi
    export LEBAG_SPEED="$SPEED_INPUT"

    # Belt runtime
    read -p "  How long to run belt? (seconds, 0 = forever) [default: 0]: " RUNTIME_INPUT
    RUNTIME_INPUT="${RUNTIME_INPUT:-0}"
    export LEBAG_RUNTIME="$RUNTIME_INPUT"

    echo "  Conveyor: ON | Speed: ${LEBAG_SPEED}% | Runtime: $( [ "$LEBAG_RUNTIME" = "0" ] && echo '∞' || echo "${LEBAG_RUNTIME}s" )"
else
    export LEBAG_CONVEYOR=0
    export LEBAG_SPEED=0
    export LEBAG_RUNTIME=0
    echo "  Conveyor: OFF (NFC reader only)"
fi

echo ""
echo "================================================"
echo "  Starting..."
echo "================================================"
echo ""

# -----------------------------------------------
# Start camera stream in background
# -----------------------------------------------
echo "[1/2] Starting camera stream..."
rpicam-vid -t 0 --inline --listen \
    -o tcp://0.0.0.0:$STREAM_PORT \
    --width 1280 --height 720 \
    --framerate 20 --bitrate 1500000 &
STREAM_PID=$!
echo "  ✅ Camera stream ready (PID: $STREAM_PID)"
sleep 1

# -----------------------------------------------
# Start NFC reader (foreground)
# -----------------------------------------------
echo ""
echo "[2/2] Starting NFC reader..."
echo "      (Ctrl+C stops everything)"
echo ""
python3 "$SCRIPT_DIR/nfc_reader.py"

# -----------------------------------------------
# Cleanup
# -----------------------------------------------
echo ""
echo "Stopping camera stream (PID: $STREAM_PID)..."
kill $STREAM_PID 2>/dev/null
wait $STREAM_PID 2>/dev/null
echo "✅ All Pi services stopped."
