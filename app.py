from flask import Flask, render_template, request, send_file, jsonify, Response
import os, qrcode, time, shutil, json, uuid, mimetypes
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet
from threading import Thread

app = Flask(__name__)

# ---- Paths ----
BASE     = os.path.dirname(os.path.abspath(__file__))
UPLOAD   = os.path.join(BASE, "uploads")
ENCRYPT  = os.path.join(BASE, "encrypted")
STATIC   = os.path.join(BASE, "static")
QRFOLDER = os.path.join(STATIC, "qrcodes")
CHUNKS   = os.path.join(BASE, "chunks")

# Ensure folders exist
for f in [UPLOAD, ENCRYPT, STATIC, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

# ---- Config ----
PUBLIC      = "https://smartqr-oyjd.onrender.com"   # your public base URL
CHUNK_SIZE  = 5 * 1024 * 1024                       # 5MB (upload & encrypt in same size)
tasks       = {}                                     # file_id -> {"status": "...", "key": "...", "filename": "..."}

# ---- Helpers ----
def make_key() -> str:
    return Fernet.generate_key().decode()

def parse_key(k: str) -> bytes:
    return k.encode()

def encrypt_stream(src_path: str, dst_path: str, key: bytes) -> None:
    """Chunk-compatible Fernet encryption: writes [len|token] frames."""
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
    """Chunk-compatible Fernet decryption: reads [len|token] frames."""
    f = Fernet(key)
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            size_bytes = fin.read(4)
            if not size_bytes:
                break
            size = int.from_bytes(size_bytes, "big")
            token = fin.read(size)
            fout.write(f.decrypt(token))

# ---- Background job ----
def process_file(file_id: str, filename: str) -> None:
    """Merge uploaded parts, encrypt, save meta & QR, then mark task done."""
    try:
        folder      = os.path.join(CHUNKS, file_id)
        merged_path = os.path.join(UPLOAD, filename)
        enc_path    = os.path.join(ENCRYPT, filename)

        parts = sorted([p for p in os.listdir(folder) if p.endswith(".part")])

        # Merge parts
        with open(merged_path, "wb") as out:
            for p in parts:
                with open(os.path.join(folder, p), "rb") as ch:
                    shutil.copyfileobj(ch, out)

        # Cleanup chunk folder
        shutil.rmtree(folder, ignore_errors=True)

        # Encrypt
        key = make_key()
        encrypt_stream(merged_path, enc_path, parse_key(key))

        # Remove plain file
        try:
            os.remove(merged_path)
        except FileNotFoundError:
            pass

        # Save meta (store key to guarantee match later)
        with open(enc_path + ".meta", "w") as f:
            json.dump({"time": time.time(), "key": key}, f)

        # Generate QR for view link
        qr_name = f"{filename}_qr.png"
        qrcode.make(f"{PUBLIC}/view/{filename}").save(os.path.join(QRFOLDER, qr_name))

        # Mark task done
        tasks[file_id] = {"status": "done", "key": key, "filename": filename}

    except Exception as e:
        tasks[file_id] = {"status": "error", "error": str(e)}

# ---- Routes ----
@app.route("/")
def index():
    # Your upload UI lives here
    return render_template("preview.html")

# Chunk upload endpoint
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id  = request.form["file_id"]
    filename = secure_filename(request.form["filename"])
    index    = int(request.form["index"])
    chunk    = request.files["chunk"]

    folder = os.path.join(CHUNKS, file_id)
    os.makedirs(folder, exist_ok=True)

    part_path = os.path.join(folder, f"{index:08d}.part")
    if os.path.exists(part_path):
        return "OK", 200

    chunk.stream.seek(0)
    with open(part_path, "wb") as f:
        shutil.copyfileobj(chunk.stream, f)

    return "OK", 200

# Kick off background processing (returns immediately)
@app.route("/finish_upload")
def finish_upload():
    file_id  = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))

    tasks[file_id] = {"status": "processing"}
    Thread(target=process_file, args=(file_id, filename), daemon=True).start()

    return jsonify({"status": "processing"})

# Poll processing status
@app.route("/status/<file_id>")
def status(file_id):
    return jsonify(tasks.get(file_id, {"status": "unknown"}))

# Success page (shows QR + key)
@app.route("/success/<filename>")
def success(filename):
    filename    = secure_filename(filename)
    key         = request.args.get("key")  # usually passed from frontend after /status=done
    qr_image    = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"

    # Fallback: read key from meta if not provided
    if not key:
        meta_path = os.path.join(ENCRYPT, filename + ".meta")
        if os.path.exists(meta_path):
            try:
                key = json.load(open(meta_path)).get("key")
            except Exception:
                key = None

    # Expiry countdown (optional)
    meta = os.path.join(ENCRYPT, filename + ".meta")
    expires = None
    if os.path.exists(meta):
        try:
            data = json.load(open(meta))
            expires = max(0, int((data["time"] + 86400) - time.time()))
        except Exception:
            expires = None

    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_image,
        public_link=public_link,
        key=key,
        expires_in=expires,
        uuid=uuid.uuid4().hex
    )

# Public file view page (asks for key)
@app.route("/view/<filename>")
def view(filename):
    filename = secure_filename(filename)
    if not os.path.exists(os.path.join(ENCRYPT, filename)):
        return render_template("404.html")
    return render_template("view.html", filename=filename)

@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=secure_filename(filename))

# Decrypt using the exact key saved in meta
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    filename = secure_filename(filename)
    enc_path = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc_path):
        return render_template("404.html")

    # Load real key from meta
    meta_path = enc_path + ".meta"
    if not os.path.exists(meta_path):
        return "<h2>❌ Key not found</h2><a href='/'>Home</a>"
    try:
        real_key = json.load(open(meta_path)).get("key")
    except Exception:
        return "<h2>❌ Key read error</h2><a href='/'>Home</a>"

    key_entered = request.form.get("key", "")

    # Strict check (prevents decrypting with wrong/old key)
    if key_entered != real_key:
        return "<h2>❌ Wrong key</h2><a href='/'>Home</a>"

    # Decrypt to uploads/
    dec_path = os.path.join(UPLOAD, filename)
    try:
        decrypt_stream(enc_path, dec_path, parse_key(real_key))
    except Exception:
        if os.path.exists(dec_path):
            os.remove(dec_path)
        return "<h2>❌ Wrong key</h2><a href='/'>Home</a>"

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

# Streaming / download
@app.route("/uploads/<filename>")
def serve_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    if not os.path.exists(path):
        return render_template("404.html")

    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"

    # Force download with correct name
    if request.args.get("download") == "1":
        return send_file(path, mimetype=mimetype, as_attachment=True, download_name=filename)

    # HTTP Range streaming for video
    if mimetype.startswith("video/"):
        file_size = os.path.getsize(path)
        range_header = request.headers.get("Range")
        if range_header:
            bytes_range = range_header.replace("bytes=", "").split("-")
            start = int(bytes_range[0]) if bytes_range[0] else 0
            end = file_size - 1 if len(bytes_range) == 1 or not bytes_range[1] else int(bytes_range[1])
            length = end - start + 1

            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)

            return Response(
                data, 206, mimetype=mimetype,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length)
                }
            )

        return Response(
            open(path, "rb").read(),
            mimetype=mimetype,
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"}
        )

    # Inline preview for images/pdfs/others
    return send_file(path, mimetype=mimetype, as_attachment=False, download_name=filename)

# HEAD route for some browsers
@app.route("/uploads/<filename>", methods=["HEAD"])
def head_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    mimetype = mimetypes.guess_type(path)[0] or "video/mp4"
    return Response(headers={"Accept-Ranges": "bytes", "Content-Type": mimetype})

# ---- Local run ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
