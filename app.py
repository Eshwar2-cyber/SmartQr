from flask import Flask, render_template, request, send_file, jsonify, Response
import os, qrcode, time, shutil, json, uuid, mimetypes, traceback
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet, InvalidToken
from threading import Thread

app = Flask(__name__)

# ---------- Paths ----------
BASE     = os.path.dirname(os.path.abspath(__file__))
UPLOAD   = os.path.join(BASE, "uploads")
ENCRYPT  = os.path.join(BASE, "encrypted")
STATIC   = os.path.join(BASE, "static")
QRFOLDER = os.path.join(STATIC, "qrcodes")
CHUNKS   = os.path.join(BASE, "chunks")

for p in [UPLOAD, ENCRYPT, STATIC, QRFOLDER, CHUNKS]:
    os.makedirs(p, exist_ok=True)

# ---------- Config ----------
PUBLIC     = "https://smartqr-oyjd.onrender.com"
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB
tasks      = {}  # file_id -> {"status", "key", "filename", "error"}

# ---------- Helpers ----------
def make_key() -> str:
    return Fernet.generate_key().decode()

def parse_key(k: str) -> bytes:
    return k.encode()

def encrypt_stream(src_path: str, dst_path: str, key: bytes) -> None:
    """Chunked encryption (safe for 10GB files)."""
    f = Fernet(key)
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            token = f.encrypt(chunk)
            fout.write(len(token).to_bytes(4, "big"))
            fout.write(token)

def decrypt_stream(src_path: str, dst_path: str, key: bytes) -> None:
    """Chunked decrypt (safe for large files)."""
    f = Fernet(key)
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            size_bytes = fin.read(4)
            if not size_bytes:
                break
            size = int.from_bytes(size_bytes, "big")

            remaining = size
            parts = []
            while remaining > 0:
                part = fin.read(min(1024 * 1024, remaining))
                if not part:
                    raise InvalidToken("Encrypted data incomplete.")
                parts.append(part)
                remaining -= len(part)

            token = b"".join(parts)
            fout.write(f.decrypt(token))

# ---------- Background Job ----------
def process_file(file_id: str, filename: str):
    try:
        folder      = os.path.join(CHUNKS, file_id)
        merged_path = os.path.join(UPLOAD, filename)
        enc_path    = os.path.join(ENCRYPT, filename)

        parts = sorted(p for p in os.listdir(folder) if p.endswith(".part"))

        # MERGE CHUNKS
        with open(merged_path, "wb") as out:
            for p in parts:
                with open(os.path.join(folder, p), "rb") as ch:
                    shutil.copyfileobj(ch, out)

        shutil.rmtree(folder, ignore_errors=True)

        # ENCRYPT
        key = make_key()
        encrypt_stream(merged_path, enc_path, parse_key(key))

        try:
            os.remove(merged_path)
        except:
            pass

        # META (key + time)
        with open(enc_path + ".meta", "w") as f:
            json.dump({"time": time.time(), "key": key}, f)

        # QR
        qr_name = f"{filename}_qr.png"
        qrcode.make(f"{PUBLIC}/view/{filename}").save(os.path.join(QRFOLDER, qr_name))

        tasks[file_id] = {"status": "done", "key": key, "filename": filename}

    except Exception as e:
        app.logger.error("[process_file] " + traceback.format_exc())
        tasks[file_id] = {"status": "error", "error": str(e)}

# ---------- Routes ----------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/preview")
def preview():
    return render_template("preview.html")

# ------------------------------------------
# 1️⃣ RECEIVE CHUNKS
# ------------------------------------------
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id  = request.form["file_id"]
    filename = secure_filename(request.form["filename"])
    index    = int(request.form["index"])
    chunk    = request.files["chunk"]

    folder = os.path.join(CHUNKS, file_id)
    os.makedirs(folder, exist_ok=True)

    part_path = os.path.join(folder, f"{index:08d}.part")

    # Avoid overwriting already uploaded parts
    if os.path.exists(part_path):
        return "OK", 200

    chunk.stream.seek(0)
    with open(part_path, "wb") as f:
        shutil.copyfileobj(chunk.stream, f)

    return "OK", 200

# ------------------------------------------
# 2️⃣ FINISH UPLOAD → ENCRYPT + QR GENERATE
# ------------------------------------------
@app.route("/finish_upload")
def finish_upload():
    file_id  = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))

    tasks[file_id] = {"status": "processing"}
    Thread(target=process_file, args=(file_id, filename), daemon=True).start()

    return jsonify({"status": "processing"})

# ------------------------------------------
# 3️⃣ STATUS CHECK
# ------------------------------------------
@app.route("/status/<file_id>")
def status(file_id):
    return jsonify(tasks.get(file_id, {"status": "unknown"}))

# ------------------------------------------
# 4️⃣ SUCCESS PAGE
# ------------------------------------------
@app.route("/success/<filename>")
def success(filename):
    filename    = secure_filename(filename)
    key         = request.args.get("key")
    qr_image    = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"

    # fetch key from meta if missing
    if not key:
        meta = os.path.join(ENCRYPT, filename + ".meta")
        if os.path.exists(meta):
            try:
                key = json.load(open(meta)).get("key")
            except:
                key = None

    # 24-hour expiration
    expires = None
    meta_path = os.path.join(ENCRYPT, filename + ".meta")
    if os.path.exists(meta_path):
        try:
            data = json.load(open(meta_path))
            expires = max(0, int((data["time"] + 86400) - time.time()))
        except:
            expires = None

    return render_template("success.html",
                           filename=filename,
                           qr_image=qr_image,
                           public_link=public_link,
                           key=key,
                           expires_in=expires,
                           uuid=uuid.uuid4().hex)

# ------------------------------------------
# 5️⃣ VIEW PAGE (Enter Key)
# ------------------------------------------
@app.route("/view/<filename>")
def view(filename):
    filename = secure_filename(filename)
    if not os.path.exists(os.path.join(ENCRYPT, filename)):
        return render_template("404.html")
    return render_template("view.html", filename=filename)

@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=secure_filename(filename))

# ------------------------------------------
# 6️⃣ DECRYPT
# ------------------------------------------
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    filename = secure_filename(filename)
    enc_path = os.path.join(ENCRYPT, filename)

    if not os.path.exists(enc_path):
        return "<h2>❌ Encrypted file missing</h2><a href='/'>Home</a>"

    meta_path = enc_path + ".meta"
    if not os.path.exists(meta_path):
        return "<h2>❌ Meta missing</h2><a href='/'>Home</a>"

    meta = json.load(open(meta_path))
    real_key = meta.get("key")
    entered  = request.form.get("key", "")

    if entered != real_key:
        return "<h2>❌ Wrong key</h2><a href='/'>Home</a>"

    dec_path = os.path.join(UPLOAD, filename)

    try:
        decrypt_stream(enc_path, dec_path, parse_key(real_key))
    except Exception as e:
        if os.path.exists(dec_path):
            os.remove(dec_path)
        return f"<h2>❌ Decrypt error: {str(e)}</h2><a href='/'>Home</a>"

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

# ------------------------------------------
# 7️⃣ SERVE FILES (Preview + Video Streaming)
# ------------------------------------------
@app.route("/uploads/<filename>")
def serve_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)

    if not os.path.exists(path):
        return render_template("404.html")

    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"

    # video streaming
    if mimetype.startswith("video/"):
        file_size = os.path.getsize(path)
        rng = request.headers.get("Range")

        if rng:
            a, b = rng.replace("bytes=", "").split("-")
            start = int(a) if a else 0
            end = file_size - 1 if not b else int(b)
            length = end - start + 1

            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)

            return Response(data, 206, mimetype=mimetype, headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length)
            })

        return Response(open(path, "rb").read(),
                        mimetype=mimetype,
                        headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"})

    return send_file(path, mimetype=mimetype, as_attachment=False, download_name=filename)

@app.route("/uploads/<filename>", methods=["HEAD"])
def head_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(headers={"Accept-Ranges": "bytes", "Content-Type": mimetype})

# ------------------------------------------
# Run
# ------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
