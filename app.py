from flask import Flask, render_template, request, send_from_directory
import os, qrcode, threading, time, requests, subprocess, re
from werkzeug.utils import secure_filename

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
QR_FOLDER = "static/qrcodes"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ========== AUTO START CLOUDFLARE AND GET PUBLIC URL ==========
public_url = None

def start_cloudflare_tunnel():
    global public_url
    print("🌐 Starting Cloudflare tunnel... please wait...")

    # Start the tunnel as a subprocess
    process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://127.0.0.1:5000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    # Read output line-by-line to detect the public URL
    for line in iter(process.stdout.readline, ''):
        if "trycloudflare.com" in line:
            url_match = re.search(r"https://[-\w]+\.trycloudflare\.com", line)
            if url_match:
                public_url = url_match.group(0)
                print(f"✅ Public URL detected: {public_url}")
                break

# Start the tunnel in a background thread
threading.Thread(target=start_cloudflare_tunnel, daemon=True).start()

# ========== FILE UPLOAD & QR GENERATION ==========
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    global public_url
    if "file" not in request.files:
        return "No file uploaded", 400
    file = request.files["file"]
    if file.filename == "":
        return "No selected file", 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    # Wait for Cloudflare public URL if not ready yet
    while public_url is None:
        print("⏳ Waiting for Cloudflare public link...")
        time.sleep(1)

    file_url = f"{public_url}/view/{filename}"

    # Generate QR code
    qr_img = qrcode.make(file_url)
    qr_filename = f"{filename}.png"
    qr_path = os.path.join(QR_FOLDER, qr_filename)
    qr_img.save(qr_path)

    return render_template("success.html", filename=filename, qr_image=qr_filename, public_link=file_url)

@app.route("/view/<filename>")
def view_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ========== AUTO CLEANUP (optional) ==========
def cleanup_worker(upload_folder, qr_folder, retention_seconds, interval):
    while True:
        now = time.time()
        for folder in [upload_folder, qr_folder]:
            for f in os.listdir(folder):
                path = os.path.join(folder, f)
                try:
                    if os.path.isfile(path) and now - os.path.getmtime(path) > retention_seconds:
                        os.remove(path)
                except Exception:
                    pass
        time.sleep(interval)

# Start cleanup every 10 mins, keep files for 1 hour
threading.Thread(target=cleanup_worker, args=(UPLOAD_FOLDER, QR_FOLDER, 3600, 600), daemon=True).start()

# ========== MAIN ==========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
