from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
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
    f = Fernet(key)
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            size_bytes = fin.read(4)
            if not size_bytes:
                break

            size = int.from_bytes(size_bytes, "big")

            remaining = size
            token_parts = []
            while remaining > 0:
                part = fin.read(min(1024 * 1024, remaining))
                if not part:
                    raise InvalidToken("Encrypted data incomplete")
                token_parts.append(part)
                remaining -= len(part)

            token = b"".join(token_parts)
            fout.write(f.decrypt(token))

# ---------- Background Job ----------
def process_file(file_id: str, filename: str) -> None:
    try:
        folder      = os.path.join(CHUNKS, file_id)
        merged_path = os.path.join(UPLOAD, filename)
        enc_path    = os.path.join(ENCRYPT, filename)

        parts = sorted(p for p in os.listdir(folder) if p.endswith(".part"))

        # MERGE
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

        # META save key + time
        with open(enc_path + ".meta", "w") as f:
            json.dump({"time": time.time(), "key": key}, f)

        # QR IMAGE
        qr_name = f"{filename}_qr.png"
        qrcode.make(f"{PUBLIC}/view/{filename}").save(os.path.join(QRFOLDER, qr_name))

        tasks[file_id] = {"status": "done", "key": key, "filename": filename}

    except Exception as e:
        app.logger.error("[process_file] " + traceback.format_exc())
        tasks[file_id] = {"status": "error", "error": str(e), "filename": filename}

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("preview.html")

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

@app.route("/finish_upload")
def finish_upload():
    file_id  = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))

    tasks[file_id] = {"status": "processing"}
    Thread(target=process_file, args=(file_id, filename), daemon=True).start()
    return jsonify({"status": "processing"})

@app.route("/status/<file_id>")
def status(file_id):
    return jsonify(tasks.get(file_id, {"status": "unknown"}))

@app.route("/success/<filename>")
def success(filename):
    filename    = secure_filename(filename)
    key         = request.args.get("key")
    qr_image    = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"

    if not key:
        meta_path = os.path.join(ENCRYPT, filename + ".meta")
        if os.path.exists(meta_path):
            try:
                key = json.load(open(meta_path)).get("key")
            except:
                key = None

    # expiration (24 hrs)
    expires = None
    meta = os.path.join(ENCRYPT, filename + ".meta")
    if os.path.exists(meta):
        try:
            data = json.load(open(meta))
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

@app.route("/view/<filename>")
def view(filename):
    filename = secure_filename(filename)
    if not os.path.exists(os.path.join(ENCRYPT, filename)):
        return render_template("404.html")
    return render_template("view.html", filename=filename)

@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=secure_filename(filename))

@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    filename = secure_filename(filename)
    enc_path = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc_path):
        return "<h2>❌ Encrypted file missing</h2><a href='/'>Home</a>"

    meta_path = enc_path + ".meta"
    if not os.path.exists(meta_path):
        return "<h2>❌ Meta missing. Re-upload the file.</h2><a href='/'>Home</a>"

    try:
        meta = json.load(open(meta_path))
        real_key = meta.get("key")
    except:
        return "<h2>❌ Meta corrupted</h2><a href='/'>Home</a>"

    entered = request.form.get("key", "")
    if entered != real_key:
        return "<h2>❌ Wrong key</h2><a href='/'>Home</a>"

    dec_path = os.path.join(UPLOAD, filename)

    try:
        decrypt_stream(enc_path, dec_path, parse_key(real_key))
    except InvalidToken:
        if os.path.exists(dec_path): os.remove(dec_path)
        return "<h2>❌ Invalid token. File corrupted or wrong key.</h2><a href='/'>Home</a>"
    except Exception as e:
        app.logger.error("[decrypt] " + traceback.format_exc())
        if os.path.exists(dec_path): os.remove(dec_path)
        return f"<h2>❌ Decrypt error: {str(e)}</h2><a href='/'>Home</a>"

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

# ---------- Helper: stream a byte range ----------
def stream_file_range(path, start, end):
    length = end - start + 1
    def generator():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            chunk_size = 64 * 1024
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                data = f.read(read_size)
                if not data:
                    break
                remaining -= len(data)
                yield data
    return generator()

# ------------------------------------------
# 7️⃣ SERVE FILES (Preview + Robust Range Streaming)
# ------------------------------------------
@app.route("/uploads/<path:filename>")
def serve_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    if not os.path.exists(path):
        return render_template("404.html")

    # force a safe binary MIME default
    guessed = mimetypes.guess_type(path)[0]
    mimetype = guessed or "application/octet-stream"

    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range', None)
    download_param = request.args.get("download") == "1"

    # If client requested a byte range, serve partial content (works for all types)
    if range_header:
        # Parse range: expecting "bytes=start-end"
        try:
            range_value = range_header.strip().split('=')[1]
            start_str, end_str = range_value.split('-')
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            if end >= file_size:
                end = file_size - 1
            if start > end:
                return Response(status=416)
        except Exception:
            # Bad Range header
            return Response(status=416)

        length = end - start + 1
        generator = stream_file_range(path, start, end)

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": mimetype,
            "Content-Encoding": "identity"
        }
        return Response(stream_with_context(generator), status=206, headers=headers, mimetype=mimetype)

    # No Range header: either stream whole file or send as attachment based on request
    # If user explicitly asked for download, send as attachment; otherwise for common previewable types allow inline.
    inline_types = ("text/", "image/", "application/pdf", "audio/", "video/")
    as_attachment = download_param or not any(mimetype.startswith(p) for p in inline_types)

    # Use send_file for simple full response but ensure correct headers for binary safety
    response = send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=filename)
    # Ensure these headers exist to prevent browser from modifying bytes
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Encoding"] = "identity"
    # Content-Length is already set by send_file / WSGI, but ensure present
    response.headers["Content-Length"] = str(file_size)
    return response

@app.route("/uploads/<filename>", methods=["HEAD"])
def head_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    if not os.path.exists(path):
        return Response(status=404)

    guessed = mimetypes.guess_type(path)[0]
    mimetype = guessed or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Content-Type": mimetype}
    return Response(headers=headers)

# ------------------------------------------
# Run
# ------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
