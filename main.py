import os
# Suppress TensorFlow logs BEFORE importing biometrics/tensorflow
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

import json
import base64
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
import uvicorn

from models import init_db, get_db_connection
from auth import get_password_hash, verify_password, create_access_token, decode_access_token
import os
from fastapi.staticfiles import StaticFiles
from biometrics import get_face_embedding, compare_faces

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB on startup
    print("Starting application lifespan...")
    try:
        init_db()
        print("Database initialized.")
        conn = get_db_connection()
        cursor = conn.cursor()
        # Create default faculty
        cursor.execute("SELECT id FROM faculty WHERE email = ?", ("faculty@school.com",))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO faculty (name, email, rfid_id, password_hash) VALUES (?, ?, ?, ?)",
                           ("Admin Faculty", "faculty@school.com", "RFID_FAC_123", get_password_hash("password123")))
            conn.commit()
        conn.close()
        print("Application startup complete.")
    except Exception as e:
        print(f"CRITICAL STARTUP ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise e
    yield

app = FastAPI(title="Student Attendance System", lifespan=lifespan)

# CORS - Must be added to the final app instance
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Track active faculty connections for live attendance feed
# Track active faculty notifications for live attendance feed (stateless)
# Since we are moving to Vercel/stateless, we'll store recent detections in the DB 
# and the frontend will poll for them.


# --- Auth Dependency ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    email = payload.get("sub")
    role = payload.get("role")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if role == "faculty":
        cursor.execute("SELECT * FROM faculty WHERE email = ?", (email,))
    else:
        cursor.execute("SELECT * FROM students WHERE email = ?", (email,))
    
    user = cursor.fetchone()
    conn.close()
        
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"user": dict(user), "role": role}

# --- Endpoints ---

@app.get("/user/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    user = current_user["user"]
    user.pop("password_hash", None)
    return {"user": user, "role": current_user["role"]}

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check Faculty
    cursor.execute("SELECT * FROM faculty WHERE email = ?", (username,))
    user = cursor.fetchone()
    role = "faculty"
    
    if not user:
        # Check Student
        cursor.execute("SELECT * FROM students WHERE email = ? OR reg_no = ?", (username, username))
        user = cursor.fetchone()
        role = "student"
    
    conn.close()
        
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token(data={"sub": user["email"], "role": role})
    return {"access_token": access_token, "token_type": "bearer", "role": role}

@app.post("/signup_faculty")
async def signup_faculty(
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    rfid_id: str = Form(...)
):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if faculty already exists
    cursor.execute("SELECT id FROM faculty WHERE email = ? OR rfid_id = ?", (email, rfid_id))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Email or RFID already registered")
    
    try:
        cursor.execute('''
            INSERT INTO faculty (name, email, rfid_id, password_hash)
            VALUES (?, ?, ?, ?)
        ''', (name, email, rfid_id, get_password_hash(password)))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Signup failed: {str(e)}")
    
    conn.close()
    return {"message": f"Faculty {name if name else email} registered successfully. You can now login."}

@app.post("/register_student")
async def register_student(
    name: str = Form(...),
    reg_no: str = Form(...),
    email: str = Form(...),
    section_id: int = Form(...),
    department_id: int = Form(...),
    year: str = Form(...),
    rfid_id: str = Form("N/A"),
    photo: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Only faculty can register students")
    
    # Verify section and department and ownership
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.name as sname, d.name as dname 
        FROM sections s 
        JOIN departments d ON s.department_id = d.id 
        WHERE s.id = ? AND d.id = ? AND s.faculty_id = ?
    ''', (section_id, department_id, faculty_id))
    meta = cursor.fetchone()
    if not meta:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid Section or Department")

    photo_bytes = await photo.read()
    embedding, cropped_face_bytes = get_face_embedding(photo_bytes)
    if not embedding:
        conn.close()
        raise HTTPException(status_code=400, detail="Could not detect face in photo")
    
    try:
        # Check if student already exists
        cursor.execute("SELECT id FROM students WHERE reg_no = ?", (reg_no,))
        existing_student = cursor.fetchone()
        
        if existing_student:
            student_id = existing_student["id"]
        else:
            # Create new student with face embedding only
            cursor.execute('''
                INSERT INTO students (name, reg_no, email, section, department, year, rfid_id, face_embedding, password_hash, photo_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (name, reg_no, email, meta["sname"], meta["dname"], year, rfid_id, json.dumps(embedding), get_password_hash(reg_no), None))
            student_id = cursor.lastrowid
        
        # Enroll in section
        cursor.execute("INSERT OR IGNORE INTO enrollments (student_id, section_id) VALUES (?, ?)", (student_id, section_id))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")
    
    conn.close()
    return {"message": f"Student {name} registered and enrolled successfully"}

@app.post("/students/{student_id}/update")
async def update_student(
    student_id: int,
    name: str = Form(...),
    reg_no: str = Form(...),
    email: str = Form(...),
    rfid_id: str = Form(...),
    photo: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Check if student exists
    cursor.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cursor.fetchone()
    if not student:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")

    try:
        update_fields = ["name = ?", "reg_no = ?", "email = ?", "rfid_id = ?"]
        params = [str(name), str(reg_no), str(email), str(rfid_id)]

        if photo:
            photo_bytes = await photo.read()
            embedding, cropped_face_bytes = get_face_embedding(photo_bytes)
            if embedding:
                # Update embedding
                update_fields.extend(["face_embedding = ?", "photo_path = ?"])
                params.extend([json.dumps(embedding), None])

        params.append(student_id)
        cursor.execute(f"UPDATE students SET {', '.join(update_fields)} WHERE id = ?", tuple(params))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Update failed: {str(e)}")
    
    conn.close()
    return {"message": "Student updated successfully"}

@app.post("/departments")
async def create_department(name: str = Form(...), current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO departments (name) VALUES (?)", (name,))
        conn.commit()
    except:
        conn.close()
        raise HTTPException(status_code=400, detail="Department already exists")
    conn.close()
    return {"message": "Department created"}

@app.get("/departments")
async def get_departments():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM departments")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/sections")
async def create_section(
    name: str = Form(...), 
    year: str = Form(...), 
    section: str = Form(...),
    department_id: int = Form(...), 
    academic_duration: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO sections (name, year, section, department_id, faculty_id, academic_duration) VALUES (?, ?, ?, ?, ?, ?)", (name, year, section, department_id, faculty_id, academic_duration))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Error creating class: {str(e)}")
    conn.close()
    return {"message": "Class created successfully"}

@app.get("/all_sections")
async def get_all_sections(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.*, d.name as dept_name 
        FROM sections s 
        JOIN departments d ON s.department_id = d.id
        WHERE s.faculty_id = ?
    ''', (faculty_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.get("/sections/{dept_id}")
async def get_sections(dept_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
        
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sections WHERE department_id = ? AND faculty_id = ?", (dept_id, faculty_id))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.get("/attendance")
async def get_attendance(section_id: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if current_user["role"] == "faculty":
        faculty_id = current_user["user"]["id"]
        query = '''
            SELECT a.*, s.name as student_name, s.reg_no
            FROM attendance a 
            JOIN students s ON a.student_id = s.id 
            JOIN sections sec ON a.section_id = sec.id
            WHERE a.submitted = 1 AND sec.faculty_id = ?
        '''
        params = [faculty_id]
        if section_id:
            query += " AND sec.id = ?"
            params.append(section_id)
        query += " ORDER BY a.timestamp DESC"
        cursor.execute(query, tuple(params))
        records = [dict(row) for row in cursor.fetchall()]
        
        # Calculate Stats for the Bar Graph
        stats = {"gt90": 0, "gt80": 0, "lt80": 0, "lt75": 0}
        if section_id:
            # Get total sessions for this section
            cursor.execute("SELECT COUNT(DISTINCT date(timestamp)) FROM attendance WHERE section_id = ? AND submitted = 1", (section_id,))
            total_sessions = cursor.fetchone()[0] or 1
            
            # Get percentages for all students in this section
            cursor.execute('''
                SELECT st.id, COUNT(DISTINCT date(a.timestamp)) as attended
                FROM students st
                JOIN enrollments e ON st.id = e.student_id
                LEFT JOIN attendance a ON st.id = a.student_id AND e.section_id = a.section_id AND a.submitted = 1
                WHERE e.section_id = ?
                GROUP BY st.id
            ''', (section_id,))
            
            for row in cursor.fetchall():
                perc = (row["attended"] / total_sessions) * 100
                if perc >= 90: stats["gt90"] += 1
                if perc >= 80: stats["gt80"] += 1
                if perc < 80: stats["lt80"] += 1
                if perc < 75: stats["lt75"] += 1
                
        conn.close()
        return {"records": records, "stats": stats}
    else:
        student_id = current_user["user"]["id"]
        # Total sessions available to this student
        cursor.execute('''
            SELECT COUNT(DISTINCT date(a.timestamp)) 
            FROM attendance a
            JOIN enrollments e ON a.section_id = e.section_id
            WHERE e.student_id = ? AND a.submitted = 1
        ''', (student_id,))
        total_days = cursor.fetchone()[0] or 1

        cursor.execute("SELECT * FROM attendance WHERE student_id = ? AND submitted = 1 ORDER BY timestamp DESC", (student_id,))
        records = [dict(row) for row in cursor.fetchall()]
        
        present_count = len(set(row["timestamp"][:10] for row in records)) # Count unique dates
        percentage = (present_count / total_days) * 100
        
        # Per Subject Analytics with History
        cursor.execute('''
            SELECT s.id as section_id, s.name as subject_name, s.section as section_code
            FROM enrollments e
            JOIN sections s ON e.section_id = s.id
            WHERE e.student_id = ?
        ''', (student_id,))
        enrolled_sections = cursor.fetchall()
        
        subject_stats = []
        for sec in enrolled_sections:
            sid = sec["section_id"]
            # Get all submitted session dates for this section
            cursor.execute("SELECT DISTINCT date(timestamp) as session_date FROM attendance WHERE section_id = ? AND submitted = 1 ORDER BY timestamp ASC", (sid,))
            all_sessions = [r["session_date"] for r in cursor.fetchall()]
            
            # Get student's present dates
            cursor.execute("SELECT DISTINCT date(timestamp) as present_date FROM attendance WHERE student_id = ? AND section_id = ? AND submitted = 1", (student_id, sid))
            present_dates = set(r["present_date"] for r in cursor.fetchall())
            
            history = []
            for s_date in all_sessions:
                history.append({
                    "date": s_date,
                    "status": "P" if s_date in present_dates else "A"
                })
            
            subject_stats.append({
                "subject_name": f"{sec['subject_name']} ({sec['section_code']})",
                "total_sessions": len(all_sessions),
                "present_sessions": len(present_dates),
                "history": history
            })
            
        conn.close()
        return {"records": records, "percentage": percentage, "subject_stats": subject_stats}

@app.get("/attendance/current_live_section")
async def get_current_live_section(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    now = datetime.now()
    current_day = now.strftime("%A")
    current_time = now.strftime("%H:%M")
    
    cursor.execute('''
        SELECT t.section_id, s.name as subject_name, s.section as section_code
        FROM timetable t
        JOIN sections s ON t.section_id = s.id
        WHERE t.faculty_id = ? AND t.day_of_week = ? AND ? BETWEEN t.start_time AND t.end_time
    ''', (faculty_id, current_day, current_time))
    active = cursor.fetchone()
    conn.close()
    
    if not active:
        return {"section_id": None}
    return dict(active)

@app.get("/attendance/pending/{section_id}")
async def get_pending_attendance(section_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify section belongs to faculty
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    # Get active session bounds for this section to prevent bleed from earlier same-day sessions
    now = datetime.now()
    current_day = now.strftime("%A")
    current_time = now.strftime("%H:%M")
    
    cursor.execute('''
        SELECT start_time, end_time FROM timetable
        WHERE faculty_id = ? AND section_id = ? AND day_of_week = ? 
        AND ? BETWEEN start_time AND end_time
    ''', (faculty_id, section_id, current_day, current_time))
    active_slot = cursor.fetchone()

    cursor.execute('''
        SELECT a.*, s.name as student_name, s.reg_no 
        FROM attendance a 
        JOIN students s ON a.student_id = s.id 
        WHERE a.section_id = ? AND a.submitted = 0
        ORDER BY a.timestamp DESC
    ''', (section_id,))
    records = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Filter out records that are outside the current live timetable window.
    if active_slot:
        try:
            sh, sm = map(int, active_slot["start_time"].split(':'))
            eh, em = map(int, active_slot["end_time"].split(':'))
            filtered_records = []
            for r in records:
                # Timestamps are normally stored as naive ISO strings from datetime.now()
                # Use string splicing or fromisoformat to be safe
                if isinstance(r["timestamp"], str):
                    dt = datetime.fromisoformat(r["timestamp"])
                else:
                    dt = r["timestamp"]
                
                if dt.date() == now.date():
                    if (sh, sm) <= (dt.hour, dt.minute) <= (eh, em):
                        filtered_records.append(r)
            return filtered_records
        except Exception as e:
            print("Error filtering by duration:", e)
            return records

    return records

@app.post("/attendance/submit/{section_id}")
async def submit_attendance(section_id: int, statuses: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify section belongs to faculty
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    # Get subject name and current timetable session time
    now = datetime.now()
    current_day = now.strftime("%A")
    current_time_str = now.strftime("%H:%M")
    cursor.execute('''
        SELECT s.name as subject_name, t.start_time, t.end_time
        FROM sections s
        LEFT JOIN timetable t ON t.section_id = s.id AND t.faculty_id = ? AND t.day_of_week = ?
            AND ? BETWEEN t.start_time AND t.end_time
        WHERE s.id = ?
    ''', (faculty_id, current_day, current_time_str, section_id))
    meta = cursor.fetchone()
    subject_name = meta["subject_name"] if meta else None
    if meta and meta["start_time"] and meta["end_time"]:
        # Format as 12h AM/PM
        def fmt_time(t):
            try:
                h, m = int(t.split(":")[0]), int(t.split(":")[1])
                suffix = "AM" if h < 12 else "PM"
                h12 = h if h <= 12 else h - 12
                h12 = 12 if h12 == 0 else h12
                return f"{h12}:{m:02d} {suffix}"
            except:
                return t
        session_time = f"{fmt_time(meta['start_time'])} - {fmt_time(meta['end_time'])}"
    else:
        session_time = now.strftime("%I:%M %p")

    if statuses:
        status_list = [s.strip() for s in statuses.split(",")]
        # Mark only requested ones as submitted, stamping subject_name and session_time
        placeholders = ",".join(["?" for _ in status_list])
        cursor.execute(
            f"UPDATE attendance SET submitted = 1, subject_name = ?, session_time = ? WHERE section_id = ? AND submitted = 0 AND status IN ({placeholders})",
            (subject_name, session_time, section_id, *status_list)
        )
        # Clear the ones that were NOT submitted to start fresh session
        cursor.execute(f"DELETE FROM attendance WHERE section_id = ? AND submitted = 0 AND status NOT IN ({placeholders})", (section_id, *status_list))
    else:
        cursor.execute(
            "UPDATE attendance SET submitted = 1, subject_name = ?, session_time = ? WHERE section_id = ? AND submitted = 0",
            (subject_name, session_time, section_id)
        )
    
    conn.commit()
    conn.close()
    return {"message": "Attendance submitted successfully"}

@app.post("/attendance/manual")
async def mark_manual_attendance(
    student_id: int = Form(...), 
    section_id: int = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Check if student exists and faculty owns the section
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    cursor.execute("SELECT name FROM students WHERE id = ?", (student_id,))
    student = cursor.fetchone()
    if not student:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
        
    try:
        cursor.execute('''
            INSERT INTO attendance (student_id, section_id, timestamp, face_verified, rfid_verified, status, submitted, is_manual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (student_id, section_id, datetime.now(), True, True, "Verified (Manual)", 1, 1))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Failed to mark attendance: {str(e)}")
    
    conn.close()
    return {"message": f"Manual attendance marked for {student['name']}"}

@app.get("/students")
async def get_students(section_id: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Only faculty can view student list")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    query = '''
        SELECT DISTINCT st.id, st.name, st.reg_no, st.email, st.section, st.department, st.year, st.rfid_id, st.photo_path, st.face_embedding,
               (SELECT COUNT(DISTINCT date(timestamp)) FROM attendance WHERE section_id = s.id AND submitted = 1) as total_days,
               (SELECT COUNT(DISTINCT date(timestamp)) FROM attendance WHERE student_id = st.id AND section_id = s.id AND submitted = 1) as attended_days
        FROM students st
        JOIN enrollments e ON st.id = e.student_id
        JOIN sections s ON e.section_id = s.id
        WHERE s.faculty_id = ?
    '''
    params = [faculty_id]
    if section_id:
        query += " AND s.id = ?"
        params.append(section_id)
    query += " ORDER BY st.name ASC"
    cursor.execute(query, tuple(params))
    
    students = []
    for row in cursor.fetchall():
        d = dict(row)
        total = d.get("total_days") or 0
        attended = d.get("attended_days") or 0
        d["attendance_percentage"] = round(float(attended) / float(total) * 100, 1) if total > 0 else 0
        students.append(d)
        
    conn.close()
    return students

@app.get("/students/{student_id}/attendance_log")
async def get_student_attendance_log(student_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")

    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all sections this student is enrolled in (owned by this faculty)
    cursor.execute('''
        SELECT s.id as section_id, s.name as subject_name, s.section as section_code
        FROM enrollments e
        JOIN sections s ON e.section_id = s.id
        WHERE e.student_id = ? AND s.faculty_id = ?
    ''', (student_id, faculty_id))
    enrolled = cursor.fetchall()

    result = []
    for sec in enrolled:
        sid = sec["section_id"]

        # All sessions submitted for this section (unique session_time + date combos)
        cursor.execute('''
            SELECT DISTINCT date(timestamp) as session_date, session_time, subject_name
            FROM attendance
            WHERE section_id = ? AND submitted = 1
            ORDER BY timestamp ASC
        ''', (sid,))
        all_sessions = cursor.fetchall()

        # Dates student was present
        cursor.execute('''
            SELECT DISTINCT date(timestamp) as present_date
            FROM attendance
            WHERE student_id = ? AND section_id = ? AND submitted = 1
        ''', (student_id, sid))
        present_dates = set(r["present_date"] for r in cursor.fetchall())

        sessions = []
        for sess in all_sessions:
            sessions.append({
                "date": sess["session_date"],
                "session_time": sess["session_time"] or "—",
                "subject_name": sess["subject_name"] or sec["subject_name"],
                "status": "Present" if sess["session_date"] in present_dates else "Absent"
            })

        result.append({
            "section_id": sid,
            "subject_name": sec["subject_name"],
            "section_code": sec["section_code"],
            "sessions": sessions,
            "total": len(sessions),
            "present": len(present_dates)
        })

    conn.close()
    return result

@app.delete("/students/{student_id}")
async def delete_student(student_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM attendance WHERE student_id = ?", (student_id,))
    cursor.execute("DELETE FROM enrollments WHERE student_id = ?", (student_id,))
    cursor.execute("DELETE FROM students WHERE id = ?", (student_id,))
    conn.commit()
    conn.close()
    return {"message": "Student removed successfully"}

@app.delete("/sections/{section_id}")
async def delete_section(section_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify section belongs to faculty
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    cursor.execute("DELETE FROM attendance WHERE section_id = ?", (section_id,))
    cursor.execute("DELETE FROM enrollments WHERE section_id = ?", (section_id,))
    cursor.execute("DELETE FROM sections WHERE id = ?", (section_id,))
    conn.commit()
    conn.close()
    return {"message": "Section removed successfully"}

@app.delete("/faculty/forget")
async def forget_faculty_account(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM faculty WHERE id = ?", (faculty_id,))
    conn.commit()
    conn.close()
    return {"message": "Account forgotten successfully"}

@app.post("/attendance/manual_toggle")
async def toggle_manual_attendance(
    student_id: int = Form(...), 
    section_id: int = Form(...),
    status: str = Form(...), # accept as string for robustness
    current_user: dict = Depends(get_current_user)
):
    # Convert 'true'/'false' string to bool
    is_checked = status.lower() == 'true'
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify section ownership
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    if is_checked:
        # Check if already present for "today" (simple check: same day)
        cursor.execute('''
            SELECT id FROM attendance 
            WHERE student_id = ? AND section_id = ? AND submitted = 0
        ''', (student_id, section_id))
        record = cursor.fetchone()
        if not record:
            cursor.execute('''
                INSERT INTO attendance (student_id, section_id, timestamp, face_verified, rfid_verified, status, submitted, is_manual)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (student_id, section_id, datetime.now(), True, True, "Verified (Manual)", 0, 1))
        else:
            cursor.execute('''
                UPDATE attendance SET status = 'Verified (Manual)', is_manual = 1 WHERE id = ?
            ''', (record['id'],))
    else:
        # Remove unsubmitted manual attendance for this session
        cursor.execute('''
            DELETE FROM attendance 
            WHERE student_id = ? AND section_id = ? AND submitted = 0
        ''', (student_id, section_id))
        
    conn.commit()
    conn.close()
    return {"message": "Attendance updated"}

# --- Timetable Endpoints ---

@app.get("/timetable")
async def get_timetable(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, t.day_of_week, t.start_time, t.end_time, s.name as subject_name, s.section as section_code
        FROM timetable t
        JOIN sections s ON t.section_id = s.id
        WHERE t.faculty_id = ?
        ORDER BY
            CASE t.day_of_week
                WHEN 'Monday' THEN 1
                WHEN 'Tuesday' THEN 2
                WHEN 'Wednesday' THEN 3
                WHEN 'Thursday' THEN 4
                WHEN 'Friday' THEN 5
                WHEN 'Saturday' THEN 6
                WHEN 'Sunday' THEN 7
            END,
            t.start_time
    ''', (faculty_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/timetable")
async def create_timetable_entry(
    section_id: int = Form(...),
    day_of_week: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify section belongs to faculty
    cursor.execute("SELECT id FROM sections WHERE id = ? AND faculty_id = ?", (section_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Section not found or access denied")

    try:
        cursor.execute('''
            INSERT INTO timetable (faculty_id, section_id, day_of_week, start_time, end_time)
            VALUES (?, ?, ?, ?, ?)
        ''', (faculty_id, section_id, day_of_week, start_time, end_time))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Failed to add timetable entry: {str(e)}")
    
    conn.close()
    return {"message": "Timetable entry created"}

@app.put("/timetable/{entry_id}")
async def update_timetable_entry(
    entry_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify ownership
    cursor.execute("SELECT id FROM timetable WHERE id = ? AND faculty_id = ?", (entry_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Entry not found")

    try:
        cursor.execute("UPDATE timetable SET start_time = ?, end_time = ? WHERE id = ?", (start_time, end_time, entry_id))
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Failed to update entry: {str(e)}")
    
    conn.close()
    return {"message": "Timetable entry updated"}

@app.delete("/timetable/{entry_id}")
async def delete_timetable_entry(entry_id: int, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "faculty":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verify ownership
    cursor.execute("SELECT id FROM timetable WHERE id = ? AND faculty_id = ?", (entry_id, faculty_id))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Entry not found")

    cursor.execute("DELETE FROM timetable WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return {"message": "Timetable entry deleted"}

# --- IoT WebSocket ---

# --- IoT HTTP Endpoints (Replacement for WebSockets) ---

@app.post("/iot/verify")
async def iot_faculty_verify(payload: dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        rfid_id = payload.get("rfid_id")
        cursor.execute("SELECT * FROM faculty WHERE rfid_id = ?", (rfid_id,))
        faculty = cursor.fetchone()
        
        if faculty:
            now = datetime.now()
            current_day = now.strftime("%A")
            current_time = now.strftime("%H:%M")
            
            cursor.execute('''
                SELECT t.section_id FROM timetable t
                WHERE t.faculty_id = ? AND t.day_of_week = ? AND ? BETWEEN t.start_time AND t.end_time
            ''', (faculty["id"], current_day, current_time))
            active_schedule = cursor.fetchone()

            if active_schedule:
                return {
                    "type": "faculty_status",
                    "verified": True,
                    "faculty_name": faculty["name"],
                    "faculty_rfid": faculty["rfid_id"]
                }
            else:
                return {
                    "type": "faculty_status",
                    "verified": False,
                    "message": f"No class scheduled for {faculty['name']} now."
                }
        else:
            return {
                "type": "faculty_status",
                "verified": False,
                "message": "Invalid Faculty RFID"
            }
    finally:
        conn.close()

@app.post("/iot/detect")
async def iot_student_detect(payload: dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        face_b64 = payload.get("face_image_base64")
        rfid_id = payload.get("rfid_id")
        faculty_rfid = payload.get("faculty_rfid")
        
        now = datetime.now()
        current_day = now.strftime("%A")
        current_time = now.strftime("%H:%M")
        
        cursor.execute('''
            SELECT t.section_id, t.faculty_id FROM timetable t
            JOIN faculty f ON t.faculty_id = f.id
            WHERE f.rfid_id = ? AND t.day_of_week = ? AND ? BETWEEN t.start_time AND t.end_time
        ''', (faculty_rfid, current_day, current_time))
        active_schedule = cursor.fetchone()
        
        if not active_schedule:
            return {
                "type": "no_match",
                "student_name": "No Class",
                "reg_no": "N/A",
                "status": "No scheduled class",
                "timestamp": datetime.now().isoformat()
            }
            
        section_id = active_schedule["section_id"]
        faculty_id = active_schedule["faculty_id"]
        
        student = None
        face_matches = False
        rfid_matches = False
        
        if face_b64:
            try:
                image_bytes = base64.b64decode(face_b64)
                current_embedding, _ = get_face_embedding(image_bytes)
                if current_embedding:
                    cursor.execute('''
                        SELECT s.* FROM students s
                        JOIN enrollments e ON s.id = e.student_id
                        WHERE e.section_id = ?
                    ''', (section_id,))
                    candidates = cursor.fetchall()
                    for candidate in candidates:
                        if candidate["face_embedding"]:
                            stored_emb = json.loads(candidate["face_embedding"])
                            if compare_faces(stored_emb, current_embedding):
                                student = candidate
                                face_matches = True
                                break
                    if not student:
                        cursor.execute("SELECT * FROM students WHERE face_embedding IS NOT NULL")
                        all_students = cursor.fetchall()
                        for candidate in all_students:
                            stored_emb = json.loads(candidate["face_embedding"])
                            if compare_faces(stored_emb, current_embedding):
                                student = candidate
                                face_matches = True
                                break
            except Exception as e:
                print(f"Face identification error: {e}")

        if not student and rfid_id:
            cursor.execute('''
                SELECT s.* FROM students s
                JOIN enrollments e ON s.id = e.student_id
                WHERE e.section_id = ? AND s.rfid_id = ?
            ''', (section_id, rfid_id))
            student = cursor.fetchone()
            if student:
                rfid_matches = True
        elif student and rfid_id:
            rfid_matches = (student["rfid_id"] == rfid_id)

        if student:
            if face_matches and rfid_matches:
                status_str = "Fully Verified"
            elif face_matches:
                status_str = "Only Face Verified"
            elif rfid_matches:
                status_str = "Only RFID Verified"
            else:
                status_str = "Absent"
            
            cursor.execute('''
                SELECT id, face_verified, rfid_verified FROM attendance
                WHERE student_id = ? AND section_id = ? AND submitted = 0
                ORDER BY id ASC
            ''', (student["id"], section_id))
            existing_rows = cursor.fetchall()

            if existing_rows:
                merged_face = face_matches
                merged_rfid = rfid_matches
                for row in existing_rows:
                    merged_face = merged_face or bool(row["face_verified"])
                    merged_rfid = merged_rfid or bool(row["rfid_verified"])

                if merged_face and merged_rfid:
                    merged_status = "Fully Verified"
                elif merged_face:
                    merged_status = "Only Face Verified"
                elif merged_rfid:
                    merged_status = "Only RFID Verified"
                else:
                    merged_status = "Absent"

                keep_id = existing_rows[0]["id"]
                if len(existing_rows) > 1:
                    extra_ids = [r["id"] for r in existing_rows[1:]]
                    cursor.execute(f"DELETE FROM attendance WHERE id IN ({','.join('?' for _ in extra_ids)})", extra_ids)

                cursor.execute('''
                    UPDATE attendance
                    SET face_verified = ?, rfid_verified = ?, status = ?, timestamp = ?
                    WHERE id = ?
                ''', (merged_face, merged_rfid, merged_status, datetime.now(), keep_id))
                status_str = merged_status
            else:
                cursor.execute('''
                    INSERT INTO attendance (student_id, section_id, timestamp, face_verified, rfid_verified, status, submitted)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                ''', (student["id"], section_id, datetime.now(), face_matches, rfid_matches, status_str))
            conn.commit()

            detection_event = {
                "type": "detection",
                "student_name": student["name"],
                "reg_no": student["reg_no"],
                "status": status_str,
                "timestamp": datetime.now().isoformat()
            }
            return detection_event
        else:
            return {
                "type": "no_match",
                "student_name": "Unknown",
                "reg_no": "N/A",
                "status": "No Match Found",
                "timestamp": datetime.now().isoformat()
            }
    finally:
        conn.close()

@app.get("/attendance/notifications")
async def get_attendance_notifications(current_user: dict = Depends(get_current_user)):
    """Polling endpoint for the live dashboard to get recent unsubmitted detections."""
    if current_user["role"] != "faculty":
        return []
    
    faculty_id = current_user["user"]["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Get all unsubmitted attendance for sections owned by this faculty, limited to today
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('''
            SELECT a.timestamp, s.name as student_name, s.reg_no, a.status
            FROM attendance a
            JOIN students s ON a.student_id = s.id
            JOIN sections sec ON a.section_id = sec.id
            WHERE sec.faculty_id = ? AND a.submitted = 0 AND date(a.timestamp) = ?
            ORDER BY a.timestamp DESC
            LIMIT 20
        ''', (faculty_id, today))
        rows = [dict(row) for row in cursor.fetchall()]
        # Map to the 'detection' type format
        events = []
        for r in rows:
            events.append({
                "type": "detection",
                "student_name": r["student_name"],
                "reg_no": r["reg_no"],
                "status": r["status"],
                "timestamp": r["timestamp"]
            })
        return events
    finally:
        conn.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
