import cv2
import numpy as np
import tensorflow as tf
from ultralytics import YOLO
import os

# Initialize YOLOv8 for face detection
# In production/Vercel, we hope yolov8n.pt is in the root. 
# If not, it will try to download to the current dir (which might fail on read-only FS).
detector = YOLO("yolov8n.pt")


# Initialize FaceNet (.h5)
# Check project root first, then static
MODEL_PATH = "facenet_keras.h5"
if not os.path.exists(MODEL_PATH):
    MODEL_PATH = os.path.join("static", "facenet_keras.h5")

facenet_model = None

# Initialize Haar Cascade once at module level for speed
# This uses cv2 data, which is fine on Vercel if opencv-python-headless is installed.
cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
face_cascade = cv2.CascadeClassifier(cascade_path)

if os.path.exists(MODEL_PATH):
    try:
        facenet_model = tf.keras.models.load_model(MODEL_PATH)
        print(f"Loaded FaceNet from {MODEL_PATH} successfully")
    except Exception as e:
        print(f"Error loading {MODEL_PATH}: {e}")

# Fallback to keras-facenet if .h5 is missing or fails
if facenet_model is None:
    try:
        from keras_facenet import FaceNet
        facenet_engine = FaceNet()
        print("Using keras-facenet as fallback for embedding")
    except Exception as e:
        print(f"Could not load keras-facenet: {e}")
        facenet_engine = None

def pre_process(img):
    """Pre-process image for FaceNet model."""
    img = cv2.resize(img, (160, 160))
    img = img.astype('float32')
    mean, std = img.mean(), img.std()
    img = (img - mean) / std
    return np.expand_dims(img, axis=0)

def get_face_embedding(image_bytes: bytes):
    """
    1) Detect face using YOLOv8n
    2) Extract and pre-process face
    3) Get embedding using FaceNet (.h5)
    Returns: (embedding, cropped_face_bytes)
    """
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        return None, None
    
    h_orig, w_orig = img.shape[:2]
    
    # 0. Optimization: Downscale for detection if too large
    max_det_dim = 640
    if max(h_orig, w_orig) > max_det_dim:
        scale = max_det_dim / max(h_orig, w_orig)
        img_det = cv2.resize(img, (int(w_orig * scale), int(h_orig * scale)))
    else:
        scale = 1.0
        img_det = img

    # 1. Detect faces with YOLO (Using faster imgsz)
    results = detector(img_det, verbose=False, imgsz=320)
    
    best_face_img = None
    
    if results and len(results[0].boxes) > 0:
        for b in results[0].boxes:
            cls = int(b.cls[0])
            name = results[0].names[cls].lower()
            box = b.xyxy[0].cpu().numpy()
            
            if "face" in name:
                x1, y1, x2, y2 = (box / scale).astype(int)
                best_face_img = img[max(0, y1):min(h_orig, y2), max(0, x1):min(w_orig, x2)]
                break
            elif "person" in name:
                px1, py1, px2, py2 = box.astype(int)
                person_roi = img_det[py1:py2, px1:px2]
                if person_roi.size > 0:
                    gray_roi = cv2.cvtColor(person_roi, cv2.COLOR_BGR2GRAY)
                    faces = face_cascade.detectMultiScale(gray_roi, 1.1, 4)
                    if len(faces) > 0:
                        fx, fy, fw, fh = sorted(faces, key=lambda f: f[2]*f[3], reverse=True)[0]
                        # Map back to original image
                        x1 = int((px1 + fx) / scale)
                        y1 = int((py1 + fy) / scale)
                        x2 = int((px1 + fx + fw) / scale)
                        y2 = int((py1 + fy + fh) / scale)
                        best_face_img = img[max(0, y1):min(h_orig, y2), max(0, x1):min(w_orig, x2)]
                        break

    # 2. Final Fallback: Full image Cascade if YOLO/ROI failed
    if best_face_img is None:
        gray = cv2.cvtColor(img_det, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        if len(faces) > 0:
            fx, fy, fw, fh = sorted(faces, key=lambda f: f[2]*f[3], reverse=True)[0]
            x1, y1, x2, y2 = int(fx/scale), int(fy/scale), int((fx+fw)/scale), int((fy+fh)/scale)
            best_face_img = img[max(0, y1):min(h_orig, y2), max(0, x1):min(w_orig, x2)]

    if best_face_img is None or best_face_img.size == 0:
        return None, None

    # Encode cropped face to bytes for saving
    _, buffer = cv2.imencode('.jpg', best_face_img)
    face_bytes = buffer.tobytes()

    # 2. Convert to RGB for embedding (FaceNet expects 160x160 usually)
    face_rgb = cv2.cvtColor(best_face_img, cv2.COLOR_BGR2RGB)
    
    # 3. Generate Embedding
    embedding = None
    if facenet_model:
        processed_face = pre_process(face_rgb)
        embedding_res = facenet_model.predict(processed_face, verbose=False)
        embedding = embedding_res[0].flatten().tolist()
    elif facenet_engine:
        embeddings = facenet_engine.embeddings([face_rgb])
        embedding = embeddings[0].tolist()
        
    return embedding, face_bytes

def compare_faces(stored_embedding_list, current_embedding_list, tolerance=0.7):
    """
    Compares two FaceNet embeddings using Euclidean distance.
    """
    stored = np.array(stored_embedding_list)
    current = np.array(current_embedding_list)
    
    # Calculate Euclidean distance
    distance = np.linalg.norm(stored - current)
    
    return distance <= tolerance
