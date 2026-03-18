import os
import re
import uuid
import shutil
import zipfile
import logging
import mimetypes
import smtplib
import secrets
from functools import wraps
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from flask import Flask, request, render_template, flash, redirect, url_for, session
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
app = Flask(__name__)

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    raise RuntimeError(
        "SECRET_KEY não definida. "
        "Gere com: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
app.secret_key = _secret_key

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/email_sender_uploads")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Limites de upload
MAX_ZIP_UPLOAD_BYTES = 5 * 1024 * 1024    # 5 MB comprimido
MAX_ZIP_EXTRACT_BYTES = 10 * 1024 * 1024  # 10 MB descomprimido
MAX_ZIP_FILES = 200
app.config["MAX_CONTENT_LENGTH"] = MAX_ZIP_UPLOAD_BYTES

ZIP_MAGIC = b"PK\x03\x04"

ALLOWED_EXTENSIONS = {
    ".html", ".htm", ".css", ".js",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mask(value: str) -> str:
    """Mascara um valor sensível para logs."""
    if not value or len(value) < 4:
        return "***"
    return value[:2] + "***" + value[-1]


def _is_valid_zip(file_storage) -> bool:
    """Valida magic bytes do ZIP antes de salvar."""
    header = file_storage.read(4)
    file_storage.seek(0)
    return header == ZIP_MAGIC


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ADMIN_PASSWORD:
        # Sem senha configurada: acesso livre (dev local sem restrição)
        session["authenticated"] = True
        return redirect(url_for("index"))

    if request.method == "POST":
        senha = request.form.get("password", "")
        if secrets.compare_digest(senha, ADMIN_PASSWORD):
            session["authenticated"] = True
            session.permanent = True
            logger.info("login_success ip=%s", request.remote_addr)
            return redirect(url_for("index"))
        logger.warning("login_failed ip=%s", request.remote_addr)
        flash("Senha incorreta.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# ZIP helpers
# ---------------------------------------------------------------------------
def extract_zip(zip_path: str, extract_to: str) -> str:
    """Extrai ZIP com proteção contra path traversal e zip bombs."""
    with zipfile.ZipFile(zip_path, "r") as z:
        members = z.infolist()

        if len(members) > MAX_ZIP_FILES:
            raise ValueError(f"ZIP contém mais de {MAX_ZIP_FILES} arquivos.")

        total_size = sum(m.file_size for m in members)
        if total_size > MAX_ZIP_EXTRACT_BYTES:
            raise ValueError("ZIP excede o limite de tamanho descomprimido (10 MB).")

        base_real = os.path.realpath(extract_to)

        for member in members:
            # Bloquear path traversal
            member_path = os.path.realpath(os.path.join(extract_to, member.filename))
            if not member_path.startswith(base_real + os.sep) and member_path != base_real:
                logger.warning("zip_traversal_blocked filename=%s", member.filename)
                raise ValueError(f"Path inválido no ZIP: {member.filename}")

            # Ignorar extensões não permitidas (exceto diretórios)
            if not member.filename.endswith("/"):
                ext = os.path.splitext(member.filename)[1].lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue

            z.extract(member, extract_to)

    items = os.listdir(extract_to)
    if len(items) == 1 and os.path.isdir(os.path.join(extract_to, items[0])):
        return os.path.join(extract_to, items[0])
    return extract_to


def find_html_file(base_dir: str):
    """Encontra o arquivo HTML principal no diretório extraído."""
    for name in ("index.html", "email.html"):
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            return path

    for root, _, files in os.walk(base_dir):
        for f in files:
            if f.lower().endswith(".html"):
                return os.path.join(root, f)
    return None


def inline_images(html_content: str, base_dir: str):
    """Substitui referências locais de imagem por CID, com proteção contra symlinks."""
    images = []
    base_real = os.path.realpath(base_dir)
    src_pattern = re.compile(r'(src\s*=\s*["\'])([^"\']+)(["\'])', re.IGNORECASE)

    def replacer(match):
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)

        if src.startswith(("http://", "https://", "cid:", "data:")):
            return match.group(0)

        img_path = os.path.realpath(os.path.join(base_dir, src))

        # Bloquear path traversal e symlinks
        if not img_path.startswith(base_real + os.sep):
            return match.group(0)
        if os.path.islink(img_path):
            return match.group(0)
        if not os.path.isfile(img_path):
            return match.group(0)

        mime_type, _ = mimetypes.guess_type(img_path)
        if not mime_type or not mime_type.startswith("image/"):
            return match.group(0)

        cid = uuid.uuid4().hex
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


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------
def send_email(smtp_host, smtp_port, smtp_user, smtp_pass, use_tls,
               from_email, to_email, subject, html_content, image_parts):
    msg = MIMEMultipart("related")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(html_content, "html", "utf-8"))
    for img in image_parts:
        msg.attach(img)

    server = smtplib.SMTP(smtp_host, smtp_port)
    if use_tls:
        server.starttls()
    if smtp_user and smtp_pass:
        server.login(smtp_user, smtp_pass)

    server.sendmail(from_email, to_email, msg.as_string())
    server.quit()


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        smtp_host = request.form.get("smtp_host", "").strip()
        smtp_port = int(request.form.get("smtp_port", 587))
        smtp_user = request.form.get("smtp_user", "").strip()
        smtp_pass = request.form.get("smtp_pass", "").strip()
        use_tls   = request.form.get("use_tls") == "on"
        from_email = request.form.get("from_email", "").strip()
        to_email   = request.form.get("to_email", "").strip()
        subject    = request.form.get("subject", "").strip()

        if not smtp_host or not from_email or not to_email:
            flash("Preencha todos os campos obrigatórios.", "error")
            return redirect(url_for("index"))

        zip_file = request.files.get("zip_file")
        if not zip_file or not zip_file.filename.lower().endswith(".zip"):
            flash("Envie um arquivo .zip válido.", "error")
            return redirect(url_for("index"))

        if not _is_valid_zip(zip_file):
            flash("O arquivo enviado não é um ZIP válido.", "error")
            return redirect(url_for("index"))

        job_id  = uuid.uuid4().hex
        job_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        zip_path = os.path.join(job_dir, "upload.zip")
        zip_file.save(zip_path)

        try:
            base_dir  = extract_zip(zip_path, job_dir)
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

            logger.info("email_sent to=%s subject_len=%d", _mask(to_email), len(subject))
            flash("E-mail enviado com sucesso!", "success")

        except smtplib.SMTPAuthenticationError:
            logger.error("smtp_auth_failed user=%s", _mask(smtp_user))
            flash("Falha de autenticação SMTP. Verifique usuário e senha.", "error")
        except smtplib.SMTPConnectError as e:
            logger.error("smtp_connect_error: %s", e)
            flash("Não foi possível conectar ao servidor SMTP.", "error")
        except smtplib.SMTPException as e:
            logger.error("smtp_error: %s", e)
            flash("Erro ao enviar o e-mail via SMTP.", "error")
        except ValueError as e:
            logger.warning("validation_error: %s", e)
            flash(str(e), "error")
        except Exception:
            logger.exception("unexpected_error")
            flash("Erro interno. Consulte os logs.", "error")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        return redirect(url_for("index"))

    return render_template("index.html")


if __name__ == "__main__":
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true",
        host="127.0.0.1",
        port=5000,
    )
