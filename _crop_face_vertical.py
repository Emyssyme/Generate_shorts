import os
import cv2
import numpy as np
import time
import subprocess

# -------------------------------------------------------------------
# Hard-coded configuration variables - update these with your paths!
# -------------------------------------------------------------------

INPUT_FOLDER = "path_to_your_folder"  
OUTPUT_FOLDER = "path_to_your_output_folder"  
OVERLAY_PATH = "path_to_your_overlay.png"  
here = os.path.dirname(os.path.abspath(__file__))
CAFFE_MODEL = os.path.join(here, "utils", "res10_300x300_ssd_iter_140000.caffemodel")
PROTOTXT = os.path.join(here, "utils", "deploy.prototxt.txt")
# -------------------------------------------------------------------

def get_video_files(input_folder):
    supported_ext = [".mp4", ".mov", ".avi", ".mkv"]
    return [os.path.join(input_folder, f) for f in os.listdir(input_folder)
            if os.path.splitext(f)[1].lower() in supported_ext]

def load_overlay(overlay_path, output_size):
    overlay = cv2.imread(overlay_path, cv2.IMREAD_UNCHANGED)
    if overlay is None:
        return None
    return cv2.resize(overlay, output_size)

def detect_face_center(frame, prev_gray, last_center, last_area, net, conf_threshold=0.7):
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
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w - 1, x2), min(h - 1, y2)
            
            area = (x2 - x1) * (y2 - y1)
            if area < 1000:  
                continue
                
            center = ((x1 + x2) // 2, (y1 + y2) // 2)

            motion_score = 0.0
            if prev_gray is not None:
                roi_current = gray[y1:y2, x1:x2]
                roi_prev = prev_gray[y1:y2, x1:x2]
                if roi_current.shape == roi_prev.shape and roi_current.size > 0:
                    diff = cv2.absdiff(roi_current, roi_prev)
                    motion_score = np.mean(diff)
            else:
                motion_score = 2.0

            valid_faces.append({'center': center, 'area': area, 'motion_score': motion_score})

    if not valid_faces:
        return None, None, gray

    if last_area is not None:
        valid_faces = [f for f in valid_faces if f['area'] > last_area * 0.3]
        
    if not valid_faces:
        return None, None, gray

    max_area_in_frame = max(f['area'] for f in valid_faces)
    candidates = [f for f in valid_faces if f['area'] >= max_area_in_frame * 0.4]

    if last_center is not None:
        closest = min(candidates, key=lambda f: np.sqrt((f['center'][0] - last_center[0])**2 + (f['center'][1] - last_center[1])**2))
        dist = np.sqrt((closest['center'][0] - last_center[0])**2 + (closest['center'][1] - last_center[1])**2)
        
        if dist < w * 0.20:
            return closest['center'], closest['area'], gray
        else:
            if closest['motion_score'] > 1.0:
                return closest['center'], closest['area'], gray
            return None, None, gray

    best_candidate = max(candidates, key=lambda f: f['area'] * (1.0 + f['motion_score']))
    return best_candidate['center'], best_candidate['area'], gray


def process_video(video_path, output_path, net, overlay, window_size=30):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[Error] Could not open video: {video_path}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    orig_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    desired_aspect = 9 / 16
    if int(desired_aspect * orig_height) <= orig_width:
        crop_w = int(desired_aspect * orig_height)
        crop_h = orig_height
    else:
        crop_w = orig_width
        crop_h = int(orig_width / desired_aspect)

    print(f"Pass 1 (face detection) for video: {video_path}")
    face_centers = []
    start_time = time.time()
    
    prev_gray = None
    last_known_center = None
    last_known_area = None
    frames_lost = 0  

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret: break

        center, area, prev_gray = detect_face_center(frame, prev_gray, last_known_center, last_known_area, net, conf_threshold=0.7)
        face_centers.append(center)
        
        if center is not None:
            last_known_center = center
            last_known_area = area
            frames_lost = 0
        else:
            frames_lost += 1
            if frames_lost > 5:
                last_known_center = None
                last_known_area = None

        frame_idx += 1
        if frame_idx % 30 == 0 or frame_idx == total_frames:
            elapsed = time.time() - start_time
            if frame_idx > 0:
                estimated = (elapsed / frame_idx) * (total_frames - frame_idx)
                print(f"  Detected {frame_idx}/{total_frames} frames. Estimated time remaining: {estimated:.2f} sec.")
    cap.release()

    # --- UMPLEREA GOLURILOR (Forward/Backward Fill) ---
    last_center = None
    for i in range(len(face_centers)):
        if face_centers[i] is None and last_center is not None:
            face_centers[i] = last_center
        elif face_centers[i] is not None:
            last_center = face_centers[i]

    last_center = None
    for i in range(len(face_centers) - 1, -1, -1):
        if face_centers[i] is None and last_center is not None:
            face_centers[i] = last_center
        elif face_centers[i] is not None:
            last_center = face_centers[i]

    # --- ETAPA 1: FILTRUL DEADZONE ---
    # Înlătură micro-mișcările și tremurul
    deadzone_threshold = 15
    stable_centers = []
    prev_center = None
    for center in face_centers:
        if center is None:
            center = (orig_width // 2, orig_height // 2)
        if prev_center is None:
            stable = center
        else:
            dist = np.sqrt((center[0] - prev_center[0])**2 + (center[1] - prev_center[1])**2)
            if dist < deadzone_threshold:
                stable = prev_center 
            else:
                stable = center
        stable_centers.append(stable)
        prev_center = stable

    # --- ETAPA 2: FILTRUL CINEMATIC (MOVING AVERAGE) ---
    # Netezește complet traiectoria folosind o fereastră de X cadre
    if len(stable_centers) > window_size:
        xs = [c[0] for c in stable_centers]
        ys = [c[1] for c in stable_centers]
        
        # Adăugăm padding la capete ca să nu pierdem cadre
        pad_size = window_size // 2
        xs_padded = np.pad(xs, (pad_size, window_size - pad_size - 1), mode='edge')
        ys_padded = np.pad(ys, (pad_size, window_size - pad_size - 1), mode='edge')
        
        kernel = np.ones(window_size) / window_size
        xs_smoothed = np.convolve(xs_padded, kernel, mode='valid')
        ys_smoothed = np.convolve(ys_padded, kernel, mode='valid')
        
        smoothed_centers = list(zip(map(int, xs_smoothed), map(int, ys_smoothed)))
    else:
        smoothed_centers = stable_centers

    # Pass 2: Rendering
    cap = cv2.VideoCapture(video_path)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    temp_output = output_path + "_temp.mp4"
    out = cv2.VideoWriter(temp_output, fourcc, fps, (1080, 1920))

    print(f"Pass 2 (video processing) for video: {video_path}")
    start_time = time.time()
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret: break

        center = smoothed_centers[frame_idx] if frame_idx < len(smoothed_centers) else (orig_width // 2, orig_height // 2)
        cx, cy = center

        crop_x = cx - crop_w // 2
        crop_y = cy - crop_h // 2
        crop_x = max(0, min(crop_x, orig_width - crop_w))
        crop_y = max(0, min(crop_y, orig_height - crop_h))

        crop_frame = frame[crop_y:crop_y + crop_h, crop_x:crop_x + crop_w]
        resized_frame = cv2.resize(crop_frame, (1080, 1920))

        if overlay is not None:
            if overlay.shape[2] == 4:
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

    # Merging Audio
    final_output = output_path + ".mp4"
    print("Merging audio using ffmpeg...")
    cmd = [
        "ffmpeg", "-y",
        "-i", temp_output,
        "-i", video_path,
        "-c:v", "copy", "-c:a", "aac",
        "-map", "0:v:0", "-map", "1:a:0",
        final_output
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(temp_output)
    print(f"Finished processing video: {final_output}")

def main(input_path=None, output_dir=None, overlay_path=None,
         proto=PROTOTXT, model=CAFFE_MODEL, window_size=30, cpu_only=False):
    if output_dir is None:
        output_dir = OUTPUT_FOLDER
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    overlay = None
    if overlay_path is not None:
        overlay = load_overlay(overlay_path, (1080, 1920))

    net = cv2.dnn.readNetFromCaffe(proto, model)
    
    if not cpu_only:
        try:
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            print("Using CUDA for face detection.")
        except Exception as e:
            print("CUDA not available, using CPU. Error:", e)
    else:
        print("CPU-only mode requested. Bypassing CUDA.")

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
        process_video(video_path, dest, net, overlay, window_size=window_size)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crop face to vertical format")
    parser.add_argument("--input", help="input file or folder", required=True)
    parser.add_argument("--output", help="output folder", required=True)
    parser.add_argument("--overlay", help="optional overlay PNG path")
    parser.add_argument("--proto", default=PROTOTXT, help="Caffe prototxt")
    parser.add_argument("--model", default=CAFFE_MODEL, help="Caffe model file")
    parser.add_argument("--window_size", type=int, default=30, help="Higher = smoother camera pans (e.g. 15 to 60)")
    parser.add_argument("--cpu-only", action="store_true", help="Force CPU-only processing")
    args = parser.parse_args()

    main(input_path=args.input,
         output_dir=args.output,
         overlay_path=args.overlay,
         proto=args.proto,
         model=args.model,
         window_size=args.window_size,
         cpu_only=args.cpu_only)