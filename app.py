from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, base64, time, threading, shutil, json
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)

# ==========================
# Folder Configurations
# ==========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ENCRYPTED_FOLDER = os.path.join(BASE_DIR, "encrypted")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
CHUNKS_FOLDER = os.path.join(BASE_DIR, "chunks")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ENCRYPTED_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(CHUNKS_FOLDER, exist_ok=True)

PUBLIC_BASE = "https://smartqr-pe0z.onrender.com"  # change if domain changes


# ==========================
# HOME PAGE
# ==========================
@app.route("/")
def home():
    return render_template("preview.html")


# ==========================
# CHUNK UPLOAD ENDPOINT
# ==========================
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id = request.form.get("file_id")
    filename = request.form.get("filename")
    index = request.form.get("index")
    total = request.form.get("total")
    chunk = request.files.get("chunk")

    if not all([file_id, filename, index is not None, total, chunk]):
        return "Bad Request", 400

    safe_name = secure_filename(filename)
    folder = os.path.join(CHUNKS_FOLDER, file_id)
    os.makedirs(folder, exist_ok=True)

    try:
        idx = int(index)
    except ValueError:
        return "Bad index", 400

    chunk_path = os.path.join(folder, f"{idx:08d}.part")
    chunk.save(chunk_path)

    return "OK", 200


# ==========================
# FINISH: MERGE → ENCRYPT → CREATE QR
# ==========================
@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = request.args.get("filename")

    if not file_id or not filename:
        return jsonify({"error": "missing params"}), 400

    safe_name = secure_filename(filename)
    folder = os.path.join(CHUNKS_FOLDER, file_id)

    if not os.path.isdir(folder):
        return jsonify({"error": "upload not found"}), 404

    final_path = os.path.join(UPLOAD_FOLDER, safe_name)
    part_files = sorted([f for f in os.listdir(folder) if f.endswith(".part")])

    with open(final_path, "wb") as outfile:
        for pf in part_files:
            with open(os.path.join(folder, pf), "rb") as ch:
                shutil.copyfileobj(ch, outfile)

    shutil.rmtree(folder, ignore_errors=True)

    # Encrypt
    key = Fernet.generate_key()
    cipher = Fernet(key)

    with open(final_path, "rb") as f:
        encrypted_data = cipher.encrypt(f.read())

    encrypted_path = os.path.join(ENCRYPTED_FOLDER, safe_name)
    with open(encrypted_path, "wb") as ef:
        ef.write(encrypted_data)

    try:
        os.remove(final_path)
    except:
        pass

    # Save upload time for countdown
    meta_path = os.path.join(ENCRYPTED_FOLDER, safe_name + ".meta")
    with open(meta_path, "w") as mf:
        json.dump({"uploaded_at": time.time()}, mf)

    # Generate QR
    file_url = f"{PUBLIC_BASE}/view/{safe_name}"
    qr_img = qrcode.make(file_url)
    qr_filename = f"{safe_name}_qr.png"
    qr_path = os.path.join(QR_FOLDER, qr_filename)
    qr_img.save(qr_path)

    return jsonify({"status": "ok", "key": key.decode(), "qr": qr_filename, "link": file_url})


# ==========================
# VIEW PAGE
# ==========================
@app.route("/view/<filename>")
def view_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    meta_path = os.path.join(ENCRYPTED_FOLDER, filename + ".meta")

    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r") as mf:
                meta = json.load(mf)
                uploaded_at = meta.get("uploaded_at", None)
        except:
            uploaded_at = None
    else:
        uploaded_at = None

    return render_template("view.html", filename=filename, uploaded_at=uploaded_at)


# ==========================
# ENTER KEY
# ==========================
@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


# ==========================
# DECRYPT
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
    except:
        return "<h2>❌ Invalid Key! Access Denied.</h2><a href='/'>Return Home</a>"


# ==========================
# DOWNLOAD
# ==========================
@app.route("/uploads/<filename>")
def download_file(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return render_template("404.html", filename=filename)
    return send_file(path, as_attachment=False)


# ==========================
# SUCCESS PAGE
# ==========================
@app.route("/success/<filename>")
def success_page(filename):
    qr_image = f"{filename}_qr.png"
    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_image,
        public_link=f"{PUBLIC_BASE}/view/{filename}",
        key=None
    )


# ==========================
# AUTO CLEANUP (24 hours)
# ==========================
def cleanup_worker(folders, retention_seconds=86400, interval=600):
    while True:
        now = time.time()
        for folder in folders:
            try:
                for f in os.listdir(folder):
                    path = os.path.join(folder, f)
                    try:
                        if os.path.isfile(path):
                            if now - os.path.getmtime(path) > retention_seconds:
                                os.remove(path)
                        elif os.path.isdir(path):
                            if now - os.path.getmtime(path) > retention_seconds:
                                shutil.rmtree(path, ignore_errors=True)
                    except:
                        pass
            except:
                pass
        time.sleep(interval)


threading.Thread(
    target=cleanup_worker,
    args=([UPLOAD_FOLDER, ENCRYPTED_FOLDER, QR_FOLDER, CHUNKS_FOLDER], 86400, 600),
    daemon=True
).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
