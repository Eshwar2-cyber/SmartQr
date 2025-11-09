from flask import Flask, render_template, request, send_file, jsonify, redirect, url_for
import os, qrcode, time, threading, shutil, json
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)

# -----------------------------------
# Folders
# -----------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ENCRYPTED_FOLDER = os.path.join(BASE_DIR, "encrypted")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
CHUNKS_FOLDER = os.path.join(BASE_DIR, "chunks")

for f in [UPLOAD_FOLDER, ENCRYPTED_FOLDER, QR_FOLDER, CHUNKS_FOLDER]:
    os.makedirs(f, exist_ok=True)

CHUNK_SIZE = 4 * 1024 * 1024
PUBLIC_BASE = "https://smartqr-pe0z.onrender.com"


# -----------------------------------
# HOME PAGE → index.html
# -----------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# -----------------------------------
# Upload Page (preview.html)
# -----------------------------------
@app.route("/upload")
def upload():
    return render_template("preview.html")


# -----------------------------------
# Upload Chunks
# -----------------------------------
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id = request.form.get("file_id")
    filename = request.form.get("filename")
    index = request.form.get("index")
    chunk = request.files.get("chunk")

    if not all([file_id, filename, index, chunk]):
        return "Bad Request", 400

    safe = secure_filename(filename)
    folder = os.path.join(CHUNKS_FOLDER, file_id)
    os.makedirs(folder, exist_ok=True)

    part_path = os.path.join(folder, f"{int(index):08d}.part")
    chunk.save(part_path)
    return "OK", 200


# -----------------------------------
# Encrypt Stream
# -----------------------------------
def encrypt_file_stream(src, dst, key):
    cipher = Fernet(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            data = fin.read(CHUNK_SIZE)
            if not data:
                break
            token = cipher.encrypt(data)
            fout.write(len(token).to_bytes(4, "big"))
            fout.write(token)


# -----------------------------------
# FINISH Upload -> Encrypt -> QR
# -----------------------------------
@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = request.args.get("filename")

    if not (file_id and filename):
        return jsonify({"status": "error", "error": "Missing params"})

    safe = secure_filename(filename)
    folder = os.path.join(CHUNKS_FOLDER, file_id)

    merged = os.path.join(UPLOAD_FOLDER, safe)
    part_files = sorted(f for f in os.listdir(folder) if f.endswith(".part"))

    # merge chunks
    with open(merged, "wb") as out:
        for p in part_files:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)

    shutil.rmtree(folder, ignore_errors=True)

    # encrypt
    key = Fernet.generate_key()
    encrypted = os.path.join(ENCRYPTED_FOLDER, safe)
    encrypt_file_stream(merged, encrypted, key)
    os.remove(merged)

    # save time
    with open(encrypted + ".meta", "w") as mf:
        json.dump({"uploaded_at": time.time()}, mf)

    # create QR
    qr_file = f"{safe}_qr.png"
    qr_path = os.path.join(QR_FOLDER, qr_file)
    qrcode.make(f"{PUBLIC_BASE}/view/{safe}").save(qr_path)

    return jsonify({
        "status": "ok",
        "key": key.decode(),
        "qr": qr_file,
        "filename": safe
    })


# -----------------------------------
# View (shows countdown + unlock)
# -----------------------------------
@app.route("/view/<filename>")
def view(filename):
    encrypted = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted):
        return render_template("404.html", filename=filename)

    uploaded_at = None
    meta = encrypted + ".meta"
    if os.path.exists(meta):
        uploaded_at = json.load(open(meta)).get("uploaded_at")

    return render_template("view.html", filename=filename, uploaded_at=uploaded_at)


# -----------------------------------
# Unlock Page
# -----------------------------------
@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


# -----------------------------------
# Decrypt => Go to preview page
# -----------------------------------
def decrypt_file_stream(src, dst, key):
    cipher = Fernet(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            ln = fin.read(4)
            if not ln:
                break
            size = int.from_bytes(ln, "big")
            token = fin.read(size)
            fout.write(cipher.decrypt(token))


@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt_file(filename):
    encrypted = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted):
        return render_template("404.html", filename=filename)

    key = request.form.get("key", "")
    try:
        out = os.path.join(UPLOAD_FOLDER, filename)
        decrypt_file_stream(encrypted, out, key.encode())

        return redirect(url_for("file_ready", filename=filename))

    except:
        return render_template("unlock.html", filename=filename, error="❌ Wrong key")


# -----------------------------------
# PREVIEW page after decryption
# -----------------------------------
@app.route("/ready/<filename>")
def file_ready(filename):
    return render_template(
        "file_ready.html",
        filename=filename,
        download_link=f"/download/{filename}"
    )


# -----------------------------------
# Download (only when decrypted)
# -----------------------------------
@app.route("/download/<filename>")
def download_file(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return "<h2>File not decrypted yet!</h2>"
    return send_file(path, as_attachment=True)


# -----------------------------------
# Auto cleanup (24h)
# -----------------------------------
def cleanup_worker():
    while True:
        now = time.time()
        for folder in [UPLOAD_FOLDER, ENCRYPTED_FOLDER, QR_FOLDER, CHUNKS_FOLDER]:
            try:
                for f in os.listdir(folder):
                    p = os.path.join(folder, f)
                    if now - os.path.getmtime(p) > 86400:
                        os.remove(p) if os.path.isfile(p) else shutil.rmtree(p, ignore_errors=True)
            except:
                pass
        time.sleep(600)


threading.Thread(target=cleanup_worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
