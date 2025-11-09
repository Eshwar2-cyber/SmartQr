from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, time, threading, shutil, json
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
from PIL import Image  # ✅ IMPORTANT: For QR PNG save (Pillow)

app = Flask(__name__)

# ==========================
# Limits & folders
# ==========================
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024  # 256 MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ENCRYPTED_FOLDER = os.path.join(BASE_DIR, "encrypted")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
CHUNKS_FOLDER = os.path.join(BASE_DIR, "chunks")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ENCRYPTED_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(CHUNKS_FOLDER, exist_ok=True)

PUBLIC_BASE = "https://smartqr-pe0z.onrender.com"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB stream size


# ==========================
# HOME PAGE
# ==========================
@app.route("/")
def home():
    return render_template("preview.html")


# ==========================
# CHUNK UPLOAD
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

    idx = int(index)
    chunk_path = os.path.join(folder, f"{idx:08d}.part")
    chunk.save(chunk_path)

    return "OK", 200


# ==========================
# STREAM ENCRYPT / DECRYPT
# ==========================
def encrypt_file_stream(in_path, out_path, key):
    cipher = Fernet(key)
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            data = fin.read(CHUNK_SIZE)
            if not data:
                break
            token = cipher.encrypt(data)
            fout.write(len(token).to_bytes(4, "big"))
            fout.write(token)


def decrypt_file_stream(in_path, out_path, key):
    cipher = Fernet(key)
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            length = fin.read(4)
            if not length:
                break
            size = int.from_bytes(length, "big")
            token = fin.read(size)
            fout.write(cipher.decrypt(token))


# ==========================
# FINISH -> ENCRYPT -> QR
# ==========================
@app.route("/finish_upload")
def finish_upload():
    try:
        file_id = request.args.get("file_id")
        filename = request.args.get("filename")

        if not (file_id and filename):
            return jsonify({"status": "error", "error": "missing params"}), 400

        safe_name = secure_filename(filename)
        folder = os.path.join(CHUNKS_FOLDER, file_id)

        if not os.path.isdir(folder):
            return jsonify({"status": "error", "error": "upload not found"}), 404

        final_path = os.path.join(UPLOAD_FOLDER, safe_name)
        parts = sorted(os.listdir(folder))

        with open(final_path, "wb") as outfile:
            for pf in parts:
                if pf.endswith(".part"):
                    with open(os.path.join(folder, pf), "rb") as ch:
                        shutil.copyfileobj(ch, outfile)

        shutil.rmtree(folder, ignore_errors=True)

        # Encrypt
        key = Fernet.generate_key()
        encrypted_path = os.path.join(ENCRYPTED_FOLDER, safe_name)
        encrypt_file_stream(final_path, encrypted_path, key)

        # Save time
        with open(encrypted_path + ".meta", "w") as mf:
            json.dump({"uploaded_at": time.time()}, mf)

        os.remove(final_path)

        # ✅ Create QR properly
        link = f"{PUBLIC_BASE}/view/{safe_name}"
        qr_name = f"{safe_name}_qr.png"
        qr_path = os.path.join(QR_FOLDER, qr_name)
        img = qrcode.make(link)
        img.save(qr_path)

        return jsonify({
            "status": "ok",
            "key": key.decode(),
            "qr": qr_name,
            "link": link
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ==========================
# VIEW PAGE
# ==========================
@app.route("/view/<filename>")
def view_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    meta_path = encrypted_path + ".meta"
    uploaded_at = None
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r") as mf:
                uploaded_at = json.load(mf).get("uploaded_at")
        except:
            uploaded_at = None

    return render_template("view.html", filename=filename, uploaded_at=uploaded_at)


# ==========================
# UNLOCK PAGE
# ==========================
@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


# ==========================
# DECRYPT
# ==========================
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    key = request.form.get("key")
    try:
        decrypted_path = os.path.join(UPLOAD_FOLDER, filename)
        decrypt_file_stream(encrypted_path, decrypted_path, key.encode())

        return render_template(
            "success.html",
            filename=filename,
            qr_image=None,
            public_link=f"/uploads/{filename}",
            uploaded_at=None
        )
    except:
        return "<h2>❌ Invalid Key or Decryption Error.</h2><a href='/'>Home</a>"


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
# ✅ SUCCESS PAGE FIXED
# ==========================
@app.route("/success/<filename>")
def success_page(filename):
    qr_url = f"{PUBLIC_BASE}/static/qrcodes/{filename}_qr.png"

    meta = os.path.join(ENCRYPTED_FOLDER, filename + ".meta")
    uploaded_at = None
    if os.path.exists(meta):
        try:
            with open(meta, "r") as mf:
                uploaded_at = json.load(mf).get("uploaded_at")
        except:
            uploaded_at = None

    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_url,     # ✅ Full URL now
        public_link=f"{PUBLIC_BASE}/view/{filename}",
        uploaded_at=uploaded_at
    )


# ==========================
# AUTO CLEANUP
# ==========================
def cleanup_worker(folders, retention_seconds=86400, interval=600):
    while True:
        now = time.time()
        for folder in folders:
            try:
                for f in os.listdir(folder):
                    path = os.path.join(folder, f)
                    try:
                        if os.path.isfile(path) and now - os.path.getmtime(path) > retention_seconds:
                            os.remove(path)
                        elif os.path.isdir(path) and now - os.path.getmtime(path) > retention_seconds:
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
