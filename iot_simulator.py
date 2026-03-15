import cv2
import base64
import asyncio
import httpx
import json
import time
import os
import math

async def run_simulator():
    # Use localhost for local dev; change to your Vercel URL for remote testing
    base_url = "http://localhost:8000"
    client = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    # Simulated RFID Database
    DECLARED_RFIDS = ["RFID_99230040389", "RFID_9923005130", "RFID_1", "RFID_2", "RFID_3"]

    try:
        print("Connected in HTTP mode!")
        is_faculty_verified = False
        faculty_name = "Unknown"
        faculty_rfid = None
        
        print("\n" + "="*40)
        print("INITIALIZING CAMERA... Press SPACE in the video window to scan Faculty RFID.")
        print("="*40 + "\n")

        # --- STEP 2: Start Camera & Main Loop ---
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("Error: Could not open webcam.")
            return

        is_scanning = False
        feedback_msg = "SCAN READY"
        feedback_color = (16, 185, 129)
        feedback_timer = 0

        async def handle_response(resp_data):
            """Process simulated response from HTTP post."""
            nonlocal feedback_msg, feedback_color, feedback_timer, \
                        is_faculty_verified, faculty_name, faculty_rfid
            
            msg_type = resp_data.get("type", "")
            
            if msg_type == "faculty_status":
                if resp_data.get("verified"):
                    print(f"[SERVER] ✅ Faculty Verified: {resp_data.get('faculty_name')}")
                    is_faculty_verified = True
                    faculty_name = resp_data.get("faculty_name")
                    faculty_rfid = resp_data.get("faculty_rfid")
                    feedback_msg = "FACULTY VERIFIED"
                    feedback_color = (16, 185, 129)
                else:
                    msg = resp_data.get('message', 'INVALID FACULTY CARD')
                    print(f"[SERVER] ❌ Faculty verification failed: {msg}")
                    feedback_msg = msg.upper()[:30]
                    feedback_color = (0, 0, 200)
                    is_faculty_verified = False
                    faculty_name = "Unknown"
                    faculty_rfid = None
                feedback_timer = time.time() + 4.0

            elif msg_type == "detection":
                student_name = resp_data.get("student_name", "Unknown")
                match_status = resp_data.get("status", "")
                print(f"[SERVER] ✅ Detected: {student_name} | Status: {match_status}")
                feedback_msg = f"MATCH: {student_name}"
                feedback_color = (16, 185, 129)
                feedback_timer = time.time() + 2.5
            elif msg_type == "no_match":
                print(f"[SERVER] ❌ No Match Found")
                feedback_msg = "NO MATCH FOUND"
                feedback_color = (0, 0, 200)
                feedback_timer = time.time() + 2.5

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape

            # Header Bar
            cv2.rectangle(frame, (0, 0), (w, 70), (45, 45, 45), -1)
            header_text = f"HTTP BIOMETRIC NODE (VERCEL READY)"
            if is_faculty_verified:
                header_text += f" | ACTIVE: {faculty_name}"
            else:
                header_text += " | LOCKED (Scan Faculty RFID)"
            
            cv2.putText(frame, header_text, (20, 45),
                        cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 255, 255), 2)

            # Scanner Box
            box_size = 280
            tl = (w//2 - box_size//2, h//2 - box_size//2)
            br = (w//2 + box_size//2, h//2 + box_size//2)

            color = (0, 165, 255) if not is_faculty_verified else (99, 102, 241)
            cv2.rectangle(frame, tl, br, color, 1)

            # Scanning Animation
            if is_scanning:
                scan_line_y = int(tl[1] + (box_size * (0.5 + 0.5 * math.sin(time.time() * 10))))
                cv2.line(frame, (tl[0], scan_line_y), (br[0], scan_line_y), (168, 85, 247), 2)

            # Info Panel
            cv2.rectangle(frame, (0, h-80), (w, h), (20, 20, 20), -1)
            current_color = feedback_color if time.time() <= feedback_timer else (180, 180, 180)
            
            if not is_faculty_verified:
                display_msg = "ATTENDANCE LOCKED" if time.time() > feedback_timer else feedback_msg
                controls_text = "[SPACE] SCAN FACULTY RFID"
            else:
                display_msg = "SCAN READY" if time.time() > feedback_timer else feedback_msg
                controls_text = "[S/Space] BOTH | [F] FACE | [R] RFID"

            cv2.putText(frame, f"STATUS: {display_msg}", (30, h-45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, current_color, 2)
            cv2.putText(frame, controls_text, (w - 420, h-45),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            cv2.imshow("HTTP Attendance Simulator", frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break

            payload = {}
            trigger_send = False

            if faculty_rfid:
                payload["faculty_rfid"] = faculty_rfid

            if key in [ord('s'), ord(' ')] and not is_scanning:
                if not is_faculty_verified:
                    print("\n--- INITIATING FACULTY SCAN ---")
                    fac_rfid_in = input("Please sweep Faculty RFID: ").strip()
                    if fac_rfid_in:
                        is_scanning = True
                        payload["type"] = "faculty_verify"
                        payload["rfid_id"] = fac_rfid_in
                        trigger_send = True
                else:
                    print("\n--- INITIATING FULL SCAN ---")
                    student_rfid = input("Please scan student RFID (enter card number): ").strip()
                    if student_rfid:
                        is_scanning = True
                        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        payload["face_image_base64"] = base64.b64encode(buffer).decode('utf-8')
                        payload["rfid_id"] = student_rfid
                        trigger_send = True

            elif key == ord('f') and not is_scanning:
                is_scanning = True
                _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                payload["face_image_base64"] = base64.b64encode(buffer).decode('utf-8')
                trigger_send = True

            elif key == ord('r') and not is_scanning:
                print("\n--- INITIATING RFID SCAN ---")
                student_rfid = input("Please scan student RFID (enter card number): ").strip()
                if student_rfid:
                    is_scanning = True
                    payload["rfid_id"] = student_rfid
                    trigger_send = True

            if trigger_send:
                try:
                    endpoint = "/iot/verify" if payload.get("type") == "faculty_verify" else "/iot/detect"
                    response = await client.post(endpoint, json=payload)
                    if response.status_code == 200:
                        await handle_response(response.json())
                    else:
                        print(f"[SERVER ERROR] {response.status_code}")
                except Exception as e:
                    print(f"Transmission error: {e}")
                is_scanning = False

            await asyncio.sleep(0.01)

    except Exception as e:
        print(f"Server link failure: {e}")
    finally:
        await client.aclose()
        cap.release()
        cv2.destroyAllWindows()
        print("Simulator Disconnected.")

if __name__ == "__main__":
    asyncio.run(run_simulator())
