from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, base64, time, shutil, json
from werkzeug.utils import secure_filename
from Cryptodome.Cipher import AES

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
ENCRYPT = os.path.join(BASE, "encrypted")
QRFOLDER = os.path.join(BASE, "static", "qrcodes")
CHUNKS = os.path.join(BASE, "chunks")

for f in [UPLOAD, ENCRYPT, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

PUBLIC = "https://smartqr-oyjd.onrender.com"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB

# ------ Key helpers ------
def make_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()

def parse_key(k):
    return base64.urlsafe_b64decode(k.encode())

# AES chunk format: [nonce][tag][length][ciphertext]
NONCE_LEN = 16
TAG_LEN = 16

def encrypt_stream(src, dst, keyb):
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            data = fin.read(CHUNK_SIZE)
            if not data:
                break
            cipher = AES.new(keyb, AES.MODE_EAX)
            ct, tag = cipher.encrypt_and_digest(data)
            fout.write(cipher.nonce)
            fout.write(tag)
            fout.write(len(ct).to_bytes(4, "big"))
            fout.write(ct)


def decrypt_stream(src, dst, keyb):
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            nonce = fin.read(NONCE_LEN)
            if not nonce:
                break
            tag = fin.read(TAG_LEN)
            size = int.from_bytes(fin.read(4), "big")
            ct = fin.read(size)

            cipher = AES.new(keyb, AES.MODE_EAX, nonce=nonce)
            pt = cipher.decrypt(ct)
            cipher.verify(tag)
            fout.write(pt)

# HOME
@app.route("/")
def index():
    return render_template("preview.html")

# ---- chunk upload ----
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

@app.route("/finish_upload")
def finish_upload():
    file_id = request.args.get("file_id")
    filename = secure_filename(request.args.get("filename"))
    folder = os.path.join(CHUNKS, file_id)

    if not os.path.isdir(folder):
        return jsonify({"status": "error", "error": "missing upload"}), 404

    final_path = os.path.join(UPLOAD, filename)
    parts = sorted([f for f in os.listdir(folder) if f.endswith(".part")])

    with open(final_path, "wb") as out:
        for p in parts:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)
    shutil.rmtree(folder)

    key = make_key()
    keyb = parse_key(key)

    enc = os.path.join(ENCRYPT, filename)
    encrypt_stream(final_path, enc, keyb)
    os.remove(final_path)

    with open(enc + ".meta", "w") as f:
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
    public_link = f"{PUBLIC}/view/{filename}"
    qr_image = f"{filename}_qr.png"

    key = request.args.get("key")

    # expiration
    meta = os.path.join(ENCRYPT, filename + ".meta")
    expires_in = None
    if os.path.exists(meta):
        data = json.load(open(meta))
        expires_in = max(0, int((data["time"] + 86400) - time.time()))

    # force browser load new QR version
    qr_image = qr_image + "?v=" + str(time.time())

    return render_template(
        "success.html",
        qr_image=qr_image,
        key=key,
        public_link=public_link,
        expires_in=expires_in
    )

@app.route("/view/<filename>")
def view(filename):
    enc = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc):
        return render_template("404.html")
    return render_template("view.html", filename=filename)

@app.route("/unlock/<filename>")
def unlock(filename):
    return render_template("unlock.html", filename=filename)

@app.route("/decrypt/<filename>", methods=["POST"])
def decrypt(filename):
    key = request.form.get("key")
    enc = os.path.join(ENCRYPT, filename)
    if not os.path.exists(enc):
        return render_template("404.html")

    try:
        keyb = parse_key(key)
    except:
        return "<h2>❌ Invalid key format</h2><a href='/'>Go Home</a>"

    dec = os.path.join(UPLOAD, filename)
    try:
        decrypt_stream(enc, dec, keyb)
    except:
        return "<h2>❌ Wrong Key</h2><a href='/'>Go Home</a>"

    return render_template("decrypted_success.html", link=f"/uploads/{filename}")

@app.route("/uploads/<filename>")
def serve_file(filename):
    return send_file(os.path.join(UPLOAD, filename), as_attachment=False)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
