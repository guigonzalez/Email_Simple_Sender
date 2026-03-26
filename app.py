import os
import re
import traceback
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
from flask import Flask, request, render_template, flash, redirect, url_for, jsonify

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

    server.send_message(msg)
    server.quit()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Collect form data
        smtp_host = request.form.get("smtp_host", "").strip()
        smtp_port = int(request.form.get("smtp_port", 587))
        smtp_user = request.form.get("smtp_user", "").strip()
        smtp_pass = request.form.get("smtp_pass", "").strip().replace("\xa0", "")
        use_tls = request.form.get("use_tls") == "on"
        from_email = request.form.get("from_email", "").strip().replace("\xa0", " ")
        to_email = request.form.get("to_email", "").strip().replace("\xa0", " ")
        subject = request.form.get("subject", "").strip().replace("\xa0", " ")

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
            with open(html_file, "r", encoding="utf-8-sig", errors="replace") as f:
                html_content = f.read()

            html_content, image_parts = inline_images(html_content, html_dir)

            send_email(
                smtp_host, smtp_port, smtp_user, smtp_pass, use_tls,
                from_email, to_email, subject, html_content, image_parts,
            )

            flash("E-mail enviado com sucesso!", "success")
        except Exception as e:
            print(traceback.format_exc())
            flash(f"Erro ao enviar: {e}", "error")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        return redirect(url_for("index"))

    return render_template("index.html")


def analyze_html(html):
    """Analyze HTML email and return issues + scores per mail client."""
    issues = []
    soup_text = html  # raw string, no external parser needed

    def has(pattern, flags=re.IGNORECASE | re.DOTALL):
        return bool(re.search(pattern, soup_text, flags))

    def count(pattern, flags=re.IGNORECASE | re.DOTALL):
        return len(re.findall(pattern, soup_text, flags))

    # ── Checks ──────────────────────────────────────────────────────────────

    checks = []

    # Doctype
    if not has(r'<!DOCTYPE\s+html', re.IGNORECASE):
        checks.append({"id": "doctype", "severity": "error",
                        "message": "DOCTYPE ausente — obrigatório para renderização correta.",
                        "clients": ["outlook_classic", "gmail", "yahoo", "apple_mail", "outlook_web"]})

    # <html> tag
    if not has(r'<html[\s>]'):
        checks.append({"id": "html_tag", "severity": "error",
                        "message": "Tag <html> ausente.",
                        "clients": ["outlook_classic", "gmail", "yahoo", "apple_mail", "outlook_web"]})

    # <head> with charset
    if not has(r'<meta[^>]+charset', re.IGNORECASE):
        checks.append({"id": "charset", "severity": "error",
                        "message": "Meta charset ausente — pode causar problemas de encoding.",
                        "clients": ["gmail", "yahoo", "apple_mail", "outlook_web", "outlook_classic"]})

    # Table-based layout
    if not has(r'<table[\s>]'):
        checks.append({"id": "no_tables", "severity": "error",
                        "message": "Layout não usa tabelas — Outlook Classic ignora CSS de posicionamento (flexbox, grid, float).",
                        "clients": ["outlook_classic"]})

    # width em tabelas
    if has(r'<table[\s>]') and not has(r'<table[^>]+width'):
        checks.append({"id": "table_no_width", "severity": "warning",
                        "message": "Tabelas sem atributo width= — use width=600 ou similar para Outlook.",
                        "clients": ["outlook_classic"]})

    # CSS externo (link stylesheet)
    if has(r'<link[^>]+stylesheet', re.IGNORECASE):
        checks.append({"id": "external_css", "severity": "error",
                        "message": "CSS externo via <link> é bloqueado por Gmail, Yahoo e Outlook Web.",
                        "clients": ["gmail", "yahoo", "outlook_web"]})

    # CSS inline vs <style>
    style_block = has(r'<style[\s>]')
    if style_block:
        checks.append({"id": "style_block", "severity": "warning",
                        "message": "<style> no <head>: Gmail remove este bloco — prefira estilos inline.",
                        "clients": ["gmail"]})

    # background-image via CSS
    if has(r'background-image\s*:', re.IGNORECASE):
        checks.append({"id": "bg_image_css", "severity": "warning",
                        "message": "background-image via CSS não é suportado no Outlook Classic — use o atributo background= na tabela.",
                        "clients": ["outlook_classic"]})

    # position: absolute/fixed
    if has(r'position\s*:\s*(absolute|fixed)', re.IGNORECASE):
        checks.append({"id": "position_abs", "severity": "error",
                        "message": "position: absolute/fixed ignorado no Outlook Classic e Gmail.",
                        "clients": ["outlook_classic", "gmail"]})

    # border-radius
    if has(r'border-radius\s*:', re.IGNORECASE):
        checks.append({"id": "border_radius", "severity": "info",
                        "message": "border-radius ignorado no Outlook Classic (renderiza como quadrado).",
                        "clients": ["outlook_classic"]})

    # box-shadow
    if has(r'box-shadow\s*:', re.IGNORECASE):
        checks.append({"id": "box_shadow", "severity": "info",
                        "message": "box-shadow não suportado no Outlook Classic.",
                        "clients": ["outlook_classic"]})

    # flexbox
    if has(r'display\s*:\s*flex', re.IGNORECASE):
        checks.append({"id": "flexbox", "severity": "error",
                        "message": "Flexbox não suportado no Outlook Classic — use tabelas para layout.",
                        "clients": ["outlook_classic"]})

    # grid
    if has(r'display\s*:\s*grid', re.IGNORECASE):
        checks.append({"id": "grid", "severity": "error",
                        "message": "CSS Grid não suportado no Outlook Classic.",
                        "clients": ["outlook_classic"]})

    # width em % nas tabelas (prefer px)
    if has(r'<table[^>]+width\s*=\s*["\']?\d+%', re.IGNORECASE):
        checks.append({"id": "table_percent_width", "severity": "info",
                        "message": "Tabelas com width em % podem renderizar incorretamente no Outlook Classic — prefira px.",
                        "clients": ["outlook_classic"]})

    # Imagens sem alt
    img_total = count(r'<img\s', re.IGNORECASE)
    img_alt = count(r'<img[^>]+alt\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
    if img_total > 0 and img_alt < img_total:
        missing = img_total - img_alt
        checks.append({"id": "img_no_alt", "severity": "warning",
                        "message": f"{missing} imagem(ns) sem atributo alt= — problemático quando imagens são bloqueadas.",
                        "clients": ["outlook_classic", "gmail", "yahoo", "apple_mail", "outlook_web"]})

    # Imagens sem width/height
    img_no_size = count(r'<img(?![^>]*(width|height))[^>]*>', re.IGNORECASE)
    if img_no_size > 0:
        checks.append({"id": "img_no_size", "severity": "warning",
                        "message": f"{img_no_size} imagem(ns) sem width/height — pode causar layout quebrado no Outlook.",
                        "clients": ["outlook_classic"]})

    # max-width (responsividade)
    if not has(r'max-width\s*:', re.IGNORECASE):
        checks.append({"id": "no_max_width", "severity": "warning",
                        "message": "Nenhum max-width definido — e-mail pode não ser responsivo em mobile.",
                        "clients": ["gmail", "apple_mail", "outlook_web"]})

    # media queries
    if not has(r'@media\s', re.IGNORECASE):
        checks.append({"id": "no_media_query", "severity": "info",
                        "message": "Sem @media queries — responsividade mobile limitada.",
                        "clients": ["gmail", "apple_mail", "outlook_web"]})

    # Font-face
    if has(r'@font-face', re.IGNORECASE):
        checks.append({"id": "font_face", "severity": "warning",
                        "message": "@font-face ignorado no Gmail e Outlook — defina fallbacks com fontes seguras.",
                        "clients": ["gmail", "outlook_classic", "outlook_web"]})

    # viewport meta
    if not has(r'<meta[^>]+viewport', re.IGNORECASE):
        checks.append({"id": "no_viewport", "severity": "info",
                        "message": "Meta viewport ausente — afeta renderização mobile em alguns clients.",
                        "clients": ["apple_mail", "outlook_web"]})

    # JavaScript
    if has(r'<script[\s>]', re.IGNORECASE):
        checks.append({"id": "javascript", "severity": "error",
                        "message": "JavaScript é bloqueado por todos os mail clients.",
                        "clients": ["outlook_classic", "gmail", "yahoo", "apple_mail", "outlook_web"]})

    # Forms
    if has(r'<form[\s>]', re.IGNORECASE):
        checks.append({"id": "form", "severity": "error",
                        "message": "<form> bloqueado por Gmail, Yahoo e Outlook.",
                        "clients": ["gmail", "yahoo", "outlook_classic", "outlook_web"]})

    # iframes
    if has(r'<iframe[\s>]', re.IGNORECASE):
        checks.append({"id": "iframe", "severity": "error",
                        "message": "<iframe> bloqueado por todos os mail clients.",
                        "clients": ["outlook_classic", "gmail", "yahoo", "apple_mail", "outlook_web"]})

    # Largura total > 650px
    widths = re.findall(r'width\s*[=:]\s*["\']?(\d+)(?:px)?["\']?', soup_text, re.IGNORECASE)
    max_w = max((int(w) for w in widths if int(w) < 2000), default=0)
    if max_w > 650:
        checks.append({"id": "width_too_large", "severity": "warning",
                        "message": f"Largura máxima detectada: {max_w}px — recomendado ≤ 600–650px para compatibilidade.",
                        "clients": ["outlook_classic", "gmail", "outlook_web"]})

    # ── Scoring ─────────────────────────────────────────────────────────────

    CLIENTS = {
        "outlook_classic": {"name": "Outlook Classic", "icon": "🪟", "weight": {"error": 15, "warning": 5, "info": 2}},
        "gmail":           {"name": "Gmail",            "icon": "📧", "weight": {"error": 12, "warning": 4, "info": 1}},
        "yahoo":           {"name": "Yahoo Mail",       "icon": "💜", "weight": {"error": 12, "warning": 4, "info": 1}},
        "apple_mail":      {"name": "Apple Mail",       "icon": "🍎", "weight": {"error": 8,  "warning": 3, "info": 1}},
        "outlook_web":     {"name": "Outlook Web (OWA)","icon": "🌐", "weight": {"error": 10, "warning": 4, "info": 1}},
    }

    scores = {}
    for client_id, meta in CLIENTS.items():
        deductions = 0
        for c in checks:
            if client_id in c["clients"]:
                deductions += meta["weight"][c["severity"]]
        score = max(0, 100 - deductions)
        scores[client_id] = {"name": meta["name"], "icon": meta["icon"], "score": score}

    return {"scores": scores, "issues": checks}


@app.route("/analyze", methods=["POST"])
def analyze():
    zip_file = request.files.get("zip_file")
    if not zip_file or not zip_file.filename.lower().endswith(".zip"):
        return jsonify({"error": "Envie um arquivo .zip válido."}), 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        zip_path = os.path.join(job_dir, "upload.zip")
        zip_file.save(zip_path)
        base_dir = extract_zip(zip_path, job_dir)
        html_file = find_html_file(base_dir)

        if not html_file:
            return jsonify({"error": "Nenhum arquivo HTML encontrado no ZIP."}), 400

        with open(html_file, "r", encoding="utf-8-sig") as f:
            html_content = f.read()

        result = analyze_html(html_content)
        return jsonify(result)
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.run(debug=True, host="0.0.0.0", port=5000)
