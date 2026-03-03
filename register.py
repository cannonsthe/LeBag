import cv2
import json
import os
from pyzbar.pyzbar import decode

def clean_input(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""

DB_FILE = "bag_database.json"

def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_database(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def register_bag():
    db = load_database()

    print("=" * 50)
    print("  LeBag — Bag Registration")
    print("=" * 50)
    print("Each NFC sticker and QR code should share the same")
    print("Bag ID (e.g. LB001). Enter it manually OR scan a QR")
    print("code to auto-detect it.\n")

    # Step 1 — Get the Bag ID
    bag_id = clean_input("Bag ID (e.g. LB001), or press Enter to scan from QR: ").upper()

    if not bag_id:
        print("\n📷 Opening camera — point at the QR code on the bag...")
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("❌ Could not open camera.")
            return

        detected = False
        while not detected:
            ret, frame = cap.read()
            if not ret:
                continue
            cv2.imshow("Scan QR Code — Press Q to cancel", frame)
            if cv2.waitKey(1) in [ord('q'), ord('Q')]:
                cap.release()
                cv2.destroyAllWindows()
                return
            for obj in decode(frame):
                bag_id = obj.data.decode('utf-8').strip().upper()
                print(f"✅ QR detected: {bag_id}")
                detected = True
                break

        cap.release()
        cv2.destroyAllWindows()

    if not bag_id:
        print("❌ No Bag ID provided. Exiting.")
        return

    # Step 2 — Check if already exists
    if bag_id in db:
        print(f"\n⚠️  '{bag_id}' is already registered to: {db[bag_id].get('owner', '?')}")
        if clean_input("Overwrite? (y/n): ").lower() != 'y':
            print("Cancelled.")
            return

    # Step 3 — Enter passenger details
    print(f"\n--- Registering Bag ID: {bag_id} ---")
    owner    = clean_input("Passenger Name: ")
    bag_type = clean_input("Bag Description: ")
    flight   = clean_input("Flight Number: ")
    chat_id  = clean_input("Telegram Chat ID (leave blank to use name lookup): ")

    if owner and bag_type and flight:
        db[bag_id] = {
            "owner":   owner,
            "type":    bag_type,
            "flight":  flight,
            "chat_id": chat_id
        }
        save_database(db)
        print(f"\n🎉 Registered '{bag_id}' → {owner} | {bag_type} | {flight}")
        if chat_id:
            print(f"📲 Telegram Chat ID saved: {chat_id}")
        else:
            print(f"ℹ️  No Chat ID — will use name lookup (Marcus/Leonard/Ashok/Balaji/YiBin)")
        print(f"\n💡 Next step: Write '{bag_id}' to the NFC tag on the Raspberry Pi.")
        print(f"   (Use the PN532 write tool on your Pi, or the nfc_writer tool of your choice)")
    else:
        print("❌ Registration cancelled (missing fields).")

if __name__ == "__main__":
    register_bag()
