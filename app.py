from flask import Flask, render_template, request, jsonify, send_file
import os, time, shutil, json, qrcode, threading, base64
from werkzeug.utils import secure_filename
from Cryptodome.Cipher import AES  # <= PyCryptodome (wheel-only)

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
ENCRYPT = os.path.join(BASE, "encrypted")
QRFOLDER = os.path.join(BASE, "static", "qrcodes")
CHUNKS = os.path.join(BASE, "chunks")

for f in [UPLOAD, ENCRYPT, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

PUBLIC = "https://smartqr-pe0z.onrender.com"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB safe chunks

# ----- Simple helpers to encode/decode the binary key as a URL-safe string -----
def make_key() -> str:
    key = os.urandom(32)  # 256-bit key
    return base64.urlsafe_b64encode(key).decode()

def parse_key(key_str: str) -> bytes:
    return base64.urlsafe_b64decode(key_str.encode())

# We will write each encrypted chunk as: [nonce(16)][tag(16)][len(4)][ciphertext]
NONCE_LEN = 16
TAG_LEN = 16

def encrypt_file_stream(in_path: str, out_path: str, key_b: bytes):
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            data = fin.read(CHUNK_SIZE)
            if not data:
                break
            cipher = AES.new(key_b, AES.MODE_EAX)  # generates fresh nonce
            ciphertext, tag = cipher.encrypt_and_digest(data)

            # write nonce + tag + length + ciphertext
            fout.write(cipher.nonce)                     # 16 bytes
            fout.write(tag)                              # 16 bytes
            fout.write(len(ciphertext).to_bytes(4, "big"))
            fout.write(ciphertext)

def decrypt_file_stream(in_path: str, out_path: str, key_b: bytes):
    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        while True:
            nonce = fin.read(NONCE_LEN)
            if not nonce:
                break
            if len(nonce) != NONCE_LEN:
                raise ValueError("Corrupt file (nonce)")
            tag = fin.read(TAG_LEN)
            if len(tag) != TAG_LEN:
                raise ValueError("Corrupt file (tag)")
            sizeb = fin.read(4)
            if len(sizeb) != 4:
                raise ValueError("Corrupt file (length)")
            size = int.from_bytes(sizeb, "big")
            ciphertext = fin.read(size)
            if len(ciphertext) != size:
                raise ValueError("Corrupt file (ciphertext)")

            cipher = AES.new(key_b, AES.MODE_EAX, nonce=nonce)
            plaintext = cipher.decrypt(ciphertext)
            # verify tag (raises ValueError if wrong)
            cipher.verify(tag)
            fout.write(plaintext)

# ================= ROUTES =================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/preview")
def preview():
    return render_template("preview.html")

# ✅ CHUNK UPLOAD
@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file_id = request.form["file_id"]
    filename = secure_filename(request.form["filename"])
    index = int(request.form["index"])
    chunk = request.files["chunk"]

    folder = os.path.join(CHUNKS, file_id)
    os.makedirs(folder, exist_ok=True)

    chunk.save(os.path.join(folder, f"{index:08d}.part"))
    return "OK", 200

# ✅ FINISH MERGE + ENCRYPT + QR
@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))
    folder = os.path.join(CHUNKS, file_id)

    if not os.path.isdir(folder):
        return jsonify({"status": "error", "error": "upload missing"}), 404

    final_path = os.path.join(UPLOAD, filename)
    parts = sorted([f for f in os.listdir(folder) if f.endswith(".part")])

    # merge chunks
    with open(final_path, "wb") as out:
        for p in parts:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)

    shutil.rmtree(folder, ignore_errors=True)

    # ENCRYPT STREAM (PyCryptodome)
    key_str = make_key()
    key_b = parse_key(key_str)
    encrypted_path = os.path.join(ENCRYPT, filename)
    encrypt_file_stream(final_path, encrypted_path, key_b)

    # Save meta time
    with open(encrypted_path + ".meta", "w") as f:
        json.dump({"time": time.time()}, f)

    os.remove(final_path)

    # QR
    link = f"{PUBLIC}/view/{filename}"
    qr_name = f"{filename}_qr.png"
    qrcode.make(link).save(os.path.join(QRFOLDER, qr_name))

    return jsonify({
        "status": "ok",
        "key": key_str,         # urlsafe base64
        "qr": qr_name,
        "filename": filename,
        "public": link
    })

@app.route("/view/<filename>")
def view(filename):
    enc = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc):
        return render_template("404.html")

    time_left = 86400
    meta = enc + ".meta"
    if os.path.exists(meta):
        t = json.load(open(meta)).get("time")
        time_left = max(0, int(86400 - (time.time() - t)))

    return render_template("view.html", filename=filename, countdown=time_left)

@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)

# ✅ DECRYPT STREAM & PREVIEW
@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    key_str = request.form.get("key")
    enc = os.path.join(ENCRYPT, filename)

    if not os.path.exists(enc):
        return render_template("404.html")

    # parse key string back to bytes
    try:
        key_b = parse_key(key_str)
    except Exception:
        return "<h2>❌ Invalid key format</h2><a href='/'>Home</a>"

    dec = os.path.join(UPLOAD, filename)
    try:
        decrypt_file_stream(enc, dec, key_b)
    except Exception:
        # wrong key / tampered file / corrupt
        if os.path.exists(dec):
            try:
                os.remove(dec)
            except:
                pass
        return "<h2>❌ Wrong Key</h2><a href='/'>Home</a>"

    ext = filename.lower()

    if ext.endswith((".png",".jpg",".jpeg",".gif",".webp")):
        return render_template("decrypted_image.html", image=f"/uploads/{filename}")

    if ext.endswith((".mp4",".mov",".webm",".mkv")):
        return render_template("decrypted_video.html", video=f"/uploads/{filename}")

    if ext.endswith(".pdf"):
        return render_template("decrypted_pdf.html", pdf=f"/uploads/{filename}")

    if ext.endswith((".txt",".log",".json")):
        text = open(dec, "r", errors="ignore").read()
        return render_template("decrypted_text.html", content=text)

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

@app.route("/uploads/<filename>")
def serve_file(filename):
    return send_file(os.path.join(UPLOAD, filename), as_attachment=False)
@app.route("/success/<filename>")
def success(filename):
    qr = f"/static/qrcodes/{filename}_qr.png"

    # compute remaining seconds from meta
    expires_in = None
    meta_path = os.path.join(ENCRYPT, filename + ".meta")
    if os.path.exists(meta_path):
        try:
            data = json.load(open(meta_path))
            uploaded_at = data.get("time")
            ttl = 86400  # 24h
            expires_in = max(0, int((uploaded_at + ttl) - time.time()))
        except:
            expires_in = None

    # return old style page but working
    return render_template(
        "success.html",
        filename=filename,
        qr=qr,
        public=f"{PUBLIC}/view/{filename}",
        key=request.args.get("key"),
        expires_in=expires_in
    )


# ✅ RUN SERVER (local)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
