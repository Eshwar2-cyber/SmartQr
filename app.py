from flask import Flask, render_template, request, send_file, jsonify, Response
import os, qrcode, base64, time, shutil, json, uuid, mimetypes
from werkzeug.utils import secure_filename
from cryptography.fernet import Fernet

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
ENCRYPT = os.path.join(BASE, "encrypted")
STATIC = os.path.join(BASE, "static")
QRFOLDER = os.path.join(STATIC, "qrcodes")
CHUNKS = os.path.join(BASE, "chunks")

# Ensure folders exist
for f in [UPLOAD, ENCRYPT, STATIC, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

PUBLIC = "https://smartqr-oyjd.onrender.com"
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB


def make_key():
    return Fernet.generate_key().decode()

def parse_key(k):
    return k.encode()


# ✅ Chunk-compatible Fernet encryption
def encrypt_stream(src, dst, key):
    f = Fernet(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk:
                break
            token = f.encrypt(chunk)
            fout.write(len(token).to_bytes(4, "big"))
            fout.write(token)


# ✅ Chunk-compatible Fernet decryption
def decrypt_stream(src, dst, key):
    f = Fernet(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            size_bytes = fin.read(4)
            if not size_bytes:
                break
            size = int.from_bytes(size_bytes, "big")
            token = fin.read(size)
            fout.write(f.decrypt(token))


@app.route("/")
def index():
    return render_template("preview.html")


# ✅ Chunk upload with resume
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id = request.form["file_id"]
    filename = secure_filename(request.form["filename"])
    index = int(request.form["index"])
    chunk = request.files["chunk"]

    folder = os.path.join(CHUNKS, file_id)
    os.makedirs(folder, exist_ok=True)

    part_path = os.path.join(folder, f"{index:08d}.part")
    if os.path.exists(part_path):
        return "OK", 200

    chunk.stream.seek(0)
    with open(part_path, "wb") as f:
        shutil.copyfileobj(chunk.stream, f)

    return "OK", 200


# ✅ Merge chunks, encrypt, create QR
@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))
    folder = os.path.join(CHUNKS, file_id)

    if not os.path.isdir(folder):
        return jsonify({"status": "error"}), 404

    merged_path = os.path.join(UPLOAD, filename)
    parts = sorted([p for p in os.listdir(folder) if p.endswith(".part")])

    with open(merged_path, "wb") as out:
        for p in parts:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)

    shutil.rmtree(folder)

    key = make_key()
    enc_path = os.path.join(ENCRYPT, filename)
    encrypt_stream(merged_path, enc_path, parse_key(key))
    os.remove(merged_path)

    with open(enc_path + ".meta", "w") as f:
        json.dump({"time": time.time()}, f)

    qr_name = f"{filename}_qr.png"
    qrcode.make(f"{PUBLIC}/view/{filename}").save(os.path.join(QRFOLDER, qr_name))

    return jsonify({
        "status": "ok",
        "key": key,
        "qr_image": qr_name,
        "public_link": f"{PUBLIC}/view/{filename}",
        "filename": filename
    })


@app.route("/success/<filename>")
def success(filename):
    filename = secure_filename(filename)
    key = request.args.get("key")
    qr_image = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"

    meta = os.path.join(ENCRYPT, filename + ".meta")
    expires = None
    if os.path.exists(meta):
        data = json.load(open(meta))
        expires = max(0, int((data["time"] + 86400) - time.time()))

    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_image,
        public_link=public_link,
        key=key,
        expires_in=expires,
        uuid=uuid.uuid4().hex
    )


@app.route("/view/<filename>")
def view(filename):
    filename = secure_filename(filename)
    if not os.path.exists(os.path.join(ENCRYPT, filename)):
        return render_template("404.html")
    return render_template("view.html", filename=filename)


@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=secure_filename(filename))


# ✅ Decrypt
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    filename = secure_filename(filename)
    key = request.form.get("key")
    enc_path = os.path.join(ENCRYPT, filename)

    if not os.path.exists(enc_path):
        return render_template("404.html")

    dec_path = os.path.join(UPLOAD, filename)

    try:
        decrypt_stream(enc_path, dec_path, parse_key(key))
    except:
        if os.path.exists(dec_path): os.remove(dec_path)
        return "<h2>❌ Wrong key</h2><a href='/'>Home</a>"

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")


# ✅ UNIVERSAL STREAMING & CLEAN DOWNLOADS
@app.route("/uploads/<filename>")
def serve_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)

    if not os.path.exists(path):
        return render_template("404.html")

    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"

    # ✅ If ?download=1 → force correct filename on all phones
    if request.args.get("download") == "1":
        return send_file(
            path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename    # ✅ important fix
        )

    # ✅ Video streaming for browsers
    if mimetype.startswith("video/"):
        file_size = os.path.getsize(path)
        range_header = request.headers.get("Range")

        if range_header:
            bytes_range = range_header.replace("bytes=", "").split("-")
            start = int(bytes_range[0])
            end = file_size - 1 if bytes_range[1] == "" else int(bytes_range[1])
            length = end - start + 1

            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)

            return Response(
                data,
                206,
                mimetype=mimetype,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length)
                }
            )

        return Response(
            open(path, "rb").read(),
            mimetype=mimetype,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes"
            }
        )

    # ✅ PDF / Image preview inline
    return send_file(
        path,
        mimetype=mimetype,
        as_attachment=False,
        download_name=filename
    )


# ✅ HEAD route for stubborn browsers
@app.route("/uploads/<filename>", methods=["HEAD"])
def head_file(filename):
    filename = secure_filename(filename)
    path = os.path.join(UPLOAD, filename)
    mimetype = mimetypes.guess_type(path)[0] or "video/mp4"
    return Response(headers={
        "Accept-Ranges": "bytes",
        "Content-Type": mimetype
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
