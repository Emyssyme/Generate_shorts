import os
import cv2
import numpy as np
import time
import subprocess

# -------------------------------------------------------------------
# Hard-coded configuration variables - update these with your paths!
# -------------------------------------------------------------------

INPUT_FOLDER = "path_to_your_folder"  # Path to the folder with input videos
OUTPUT_FOLDER = "path_to_your_output_folder"  # Output folder for processed videos
OVERLAY_PATH = "path_to_your_overlay.png"  # Path to overlay PNG image, or set to None to disable
# by default look in a sibling "utils" folder inside the Generate_shorts project
here = os.path.dirname(os.path.abspath(__file__))
CAFFE_MODEL = os.path.join(here, "utils", "res10_300x300_ssd_iter_140000.caffemodel")
PROTOTXT = os.path.join(here, "utils", "deploy.prototxt.txt")
# override via command‑line if desired
# -------------------------------------------------------------------

def get_video_files(input_folder):
    supported_ext = [".mp4", ".mov", ".avi", ".mkv"]
    return [os.path.join(input_folder, f) for f in os.listdir(input_folder)
            if os.path.splitext(f)[1].lower() in supported_ext]

def load_overlay(overlay_path, output_size):
    overlay = cv2.imread(overlay_path, cv2.IMREAD_UNCHANGED)
    if overlay is None:
        print("[Error] Overlay image not found!")
        return None
    overlay = cv2.resize(overlay, output_size)
    return overlay

def detect_face_center(frame, prev_gray, last_center, net, conf_threshold=0.5, motion_threshold=1.5):
    """
    Detectează fețele combinând mișcarea adaptivă cu urmărirea (Sticky Tracking).
    Dacă apare mișcare evidentă în cadru, prioritizează fața care se mișcă pentru a evita
    blocarea pe tablouri sau icoane statice de la începutul videoclipului.
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104.0, 177.0, 123.0))
    net.setInput(blob)
    detections = net.forward()

    valid_faces = []

    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence > conf_threshold:
            box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
            (x1, y1, x2, y2) = box.astype("int")
            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(w - 1, x2)
            y2 = min(h - 1, y2)
            
            area = (x2 - x1) * (y2 - y1)
            if area < 1000:  # Ignorăm zgomotul mic
                continue
                
            center = ((x1 + x2) // 2, (y1 + y2) // 2)

            # Calculăm mișcarea în interiorul feței curent vs anterior
            motion_score = 0.0
            if prev_gray is not None:
                roi_current = gray[y1:y2, x1:x2]
                roi_prev = prev_gray[y1:y2, x1:x2]
                
                if roi_current.shape == roi_prev.shape and roi_current.size > 0:
                    diff = cv2.absdiff(roi_current, roi_prev)
                    motion_score = np.mean(diff)
            else:
                # Primul cadru primește un scor simbolic
                motion_score = 2.0

            valid_faces.append({
                'center': center,
                'area': area,
                'motion_score': motion_score
            })

    if not valid_faces:
        return None, gray

    # Separăm fețele care se mișcă activ de cele statice
    moving_faces = [f for f in valid_faces if f['motion_score'] > motion_threshold]

    # --- REGULA 1: DACĂ AVEM TIMP DE MIȘCARE ACTIVĂ ÎN CADRU ---
    if moving_faces:
        # Dacă aveam deja un istoric (last_center)
        if last_center is not None:
            # Căutăm dacă printre fețele în mișcare există una aproape de unde știam noi (vorbitorul care doar s-a mișcat puțin)
            max_allowed_distance = w * 0.25
            moving_near_last = [
                f for f in moving_faces 
                if np.sqrt((f['center'][0] - last_center[0])**2 + (f['center'][1] - last_center[1])**2) < max_allowed_distance
            ]
            if moving_near_last:
                # Vorbitorul curent se mișcă și e pe poziție -> îl alegem pe el
                best_face = max(moving_near_last, key=lambda f: f['area'])
                return best_face['center'], gray

        # Dacă nu e niciuna în mișcare aproape de vechiul centru, înseamnă că omul real s-a mișcat în altă parte 
        # sau s-a activat acum. Luăm cea mai mare față în mișcare din tot cadrul (ignorând fețele statice/tablourile).
        best_face = max(moving_faces, key=lambda f: f['area'])
        return best_face['center'], gray

    # --- REGULA 2: FALLBACK (Nimeni nu se mișcă activ în acest cadru - ex: pauză de vorbire) ---
    if last_center is not None:
        # Păstrăm fața cea mai apropiată de poziția anterioară (chiar dacă acum e statică)
        max_allowed_distance = w * 0.25
        closest_face = min(valid_faces, key=lambda f: np.sqrt((f['center'][0] - last_center[0])**2 + (f['center'][1] - last_center[1])**2))
        dist = np.sqrt((closest_face['center'][0] - last_center[0])**2 + (closest_face['center'][1] - last_center[1])**2)
        
        if dist < max_allowed_distance:
            return closest_face['center'], gray

    # --- REGULA 3: PRIMUL CADRU SAU TOTUL E PIERDUT ---
    # Alegem pur și simplu cea mai mare față detectată
    best_face = max(valid_faces, key=lambda f: f['area'])
    return best_face['center'], gray

def process_video(video_path, output_path, net, overlay, smoothing=0.8):
    """
    Processes one video file:
      - Pass 1: Perform face detection on every frame and record the center positions.
      - Fill missing detections with forward/backward fill and apply smoothing.
      - Pass 2: For every frame, compute a crop rectangle (vertical 1080x1920) centered on the (smoothed) face.
      - Resize, apply overlay, and write processed frames.
      - Finally, merge the preserved audio from the original video using ffmpeg.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[Error] Could not open video: {video_path}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    orig_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Determine crop size with 9:16 aspect ratio.
    desired_aspect = 9 / 16
    if int(desired_aspect * orig_height) <= orig_width:
        crop_w = int(desired_aspect * orig_height)
        crop_h = orig_height
    else:
        crop_w = orig_width
        crop_h = int(orig_width / desired_aspect)

    # ----- Pass 1: Face detection and center extraction -----
    print(f"Pass 1 (face detection) for video: {video_path}")
    face_centers = []
    start_time = time.time()
    frame_idx = 0
    
    prev_gray = None
    last_known_center = None

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Transmitem cadrul anterior și ultimul centru pentru a detecta mișcarea
        center, prev_gray = detect_face_center(frame, prev_gray, last_known_center, net, conf_threshold=0.5)
        
        face_centers.append(center)
        
        if center is not None:
            last_known_center = center
            
        frame_idx += 1

        if frame_idx % 30 == 0 or frame_idx == total_frames:
            elapsed = time.time() - start_time
            # Evităm împărțirea la zero în cazul puțin probabil în care suntem la cadrul 0
            if frame_idx > 0:
                estimated = (elapsed / frame_idx) * (total_frames - frame_idx)
                print(f"  Detected {frame_idx}/{total_frames} frames. Estimated time remaining: {estimated:.2f} sec.")
    cap.release()

    # Forward fill for missing detection values.
    last_center = None
    for i in range(len(face_centers)):
        if face_centers[i] is None and last_center is not None:
            face_centers[i] = last_center
        elif face_centers[i] is not None:
            last_center = face_centers[i]

    # Backward fill.
    last_center = None
    for i in range(len(face_centers) - 1, -1, -1):
        if face_centers[i] is None and last_center is not None:
            face_centers[i] = last_center
        elif face_centers[i] is not None:
            last_center = face_centers[i]

    # Smooth the centers with exponential smoothing.
    smoothed_centers = []
    prev_center = None
    for center in face_centers:
        if center is None:
            center = (orig_width // 2, orig_height // 2)
        if prev_center is None:
            smoothed = center
        else:
            smoothed = (
                int(smoothing * prev_center[0] + (1 - smoothing) * center[0]),
                int(smoothing * prev_center[1] + (1 - smoothing) * center[1])
            )
        smoothed_centers.append(smoothed)
        prev_center = smoothed

    # ----- Pass 2: Frame processing with cropping, overlay, and writing out video -----
    cap = cv2.VideoCapture(video_path)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    temp_output = output_path + "_temp.mp4"
    out = cv2.VideoWriter(temp_output, fourcc, fps, (1080, 1920))

    print(f"Pass 2 (video processing) for video: {video_path}")
    start_time = time.time()
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Get the smoothed face center for the current frame.
        center = smoothed_centers[frame_idx] if frame_idx < len(smoothed_centers) else (orig_width // 2, orig_height // 2)
        cx, cy = center

        # Compute crop rectangle so that face center is centered.
        crop_x = cx - crop_w // 2
        crop_y = cy - crop_h // 2

        # Ensure crop remains within image boundaries.
        crop_x = max(0, min(crop_x, orig_width - crop_w))
        crop_y = max(0, min(crop_y, orig_height - crop_h))

        crop_frame = frame[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]

        # Resize the crop to the vertical 1080x1920 output.
        resized_frame = cv2.resize(crop_frame, (1080, 1920))

        # Add overlay if provided.
        if overlay is not None:
            if overlay.shape[2] == 4:
                # Separate overlay into color and alpha channels.
                overlay_bgr = overlay[:, :, :3]
                overlay_alpha = overlay[:, :, 3] / 255.0
                alpha_3 = cv2.merge([overlay_alpha, overlay_alpha, overlay_alpha])
                resized_frame = (overlay_bgr * alpha_3 + resized_frame * (1 - alpha_3)).astype(np.uint8)
            else:
                resized_frame = cv2.addWeighted(overlay, 0.5, resized_frame, 0.5, 0)

        out.write(resized_frame)
        frame_idx += 1

        if frame_idx % 30 == 0 or frame_idx == total_frames:
            elapsed = time.time() - start_time
            estimated = (elapsed / frame_idx) * (total_frames - frame_idx)
            print(f"  Processed {frame_idx}/{total_frames} frames. Estimated time remaining: {estimated:.2f} sec.")

    cap.release()
    out.release()

    # ----- Merging Audio using ffmpeg -----
    final_output = output_path + ".mp4"
    print("Merging audio using ffmpeg...")
    cmd = [
        "ffmpeg", "-y",
        "-i", temp_output,    # Processed video (no audio)
        "-i", video_path,     # Original video (for audio)
        "-c:v", "copy", "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        final_output
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(temp_output)
    print(f"Finished processing video: {final_output}")

def main(input_path=None, output_dir=None, overlay_path=None,
         proto=PROTOTXT, model=CAFFE_MODEL, smoothing=0.8):
    """Process either a single file or every file in a directory.

    Parameters mirror the earlier hardcoded globals but are now arguments so
    the module can be invoked programmatically or from the command line.
    """
    # determine output directory
    if output_dir is None:
        output_dir = OUTPUT_FOLDER
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # load overlay if requested
    overlay = None
    if overlay_path is not None:
        overlay = load_overlay(overlay_path, (1080, 1920))

    # Load face detection network.
    net = cv2.dnn.readNetFromCaffe(proto, model)
    try:
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
        print("Using CUDA for face detection.")
    except Exception as e:
        print("CUDA not available, using CPU. Error:", e)

    # build list of videos to process
    if input_path is None:
        video_files = get_video_files(INPUT_FOLDER)
    elif os.path.isdir(input_path):
        video_files = get_video_files(input_path)
    else:
        video_files = [input_path]

    if not video_files:
        print("No supported video files found in:", input_path or INPUT_FOLDER)
        return

    for video_path in video_files:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        dest = os.path.join(output_dir, base_name + "_processed")
        print(f"\nProcessing video: {video_path}")
        process_video(video_path, dest, net, overlay, smoothing=smoothing)


# CLI entrypoint
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crop face to vertical format")
    parser.add_argument("--input", help="input file or folder", required=True)
    parser.add_argument("--output", help="output folder", required=True)
    parser.add_argument("--overlay", help="optional overlay PNG path")
    parser.add_argument("--proto", default=PROTOTXT, help="Caffe prototxt")
    parser.add_argument("--model", default=CAFFE_MODEL, help="Caffe model file (overrides default utils path)")
    parser.add_argument("--smoothing", type=float, default=0.8)
    args = parser.parse_args()

    main(input_path=args.input,
         output_dir=args.output,
         overlay_path=args.overlay,
         proto=args.proto,
         model=args.model,
         smoothing=args.smoothing)
