import os
import re
from dotenv import load_dotenv

load_dotenv()
import uuid
import shutil
import zipfile
import mimetypes
import smtplib
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from flask import Flask, request, render_template, flash, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/email_sender_uploads")


def extract_zip(zip_path, extract_to):
    """Extract ZIP and return the root directory of its contents."""
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_to)

    # If ZIP contains a single root folder, use that as base
    items = os.listdir(extract_to)
    if len(items) == 1 and os.path.isdir(os.path.join(extract_to, items[0])):
        return os.path.join(extract_to, items[0])
    return extract_to


def find_html_file(base_dir):
    """Find the main HTML file in the extracted directory."""
    # Prefer index.html, then email.html, then first .html found
    for name in ("index.html", "email.html"):
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            return path

    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.lower().endswith(".html"):
                return os.path.join(root, f)
    return None


def inline_images(html_content, base_dir):
    """Replace local image references with CID references and collect image parts."""
    images = []
    src_pattern = re.compile(r'(src\s*=\s*["\'])([^"\']+)(["\'])', re.IGNORECASE)

    def replacer(match):
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)

        # Skip external URLs and already-CID references
        if src.startswith(("http://", "https://", "cid:", "data:")):
            return match.group(0)

        img_path = os.path.normpath(os.path.join(base_dir, src))
        if not os.path.isfile(img_path):
            return match.group(0)

        cid = uuid.uuid4().hex
        mime_type, _ = mimetypes.guess_type(img_path)
        if not mime_type or not mime_type.startswith("image/"):
            return match.group(0)

        subtype = mime_type.split("/")[1]
        with open(img_path, "rb") as f:
            img_data = f.read()

        img = MIMEImage(img_data, _subtype=subtype)
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(img_path))
        images.append(img)

        return f'{prefix}cid:{cid}{suffix}'

    new_html = src_pattern.sub(replacer, html_content)
    return new_html, images


def send_email(smtp_host, smtp_port, smtp_user, smtp_pass, use_tls,
               from_email, to_email, subject, html_content, image_parts):
    """Send the HTML email with inline images via SMTP."""
    msg = MIMEMultipart("related")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")

    html_part = MIMEText(html_content, "html", "utf-8")
    msg.attach(html_part)

    for img in image_parts:
        msg.attach(img)

    if use_tls:
        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
    else:
        server = smtplib.SMTP(smtp_host, smtp_port)

    if smtp_user and smtp_pass:
        server.login(smtp_user, smtp_pass)

    server.sendmail(from_email, to_email, msg.as_string())
    server.quit()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Collect form data
        smtp_host = request.form.get("smtp_host", "").strip()
        smtp_port = int(request.form.get("smtp_port", 587))
        smtp_user = request.form.get("smtp_user", "").strip()
        smtp_pass = request.form.get("smtp_pass", "").strip()
        use_tls = request.form.get("use_tls") == "on"
        from_email = request.form.get("from_email", "").strip()
        to_email = request.form.get("to_email", "").strip()
        subject = request.form.get("subject", "").strip()

        if not smtp_host or not from_email or not to_email:
            flash("Preencha todos os campos obrigatórios.", "error")
            return redirect(url_for("index"))

        zip_file = request.files.get("zip_file")
        if not zip_file or not zip_file.filename.lower().endswith(".zip"):
            flash("Envie um arquivo .zip válido.", "error")
            return redirect(url_for("index"))

        # Save and extract
        job_id = uuid.uuid4().hex
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        zip_path = os.path.join(job_dir, "upload.zip")
        zip_file.save(zip_path)

        try:
            base_dir = extract_zip(zip_path, job_dir)
            html_file = find_html_file(base_dir)

            if not html_file:
                flash("Nenhum arquivo HTML encontrado no ZIP.", "error")
                return redirect(url_for("index"))

            html_dir = os.path.dirname(html_file)
            with open(html_file, "r", encoding="utf-8") as f:
                html_content = f.read()

            html_content, image_parts = inline_images(html_content, html_dir)

            send_email(
                smtp_host, smtp_port, smtp_user, smtp_pass, use_tls,
                from_email, to_email, subject, html_content, image_parts,
            )

            flash("E-mail enviado com sucesso!", "success")
        except Exception as e:
            flash(f"Erro ao enviar: {e}", "error")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        return redirect(url_for("index"))

    return render_template("index.html")


if __name__ == "__main__":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.run(debug=True, host="0.0.0.0", port=5000)
