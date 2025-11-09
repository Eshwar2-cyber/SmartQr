from flask import Flask, render_template, request, send_file, jsonify
import os, qrcode, base64, time, shutil, json
from werkzeug.utils import secure_filename
from Cryptodome.Cipher import AES
import uuid

app = Flask(__name__)

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD = os.path.join(BASE, "uploads")
ENCRYPT = os.path.join(BASE, "encrypted")
QRFOLDER = os.path.join(BASE, "static", "qrcodes")
CHUNKS = os.path.join(BASE, "chunks")

for f in [UPLOAD, ENCRYPT, QRFOLDER, CHUNKS]:
    os.makedirs(f, exist_ok=True)

PUBLIC = "https://smartqr-oyjd.onrender.com"
CHUNK_SIZE = 4 * 1024 * 1024

def make_key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()

def parse_key(k):
    return base64.urlsafe_b64decode(k.encode())

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

@app.route("/")
def index():
    return render_template("preview.html")

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
        return jsonify({"status": "error"}), 404

    final_path = os.path.join(UPLOAD, filename)
    parts = sorted([f for f in os.listdir(folder) if f.endswith(".part")])

    with open(final_path, "wb") as out:
        for p in parts:
            with open(os.path.join(folder, p), "rb") as ch:
                shutil.copyfileobj(ch, out)
    shutil.rmtree(folder)

    key = make_key()
    keyb = parse_key(key)

    enc_path = os.path.join(ENCRYPT, filename)
    encrypt_stream(final_path, enc_path, keyb)
    os.remove(final_path)

    with open(enc_path + ".meta", "w") as f:
        json.dump({"time": time.time()}, f)

    qr_name = f"{filename}_qr.png"
    qr_full_path = os.path.join(QRFOLDER, qr_name)
    qrcode.make(f"{PUBLIC}/view/{filename}").save(qr_full_path)

    return jsonify({
        "status": "ok",
        "key": key,
        "qr": qr_name,
        "public_link": f"{PUBLIC}/view/{filename}",
        "filename": filename
    })

@app.route("/success/<filename>")
def success(filename):
    qr_image = f"{filename}_qr.png"
    public_link = f"{PUBLIC}/view/{filename}"
    key = request.args.get("key")

    meta = os.path.join(ENCRYPT, filename + ".meta")
    expires = None
    if os.path.exists(meta):
        data = json.load(open(meta))
        expires = max(0, int((data["time"] + 86400) - time.time()))

    return render_template(
        "success.html",
        filename=filename,
        qr_image=qr_image,
        uuid=uuid.uuid4().hex,
        public=PUBLIC,
        key=key,
        expires_in=expires
    )
