import os
import base64
import qrcode
from flask import Flask, render_template, request, send_from_directory, redirect, url_for, abort
from cryptography.fernet import Fernet
from werkzeug.utils import secure_filename

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
CHUNK_FOLDER = "chunks"
ENCRYPTED_FOLDER = "encrypted"
STATIC_QR_FOLDER = "static/qr"

for f in [UPLOAD_FOLDER, CHUNK_FOLDER, ENCRYPTED_FOLDER, STATIC_QR_FOLDER]:
    os.makedirs(f, exist_ok=True)


def encrypt_file(input_path, output_path):
    key = Fernet.generate_key()
    cipher = Fernet(key)

    with open(input_path, "rb") as infile:
        data = infile.read()
        enc = cipher.encrypt(data)

    with open(output_path, "wb") as outfile:
        outfile.write(enc)

    return base64.urlsafe_b64encode(key).decode()


def decrypt_file_data(enc_path, key):
    try:
        cipher = Fernet(key)
        with open(enc_path, "rb") as f:
            data = cipher.decrypt(f.read())
        return data
    except:
        return None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return "No file uploaded!"

    file = request.files["file"]
    if file.filename == "":
        return "No file selected."

    filename = secure_filename(file.filename)
    upload_path = os.path.join(UPLOAD_FOLDER, filename)
    enc_path = os.path.join(ENCRYPTED_FOLDER, filename + ".enc")

    file.save(upload_path)

    key = encrypt_file(upload_path, enc_path)

    public_link = request.host_url + "view/" + filename
    qr_img = qrcode.make(public_link)
    qr_file = os.path.join(STATIC_QR_FOLDER, filename + ".png")
    qr_img.save(qr_file)

    qr_url = url_for("static", filename=f"qr/{filename}.png")

    return render_template("success.html",
                           filename=filename,
                           key=key,
                           qr_url=qr_url,
                           public_link=public_link)


@app.route("/view/<filename>")
def view_file(filename):
    enc_file = os.path.join(ENCRYPTED_FOLDER, filename + ".enc")
    if not os.path.exists(enc_file):
        return render_template("404.html")

    return render_template("view.html", filename=filename)


@app.route("/unlock/<filename>", methods=["POST"])
def unlock(filename):
    key = request.form.get("key")
    if not key:
        return "Invalid key!"

    try:
        key = base64.urlsafe_b64decode(key)
    except:
        return render_template("404.html")

    enc_file = os.path.join(ENCRYPTED_FOLDER, filename + ".enc")
    data = decrypt_file_data(enc_file, key)

    if data is None:
        return render_template("404.html")

    preview_path = os.path.join(UPLOAD_FOLDER, filename)
    with open(preview_path, "wb") as f:
        f.write(data)

    return redirect(url_for("preview", filename=filename))


@app.route("/preview/<filename>")
def preview(filename):
    return render_template("preview.html", filename=filename)


@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


@app.errorhandler(404)
def error_page(e):
    return render_template("404.html")
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
