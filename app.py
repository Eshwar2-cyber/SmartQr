from flask import Flask, render_template, request, send_from_directory
import os, qrcode, threading, time
from werkzeug.utils import secure_filename

app = Flask(__name__)

# ==========================
# Folder Configurations
# ==========================
UPLOAD_FOLDER = "uploads"
QR_FOLDER = os.path.join("static", "qrcodes")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ==========================
# FILE UPLOAD & QR GENERATION
# ==========================
@app.route("/")
def index():
    return render_template("success.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return "No file uploaded", 400

    file = request.files["file"]
    if file.filename == "":
        return "No selected file", 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    # Use your Render public domain instead of Cloudflare
    public_base = "https://smartqr-pe0z.onrender.com"
    file_url = f"{public_base}/view/{filename}"

    # Generate QR code
    qr_img = qrcode.make(file_url)
    qr_filename = f"{filename}.png"
    qr_path = os.path.join(QR_FOLDER, qr_filename)
    qr_img.save(qr_path)

    return render_template(
        "view.html",
        filename=filename,
        qr_image=qr_filename,
        public_link=file_url
    )

@app.route("/view/<filename>")
def view_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ==========================
# AUTO CLEANUP (optional)
# ==========================
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

threading.Thread(
    target=cleanup_worker,
    args=(UPLOAD_FOLDER, QR_FOLDER, 3600, 600),
    daemon=True
).start()

# ==========================
# MAIN
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
