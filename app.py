from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, time, threading, shutil, json
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)

# ==========================
# USE PERSISTENT STORAGE IN RAILWAY
# ==========================
PERSIST_DIR = "/app/storage"   # Mounted volume
os.makedirs(PERSIST_DIR, exist_ok=True)

UPLOAD_FOLDER = os.path.join(PERSIST_DIR, "uploads")
ENCRYPTED_FOLDER = os.path.join(PERSIST_DIR, "encrypted")
CHUNKS_FOLDER = os.path.join(PERSIST_DIR, "chunks")

STATIC_QR = os.path.join("static", "qrcodes")

for folder in [UPLOAD_FOLDER, ENCRYPTED_FOLDER, CHUNKS_FOLDER, STATIC_QR]:
    os.makedirs(folder, exist_ok=True)

PUBLIC_BASE = "https://smartqr-production.up.railway.app"
CHUNK_SIZE = 4 * 1024 * 1024


@app.route("/")
def home():
    return render_template("preview.html")


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

    chunk.save(os.path.join(folder, f"{int(index):08d}.part"))
    return "OK", 200


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


@app.route("/finish_upload")
def finish_upload():
    try:
        file_id = request.args.get("file_id")
        filename = request.args.get("filename")

        safe = secure_filename(filename)
        folder = os.path.join(CHUNKS_FOLDER, file_id)

        final_file = os.path.join(UPLOAD_FOLDER, safe)
        parts = sorted(f for f in os.listdir(folder) if f.endswith(".part"))

        with open(final_file, "wb") as out:
            for p in parts:
                with open(os.path.join(folder, p), "rb") as c:
                    shutil.copyfileobj(c, out)

        shutil.rmtree(folder, ignore_errors=True)

        key = Fernet.generate_key()
        encrypted_path = os.path.join(ENCRYPTED_FOLDER, safe)
        encrypt_file_stream(final_file, encrypted_path, key)

        with open(encrypted_path + ".meta", "w") as f:
            json.dump({"uploaded_at": time.time()}, f)

        os.remove(final_file)

        qr_file = safe + "_qr.png"
        qrcode.make(f"{PUBLIC_BASE}/view/{safe}").save(os.path.join(STATIC_QR, qr_file))

        return jsonify({"status": "ok", "key": key.decode(), "filename": safe})

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})


@app.route("/view/<filename>")
def view_file(filename):
    encrypted = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted):
        return render_template("404.html")

    return render_template("view.html", filename=filename)


@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)


@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt_file(filename):
    encrypted = os.path.join(ENCRYPTED_FOLDER, filename)
    if not os.path.exists(encrypted):
        return render_template("404.html")

    key = request.form.get("key")
    try:
        decrypted_path = os.path.join(UPLOAD_FOLDER, filename)
        decrypt_file_stream(encrypted, decrypted_path, key.encode())

        return render_template("decrypted_success.html", filename=filename)

    except:
        return "<h2>❌ Wrong Key</h2><a href='/'>Home</a>"


@app.route("/download/<filename>")
def download(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    return send_file(path, as_attachment=True)


@app.route("/preview/<filename>")
def preview(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    return send_file(path)


# Cleanup old files
def cleanup_worker():
    while True:
        now = time.time()
        for folder in [UPLOAD_FOLDER, ENCRYPTED_FOLDER, CHUNKS_FOLDER]:
            for f in os.listdir(folder):
                path = os.path.join(folder, f)
                if now - os.path.getmtime(path) > 86400:
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                        else:
                            shutil.rmtree(path)
                    except:
                        pass
        time.sleep(600)


threading.Thread(target=cleanup_worker, daemon=True).start()

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
