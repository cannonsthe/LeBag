#!/bin/bash

# Function to handle script termination
cleanup() {
    echo "Stopping all processes..."
    pkill -P $$
    exit
}

trap cleanup SIGINT

# Start Notification Server (tele.js)
echo "Starting Notification Server..."
cd notification && node tele.js &
cd ..

# Start Python server
echo "Starting Python Server..."
python3 server.py &

echo "Starting Tracker.py..."
python3 tracker.py &

sleep 2

# Start React app
echo "Starting React Application..."
cd lebag && npm run dev &

wait
