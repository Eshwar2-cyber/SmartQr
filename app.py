from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, time, threading, shutil, json, base64
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet, InvalidToken

app = Flask(__name__)

# ==========================
# Limits & Folders
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
CHUNK_SIZE = 4 * 1024 * 1024


# ==========================
# Home Page
# ==========================
@app.route("/")
def home():
    return render_template("preview.html")


# ==========================
# Chunk Upload
# ==========================
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id = request.form.get("file_id")
    filename = request.form.get("filename")
    index = request.form.get("index")
    chunk = request.files.get("chunk")

    if not all([file_id, filename, index, chunk]):
        return "Bad Request", 400

    safe_name = secure_filename(filename)
    folder = os.path.join(CHUNKS_FOLDER, file_id)
    os.makedirs(folder, exist_ok=True)

    idx = int(index)
    chunk_path = os.path.join(folder, f"{idx:08d}.part")
    chunk.save(chunk_path)

    return "OK", 200


# ==========================
# Encrypt Stream
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


# ==========================
# ✅ Decrypt Stream (FIXED)
# ==========================
def decrypt_file_stream(in_path, out_path, key):
    cipher = Fernet(key)
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            len_bytes = fin.read(4)
            if not len_bytes:
                break
            
            size = int.from_bytes(len_bytes, "big")
            token = fin.read(size)

            # If wrong key → InvalidToken
            chunk = cipher.decrypt(token)
            fout.write(chunk)


# ==========================
# Finish Upload → Encrypt → QR
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

        final_path = os.path.join(UPLOAD_FOLDER, safe_name)
        part_files = sorted(f for f in os.listdir(folder) if f.endswith(".part"))

        with open(final_path, "wb") as out:
            for part in part_files:
                with open(os.path.join(folder, part), "rb") as c:
                    shutil.copyfileobj(c, out)

        shutil.rmtree(folder, ignore_errors=True)

        key = Fernet.generate_key()
        encrypted_path = os.path.join(ENCRYPTED_FOLDER, safe_name)
        encrypt_file_stream(final_path, encrypted_path, key)

        with open(encrypted_path + ".meta", "w") as mf:
            json.dump({"uploaded_at": time.time()}, mf)

        os.remove(final_path)

        qr_filename = f"{safe_name}_qr.png"
        qr_path = os.path.join(QR_FOLDER, qr_filename)
        qrcode.make(f"{PUBLIC_BASE}/view/{safe_name}").save(qr_path)

        return jsonify({
            "status": "ok",
            "key": key.decode(),
            "qr": qr_filename,
            "filename": safe_name
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ==========================
# View Page
# ==========================
@app.route("/view/<filename>")
def view_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    uploaded_at = None
    meta = encrypted_path + ".meta"
    if os.path.exists(meta):
        try:
            uploaded_at = json.load(open(meta)).get("uploaded_at")
        except:
            pass

    return render_template("view.html", filename=filename, uploaded_at=uploaded_at)


# ==========================
# Unlock Page
# ==========================
@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


# ==========================
# ✅ Decrypt FIXED
# ==========================
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt_file(filename):
    encrypted_path = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted_path):
        return render_template("404.html", filename=filename)

    key = request.form.get("key", "").strip()

    # Validate key format
    try:
        base64.urlsafe_b64decode(key.encode())
    except:
        return "<h2>❌ Invalid Key format.</h2><a href='/'>Home</a>"

    try:
        decrypted_path = os.path.join(UPLOAD_FOLDER, filename)
        decrypt_file_stream(encrypted_path, decrypted_path, key.encode())

        return send_file(decrypted_path, as_attachment=True)

    except InvalidToken:
        return "<h2>❌ Wrong Key.</h2><a href='/'>Home</a>"
    except Exception as e:
        return f"<h2>❌ Error: {e}</h2><a href='/'>Home</a>"


# ==========================
# Success Page
# ==========================
@app.route("/success/<filename>")
def success_page(filename):
    safe = secure_filename(filename)
    qr_image = f"{safe}_qr.png"
    return render_template(
        "success.html",
        filename=safe,
        qr_image=qr_image,
        public_link=f"{PUBLIC_BASE}/view/{safe}"
    )


# ==========================
# Cleanup
# ==========================
def cleanup_worker():
    while True:
        now = time.time()
        for folder in [UPLOAD_FOLDER, ENCRYPTED_FOLDER, QR_FOLDER, CHUNKS_FOLDER]:
            try:
                for f in os.listdir(folder):
                    path = os.path.join(folder, f)
                    if now - os.path.getmtime(path) > 86400:
                        if os.path.isfile(path):
                            os.remove(path)
                        else:
                            shutil.rmtree(path, ignore_errors=True)
            except:
                pass
        time.sleep(600)


threading.Thread(target=cleanup_worker, daemon=True).start()


# ==========================
# Run
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
