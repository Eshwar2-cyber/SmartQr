from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, base64, time, shutil, json, uuid, hashlib
from werkzeug.utils import secure_filename
from Cryptodome.Cipher import AES

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
ENCRYPT = os.path.join(BASE, "encrypted")
STATIC = os.path.join(BASE, "static")
QRFOLDER = os.path.join(STATIC, "qrcodes")
CHUNKS = os.path.join(BASE, "chunks")

# ✅ Ensure folders exist
for f in [UPLOAD, ENCRYPT, STATIC, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

PUBLIC = "https://smartqr-oyjd.onrender.com"
CHUNK_SIZE = 1 * 1024 * 1024  # 1MB safer chunks

def make_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()

def parse_key(k):
    return base64.urlsafe_b64decode(k.encode())

# ---------- Helpers ----------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK_SIZE)
            if not b: break
            h.update(b)
    return h.hexdigest()

# AES-CTR streaming with explicit nonce length prefix
def encrypt_stream(src, dst, keyb):
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        cipher = AES.new(keyb, AES.MODE_CTR)
        nonce = cipher.nonce
        fout.write(len(nonce).to_bytes(1, "big"))  # 1 byte length
        fout.write(nonce)
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk: break
            fout.write(cipher.encrypt(chunk))

def decrypt_stream(src, dst, keyb):
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        nlen = int.from_bytes(fin.read(1), "big")
        nonce = fin.read(nlen)
        cipher = AES.new(keyb, AES.MODE_CTR, nonce=nonce)
        while True:
            chunk = fin.read(CHUNK_SIZE)
            if not chunk: break
            fout.write(cipher.decrypt(chunk))

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("preview.html")

# Resume-friendly chunk upload
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

    chunk.save(part_path)
    return "OK", 200

@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))
    folder = os.path.join(CHUNKS, file_id)

    if not os.path.isdir(folder):
        return jsonify({"status": "error", "error": "missing chunks"}), 404

    # Merge chunks
    final_path = os.path.join(UPLOAD, filename)
    parts = sorted([p for p in os.listdir(folder) if p.endswith(".part")])

    with open(final_path, "wb") as out:
        for p in parts:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)

    shutil.rmtree(folder)

    # Compute integrity hash & size BEFORE encryption
    orig_size = os.path.getsize(final_path)
    orig_sha = sha256_file(final_path)

    # Encrypt using CTR
    key = make_key()
    keyb = parse_key(key)
    enc_path = os.path.join(ENCRYPT, filename)
    encrypt_stream(final_path, enc_path, keyb)
    os.remove(final_path)

    # Save metadata (expiry + hash + size)
    meta = {
        "time": time.time(),
        "sha256": orig_sha,
        "size": orig_size
    }
    with open(enc_path + ".meta", "w") as f:
        json.dump(meta, f)

    # Generate QR
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
    qr_image = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"
    key = request.args.get("key")

    meta_path = os.path.join(ENCRYPT, filename + ".meta")
    expires = None
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r") as f:
                data = json.load(f)
            expires = max(0, int((data.get("time", 0) + 86400) - time.time()))
        except:
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

@app.route("/view/<filename>")
def view(filename):
    filename = secure_filename(filename)
    enc = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc):
        return render_template("404.html")
    return render_template("view.html", filename=filename)

@app.route("/unlock/<filename>")
def unlock(filename):
    filename = secure_filename(filename)
    return render_template("unlock.html", filename=filename)

@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    filename = secure_filename(filename)
    key = request.form.get("key")
    enc_path = os.path.join(ENCRYPT, filename)
    meta_path = enc_path + ".meta"

    if not os.path.exists(enc_path):
        return render_template("404.html")

    try:
        keyb = parse_key(key)
    except:
        return "<h2>❌ Invalid key format</h2><a href='/'>Home</a>"

    dec_path = os.path.join(UPLOAD, filename)

    # Decrypt to temp file first to verify
    tmp_path = dec_path + ".tmp"

    try:
        decrypt_stream(enc_path, tmp_path, keyb)
    except Exception as e:
        if os.path.exists(tmp_path): os.remove(tmp_path)
        return "<h2>❌ Decryption failed</h2><a href='/'>Home</a>"

    # Verify integrity (size + sha256)
    try:
        with open(meta_path, "r") as f:
            meta = json.load(f)
        ok_size = (os.path.getsize(tmp_path) == meta.get("size"))
        ok_hash = (sha256_file(tmp_path) == meta.get("sha256"))
    except:
        ok_size = True
        ok_hash = True  # if no meta, skip

    if not (ok_size and ok_hash):
        os.remove(tmp_path)
        return "<h2>❌ Wrong key</h2><p>Integrity check failed.</p><a href='/'>Home</a>"

    # Replace old/any existing & serve
    if os.path.exists(dec_path): os.remove(dec_path)
    os.replace(tmp_path, dec_path)

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

@app.route("/uploads/<filename>")
def serve_file(filename):
    filename = secure_filename(filename)
    # Let Flask infer mimetype from filename (so videos stream nicely)
    return send_file(os.path.join(UPLOAD, filename), as_attachment=False)

if __name__ == "__main__":
    # In production (Render) gunicorn handles this; debug True is fine for dev
    app.run(host="0.0.0.0", port=5000, debug=True)
