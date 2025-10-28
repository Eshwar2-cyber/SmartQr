from flask import Flask, render_template, request, send_file
import os, qrcode, base64, time, threading
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)

# ==========================
# Folder Configurations
# ==========================
UPLOAD_FOLDER = "uploads"
ENCRYPTED_FOLDER = "encrypted"
QR_FOLDER = os.path.join("static", "qrcodes")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ENCRYPTED_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# ==========================
# HOME PAGE
# ==========================
@app.route("/")
def home():
    return render_template("preview.html")


# ==========================
# FILE UPLOAD + ENCRYPTION + QR GENERATION
# ==========================
@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return "No file uploaded", 400

    file = request.files["file"]
    if file.filename == "":
        return "No selected file", 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    # Encrypt file
    key = Fernet.generate_key()
    cipher = Fernet(key)

    with open(filepath, "rb") as f:
        encrypted_data = cipher.encrypt(f.read())

    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    with open(encrypted_path, "wb") as ef:
        ef.write(encrypted_data)

    os.remove(filepath)  # remove unencrypted file

    # Generate QR
    public_base = "https://smartqr-pe0z.onrender.com"
    file_url = f"{public_base}/view/{filename}"

    qr_img = qrcode.make(file_url)
    qr_filename = f"{filename}_qr.png"
    qr_path = os.path.join(QR_FOLDER, qr_filename)
    qr_img.save(qr_path)

    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_filename,
        public_link=file_url,
        key=key.decode()
    )


# ==========================
# VIEW FILE (QR SCAN PAGE)
# ==========================
@app.route("/view/<filename>")
def view_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)
    return render_template("view.html", filename=filename)


# ==========================
# UNLOCK FILE (ENTER KEY)
# ==========================
@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


# ==========================
# DECRYPT AND SHOW FILE
# ==========================
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt_file(filename):
    key = request.form.get("key")
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)

    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    try:
        cipher = Fernet(key.encode())
        with open(encrypted_path, "rb") as ef:
            decrypted_data = cipher.decrypt(ef.read())

        decrypted_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(decrypted_path, "wb") as df:
            df.write(decrypted_data)

        return render_template(
            "success.html",
            filename=filename,
            qr_image=None,
            public_link=f"/uploads/{filename}",
            key=None
        )
    except Exception:
        return "<h2>❌ Invalid Key! Access Denied.</h2><a href='/'>Return Home</a>"


# ==========================
# DOWNLOAD / VIEW ROUTE
# ==========================
@app.route("/uploads/<filename>")
def download_file(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return render_template("404.html", filename=filename)
    return send_file(path, as_attachment=False)


# ==========================
# AUTO CLEANUP
# ==========================
def cleanup_worker(folders, retention_seconds=3600, interval=600):
    while True:
        now = time.time()
        for folder in folders:
            for f in os.listdir(folder):
                path = os.path.join(folder, f)
                try:
                    if os.path.isfile(path) and now - os.path.getmtime(path) > retention_seconds:
                        os.remove(path)
                except:
                    pass
        time.sleep(interval)


threading.Thread(
    target=cleanup_worker,
    args=([UPLOAD_FOLDER, ENCRYPTED_FOLDER, QR_FOLDER], 3600, 600),
    daemon=True
).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
