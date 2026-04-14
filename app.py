from flask import Flask, render_template, request, redirect, url_for, session, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from functools import wraps
from email.message import EmailMessage
import smtplib
import ssl
import qrcode
import os
import zipfile
import io
import uuid
import re
from datetime import timedelta, datetime

# ------------------- AYARLAR -------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "gizli123"

DB_URI = os.environ.get("DATABASE_URL", "sqlite:///vistaqr.db")
if DB_URI and DB_URI.startswith("postgres://"):
    DB_URI = DB_URI.replace("postgres://", "postgresql://", 1)

QR_BASE_URL = os.environ.get("QR_BASE_URL", "https://www.vistaqrapp.com/activate/")
QR_OUTPUT_FOLDER = os.path.join(BASE_DIR, "static", "qr_codes")
SECRET_KEY = os.environ.get("SECRET_KEY", "vistaqr-secret-key-2026")
MAX_QR_BATCH = 500

# Admin route gizli
ADMIN_LOGIN_ROUTE = "/vista-secret-panel-6334"
ADMIN_PANEL_ROUTE = "/vista-secret-panel-6334/panel"
ADMIN_USERS_ROUTE = "/vista-secret-panel-6334/users"
ADMIN_LOGOUT_ROUTE = "/vista-secret-panel-6334/logout"
ADMIN_DELETE_INACTIVE_ROUTE = "/vista-secret-panel-6334/delete-inactive"
ADMIN_DELETE_USER_ROUTE = "/vista-secret-panel-6334/delete-user/<int:user_id>"

# ------------------- EMAIL / OTP AYARLARI -------------------
# Aktivasyonda mail doğrulamayı kapalı tutuyoruz; donma yapmaması için
ENABLE_EMAIL_VERIFICATION = False
ENABLE_SMS_VERIFICATION = False
VERIFICATION_CODE_TTL_MINUTES = 10

# Brevo SMTP ayarları
SMTP_HOST = "smtp-relay.brevo.com"
SMTP_PORT = 587
SMTP_USERNAME = "a7f3ea001@smtp-brevo.com"
SMTP_PASSWORD = "xsmtpsib-6d406d5dc1b9fa81e47a690466afef315c17409ab6105056cad61790087cced9-KixReEcBHhOOFPu8"
SMTP_FROM_EMAIL = "qrvista6@gmail.com"
SMTP_FROM_NAME = "VistaQR"
SMTP_USE_TLS = True

VERIFICATION_STORE = {}

# ------------------- FLASK APP -------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=2)

db = SQLAlchemy(app)
serializer = URLSafeTimedSerializer(app.secret_key)

# ------------------- MODELLER -------------------
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, default="")
    email = db.Column(db.String(200), unique=True, nullable=False)
    phone = db.Column(db.String(50))
    password = db.Column(db.String(200), nullable=False)

    keychains = db.relationship("Keychain", backref="owner", lazy=True)


class Keychain(db.Model):
    __tablename__ = "keychains"

    id = db.Column(db.Integer, primary_key=True)
    qr_code_id = db.Column(db.String(200), unique=True, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    status = db.Column(db.String(50), nullable=False, default="inactive")
    note = db.Column(db.String(300))


# ------------------- TABLOLARI OLUSTUR -------------------
with app.app_context():
    db.create_all()


# ------------------- YARDIMCI FONKSİYONLAR -------------------
def ensure_qr_folder():
    os.makedirs(QR_OUTPUT_FOLDER, exist_ok=True)


def safe_strip(value):
    return (value or "").strip()


def normalize_email(email):
    return safe_strip(email).lower()


def normalize_phone(number: str):
    if not number:
        return None

    digits = re.sub(r"\D", "", number)
    if not digits:
        return None

    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith("0") and len(digits) == 11:
        digits = "90" + digits[1:]

    if len(digits) < 8:
        return None

    return digits


def normalize_phone_for_whatsapp(number: str):
    return normalize_phone(number)


def build_whatsapp_url(number: str):
    phone_clean = normalize_phone_for_whatsapp(number)
    if not phone_clean:
        return None
    return f"https://wa.me/{phone_clean}"


def is_valid_email(email):
    if not email:
        return False
    pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    return re.match(pattern, email) is not None


def is_valid_password(password):
    return bool(password and len(password.strip()) >= 6)


def sanitize_note(note):
    return safe_strip(note)[:300]


def delete_qr_file(qr_code_id):
    try:
        file_path = os.path.join(QR_OUTPUT_FOLDER, f"{qr_code_id}.png")
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass


def generate_unique_qr_code():
    for _ in range(20):
        code = "QR-" + str(uuid.uuid4())[:12].upper()
        existing = Keychain.query.filter_by(qr_code_id=code).first()
        if not existing:
            return code
    raise ValueError("Benzersiz QR kod üretilemedi.")


def get_lang():
    return session.get("lang", "tr")


def is_placeholder_email(email):
    return bool(email and email.endswith("@vistaqr.local"))


def build_placeholder_email(phone=None):
    phone_part = phone or "no-phone"
    unique_part = str(uuid.uuid4())[:8]
    return f"user-{phone_part}-{unique_part}@vistaqr.local"


def resolve_email_for_storage(email, phone):
    email = normalize_email(email)
    if email:
        return email
    return build_placeholder_email(phone)


def get_existing_user_by_phone(phone):
    if not phone:
        return None, None

    users = User.query.filter_by(phone=phone).all()
    if len(users) == 1:
        return users[0], None
    if len(users) > 1:
        return None, "Bu telefon numarası birden fazla hesapta kayıtlı. Lütfen e-posta ile giriş yapın veya destek alın."
    return None, None


def find_existing_user_for_activation(email, phone):
    email_user = None
    phone_user = None

    email = normalize_email(email)
    phone = normalize_phone(phone)

    if email:
        email_user = User.query.filter_by(email=email).first()

    phone_user, phone_error = get_existing_user_by_phone(phone)
    if phone_error:
        return None, phone_error

    if email_user and phone_user and email_user.id != phone_user.id:
        return None, "Girilen e-posta ve telefon farklı hesaplara ait görünüyor."

    return email_user or phone_user, None


def get_qr_status_meta(status):
    if status == "active":
        return {
            "label_tr": "Aktif",
            "label_en": "Active",
            "button_text_tr": "İnaktif Et",
            "button_text_en": "Disable",
            "button_color": "#dc2626"
        }
    return {
        "label_tr": "İnaktif",
        "label_en": "Inactive",
        "button_text_tr": "Aktif Et",
        "button_text_en": "Enable",
        "button_color": "#16a34a"
    }


# ------------------- EMAIL / OTP -------------------
def generate_verification_code():
    return str(uuid.uuid4().int)[-6:]


def store_verification_code(channel, target, purpose):
    code = generate_verification_code()
    key = f"{channel}:{purpose}:{target}"
    VERIFICATION_STORE[key] = {
        "code": code,
        "expires_at": datetime.utcnow() + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
    }
    return code


def verify_stored_code(channel, target, purpose, entered_code):
    key = f"{channel}:{purpose}:{target}"
    item = VERIFICATION_STORE.get(key)

    if not item:
        return False

    if datetime.utcnow() > item["expires_at"]:
        VERIFICATION_STORE.pop(key, None)
        return False

    if item["code"] != entered_code:
        return False

    VERIFICATION_STORE.pop(key, None)
    return True


def send_email_via_smtp(to_email, subject, body_text):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL]):
        print("SMTP AYARLARI EKSIK => mail gönderilmedi")
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg.set_content(body_text)

        if SMTP_USE_TLS:
            context = ssl.create_default_context()
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8) as server:
                server.starttls(context=context)
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=8) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

        return True
    except Exception as e:
        print("EMAIL GONDERIM HATASI =>", repr(e))
        return False


def send_email_verification_code(email, code):
    subject = "VistaQR Dogrulama Kodunuz"
    body = f"""Merhaba,

VistaQR dogrulama kodunuz: {code}

Bu kod {VERIFICATION_CODE_TTL_MINUTES} dakika boyunca gecerlidir.

VistaQR
"""
    sent = send_email_via_smtp(email, subject, body)
    if not sent:
        print(f"EMAIL DOGRULAMA KODU GONDERILEMEDI => email={email}, code={code}")
    return sent


def send_password_reset_code(email, code):
    subject = "VistaQR Sifre Sifirlama Kodu"
    body = f"""Merhaba,

Sifre sifirlama kodunuz: {code}

Bu kod {VERIFICATION_CODE_TTL_MINUTES} dakika boyunca gecerlidir.
Bu islemi siz yapmadiysaniz bu maili dikkate almayin.

VistaQR
"""
    sent = send_email_via_smtp(email, subject, body)
    if not sent:
        print(f"SIFRE RESET KODU GONDERILEMEDI => email={email}, code={code}")
    return sent


def send_sms_verification_code(phone, code):
    print(f"SMS DOGRLAMA KODU => phone={phone}, code={code}")


def trigger_optional_verifications(email, phone):
    try:
        if ENABLE_EMAIL_VERIFICATION and email and not is_placeholder_email(email):
            email_code = store_verification_code("email", email, "activation")
            send_email_verification_code(email, email_code)

        if ENABLE_SMS_VERIFICATION and phone:
            sms_code = store_verification_code("sms", phone, "activation")
            send_sms_verification_code(phone, sms_code)
    except Exception as e:
        print("OPTIONAL VERIFICATION HATASI =>", repr(e))


def require_admin(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrap


# ------------------- DİL -------------------
@app.before_request
def set_default_language():
    if "lang" not in session:
        session["lang"] = "tr"


@app.route("/set_language/<lang_code>")
def set_language(lang_code):
    if lang_code in ["tr", "en"]:
        session["lang"] = lang_code
    return redirect(request.referrer or url_for("home"))


# ------------------- SAYFALAR -------------------
@app.route("/")
def home():
    return render_template("home.html", lang=get_lang())


@app.route("/about")
def about():
    return render_template("about.html", lang=get_lang())


# ------------------- ADMIN GİRİŞ -------------------
@app.route(ADMIN_LOGIN_ROUTE, methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = safe_strip(request.form.get("username"))
        password = request.form.get("password") or ""

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.permanent = True
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))

        return render_template(
            "admin_login.html",
            error="❌ Kullanıcı adı veya parola hatalı.",
            lang=get_lang()
        )

    return render_template("admin_login.html", lang=get_lang())


@app.route(ADMIN_LOGOUT_ROUTE)
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


# ------------------- ADMIN PANEL -------------------
@app.route(ADMIN_PANEL_ROUTE, methods=["GET", "POST"])
@require_admin
def admin_panel():
    message = request.args.get("message")

    if request.method == "POST":
        adet_raw = request.form.get("adet", "0")

        try:
            adet = int(adet_raw)
        except ValueError:
            return render_template(
                "admin_panel.html",
                total_qr=Keychain.query.count(),
                active_qr=Keychain.query.filter_by(status="active").count(),
                inactive_qr=Keychain.query.filter_by(status="inactive").count(),
                message="❌ Geçerli bir adet giriniz.",
                lang=get_lang()
            )

        if adet < 1 or adet > MAX_QR_BATCH:
            return render_template(
                "admin_panel.html",
                total_qr=Keychain.query.count(),
                active_qr=Keychain.query.filter_by(status="active").count(),
                inactive_qr=Keychain.query.filter_by(status="inactive").count(),
                message=f"❌ Adet 1 ile {MAX_QR_BATCH} arasında olmalıdır.",
                lang=get_lang()
            )

        ensure_qr_folder()
        qr_list = []

        try:
            for _ in range(adet):
                code = generate_unique_qr_code()
                new_qr = Keychain(qr_code_id=code, status="inactive")
                db.session.add(new_qr)
                db.session.flush()

                qr_list.append(code)
                img = qrcode.make(f"{QR_BASE_URL}{code}")
                img.save(os.path.join(QR_OUTPUT_FOLDER, f"{code}.png"))

            db.session.commit()

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                for code in qr_list:
                    qr_path = os.path.join(QR_OUTPUT_FOLDER, f"{code}.png")
                    if os.path.exists(qr_path):
                        zipf.write(qr_path, f"{code}.png")

            buffer.seek(0)
            return send_file(buffer, as_attachment=True, download_name=f"vistaqr_{adet}.zip")

        except Exception as e:
            db.session.rollback()
            for code in qr_list:
                delete_qr_file(code)

            print("QR URETIM HATASI =>", repr(e))

            return render_template(
                "admin_panel.html",
                total_qr=Keychain.query.count(),
                active_qr=Keychain.query.filter_by(status="active").count(),
                inactive_qr=Keychain.query.filter_by(status="inactive").count(),
                message="❌ QR üretimi sırasında bir hata oluştu.",
                lang=get_lang()
            )

    total_qr = Keychain.query.count()
    active_qr = Keychain.query.filter_by(status="active").count()
    inactive_qr = Keychain.query.filter_by(status="inactive").count()

    return render_template(
        "admin_panel.html",
        total_qr=total_qr,
        active_qr=active_qr,
        inactive_qr=inactive_qr,
        message=message,
        lang=get_lang()
    )


# ------------------- İNAKTİF QR SİLME -------------------
@app.route(ADMIN_DELETE_INACTIVE_ROUTE, methods=["POST"])
@require_admin
def delete_inactive_qrs():
    password = request.form.get("admin_password") or ""
    confirm = request.form.get("confirm_delete")

    if password != ADMIN_PASSWORD:
        return redirect(url_for("admin_panel", message="❌ Şifre yanlış!"))

    if confirm != "1":
        return redirect(url_for("admin_panel", message="⚠ Silme işlemi onaylanmadı."))

    try:
        inactive_qrs = Keychain.query.filter_by(status="inactive", owner_id=None).all()
        count = len(inactive_qrs)

        for qr in inactive_qrs:
            delete_qr_file(qr.qr_code_id)
            db.session.delete(qr)

        db.session.commit()
        return redirect(url_for("admin_panel", message=f"🟢 {count} adet sahipsiz inaktif QR başarıyla silindi!"))

    except Exception as e:
        db.session.rollback()
        print("INAKTIF QR SILME HATASI =>", repr(e))
        return redirect(url_for("admin_panel", message="❌ İnaktif QR silme sırasında hata oluştu."))


# ------------------- ADMIN: KULLANICI LİSTESİ -------------------
@app.route(ADMIN_USERS_ROUTE)
@require_admin
def admin_users():
    users = User.query.order_by(User.id.desc()).all()
    return render_template("admin_users.html", users=users, lang=get_lang())


@app.route(ADMIN_DELETE_USER_ROUTE, methods=["POST"])
@require_admin
def delete_user(user_id):
    user = db.session.get(User, user_id)

    if not user:
        return redirect(url_for("admin_users"))

    try:
        user_keychains = Keychain.query.filter_by(owner_id=user_id).all()
        for keychain in user_keychains:
            db.session.delete(keychain)

        db.session.delete(user)
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        print("KULLANICI SILME HATASI =>", repr(e))

    return redirect(url_for("admin_users"))


# ------------------- ŞİFREMİ UNUTTUM -------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    lang = get_lang()

    if request.method == "POST":
        email = normalize_email(request.form.get("email"))

        if not is_valid_email(email):
            return render_template(
                "forgot_password.html",
                error="⚠ Geçerli bir e-posta giriniz.",
                lang=lang
            )

        user = User.query.filter_by(email=email).first()
        if not user or is_placeholder_email(user.email):
            return render_template(
                "forgot_password.html",
                error="⚠ Bu e-posta kayıtlı değil.",
                lang=lang
            )

        code = store_verification_code("email", email, "password_reset")
        sent = send_password_reset_code(email, code)

        if not sent:
            return render_template(
                "forgot_password.html",
                error="❌ Kod e-posta adresine gönderilemedi. Lütfen daha sonra tekrar deneyin.",
                lang=lang
            )

        session["password_reset_email"] = email
        return redirect(url_for("verify_reset_code", success="✅ Şifre sıfırlama kodu e-posta adresinize gönderildi."))

    return render_template("forgot_password.html", lang=lang)


@app.route("/verify-reset-code", methods=["GET", "POST"])
def verify_reset_code():
    lang = get_lang()
    email = session.get("password_reset_email")
    success = request.args.get("success")

    if not email:
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        code = safe_strip(request.form.get("code"))
        new_pass = request.form.get("password") or ""
        confirm_pass = request.form.get("confirm_password") or ""

        if not code:
            return render_template(
                "verify_reset_code.html",
                email=email,
                error="⚠ Lütfen doğrulama kodunu giriniz.",
                success=success,
                lang=lang
            )

        if not is_valid_password(new_pass):
            return render_template(
                "verify_reset_code.html",
                email=email,
                error="⚠ Yeni şifre en az 6 karakter olmalıdır.",
                success=success,
                lang=lang
            )

        if new_pass.strip() != confirm_pass.strip():
            return render_template(
                "verify_reset_code.html",
                email=email,
                error="⚠ Şifreler eşleşmiyor.",
                success=success,
                lang=lang
            )

        if not verify_stored_code("email", email, "password_reset", code):
            return render_template(
                "verify_reset_code.html",
                email=email,
                error="❌ Kod hatalı veya süresi dolmuş.",
                success=success,
                lang=lang
            )

        user = User.query.filter_by(email=email).first()
        if not user:
            session.pop("password_reset_email", None)
            return render_template(
                "error.html",
                message="Kullanıcı bulunamadı.",
                lang=lang
            )

        try:
            user.password = generate_password_hash(new_pass.strip())
            db.session.commit()
            session.pop("password_reset_email", None)
            return render_template("success.html", name=user.name, lang=lang)
        except Exception as e:
            db.session.rollback()
            print("OTP SIFRE RESET HATASI =>", repr(e))
            return render_template(
                "verify_reset_code.html",
                email=email,
                error="❌ Şifre güncellenirken hata oluştu.",
                success=success,
                lang=lang
            )

    return render_template("verify_reset_code.html", email=email, success=success, lang=lang)


# ------------------- USER PANEL -------------------
@app.route("/user-panel", methods=["GET", "POST"])
def user_panel():
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("user_login"))

    user = db.session.get(User, uid)
    if not user:
        session.pop("user_id", None)
        return redirect(url_for("user_login"))

    keychains = Keychain.query.filter_by(owner_id=uid).order_by(Keychain.id.desc()).all()
    keychain = keychains[0] if keychains else None

    prepared_keychains = []
    for item in keychains:
        meta = get_qr_status_meta(item.status)
        prepared_keychains.append({
            "id": item.id,
            "qr_code_id": item.qr_code_id,
            "status": item.status,
            "note": item.note,
            "label_tr": meta["label_tr"],
            "label_en": meta["label_en"],
            "button_text_tr": meta["button_text_tr"],
            "button_text_en": meta["button_text_en"],
            "button_color": meta["button_color"]
        })

    success = request.args.get("success")
    error = request.args.get("error")

    if request.method == "POST":
        name = safe_strip(request.form.get("name"))
        phone_raw = request.form.get("phone")
        email_raw = normalize_email(request.form.get("email"))
        password = request.form.get("password") or ""

        if name:
            user.name = name

        if phone_raw:
            normalized_phone = normalize_phone(phone_raw)
            if normalized_phone:
                other_users = User.query.filter(User.phone == normalized_phone, User.id != user.id).all()
                if other_users:
                    return render_template(
                        "user_panel.html",
                        user=user,
                        keychain=keychain,
                        keychains=prepared_keychains,
                        error="⚠ Bu telefon numarası başka bir hesapta kayıtlı.",
                        success=success,
                        lang=get_lang()
                    )
                user.phone = normalized_phone

        if email_raw:
            if not is_valid_email(email_raw):
                return render_template(
                    "user_panel.html",
                    user=user,
                    keychain=keychain,
                    keychains=prepared_keychains,
                    error="⚠ Geçerli bir e-posta giriniz.",
                    success=success,
                    lang=get_lang()
                )

            email_owner = User.query.filter(User.email == email_raw, User.id != user.id).first()
            if email_owner:
                return render_template(
                    "user_panel.html",
                    user=user,
                    keychain=keychain,
                    keychains=prepared_keychains,
                    error="⚠ Bu e-posta başka bir hesapta kayıtlı.",
                    success=success,
                    lang=get_lang()
                )

            user.email = email_raw

        if password:
            if not is_valid_password(password):
                return render_template(
                    "user_panel.html",
                    user=user,
                    keychain=keychain,
                    keychains=prepared_keychains,
                    error="⚠ Yeni şifre en az 6 karakter olmalıdır.",
                    success=success,
                    lang=get_lang()
                )
            user.password = generate_password_hash(password.strip())

        try:
            db.session.commit()
            return redirect(url_for("user_panel", success="✅ Bilgiler başarıyla güncellendi."))
        except Exception as e:
            db.session.rollback()
            print("USER PANEL GUNCELLEME HATASI =>", repr(e))
            return render_template(
                "user_panel.html",
                user=user,
                keychain=keychain,
                keychains=prepared_keychains,
                error="❌ Bilgiler güncellenirken hata oluştu.",
                success=success,
                lang=get_lang()
            )

    return render_template(
        "user_panel.html",
        user=user,
        keychain=keychain,
        keychains=prepared_keychains,
        success=success,
        error=error,
        lang=get_lang()
    )


@app.route("/user-panel/update-note/<int:keychain_id>", methods=["POST"])
def update_keychain_note(keychain_id):
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("user_login"))

    keychain = Keychain.query.filter_by(id=keychain_id, owner_id=uid).first()
    if not keychain:
        return redirect(url_for("user_panel", error="⚠ QR kaydı bulunamadı."))

    try:
        keychain.note = sanitize_note(request.form.get("note"))
        db.session.commit()
        return redirect(url_for("user_panel", success="✅ QR notu güncellendi."))
    except Exception as e:
        db.session.rollback()
        print("QR NOTE GUNCELLEME HATASI =>", repr(e))
        return redirect(url_for("user_panel", error="❌ QR notu güncellenemedi."))


@app.route("/user-panel/toggle-keychain/<int:keychain_id>", methods=["POST"])
def toggle_keychain_status(keychain_id):
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("user_login"))

    keychain = Keychain.query.filter_by(id=keychain_id, owner_id=uid).first()
    if not keychain:
        return redirect(url_for("user_panel", error="⚠ QR kaydı bulunamadı."))

    try:
        if keychain.status == "active":
            keychain.status = "inactive"
            message = "🟥 QR inaktif hale getirildi."
        else:
            keychain.status = "active"
            message = "🟩 QR tekrar aktif hale getirildi."

        db.session.commit()
        return redirect(url_for("user_panel", success=message))
    except Exception as e:
        db.session.rollback()
        print("QR TOGGLE HATASI =>", repr(e))
        return redirect(url_for("user_panel", error="❌ QR durumu güncellenemedi."))


# ------------------- USER LOGIN -------------------
@app.route("/login", methods=["GET", "POST"])
def user_login():
    lang = get_lang()

    if request.method == "POST":
        identifier = safe_strip(request.form.get("email"))
        password = request.form.get("password") or ""

        if not identifier or not password:
            return render_template("user_login.html", error="⚠ Lütfen tüm alanları doldurun.", lang=lang)

        user = None

        if "@" in identifier:
            user = User.query.filter_by(email=normalize_email(identifier)).first()
        else:
            normalized_phone = normalize_phone(identifier)
            if normalized_phone:
                users = User.query.filter_by(phone=normalized_phone).all()
                if len(users) == 1:
                    user = users[0]
                elif len(users) > 1:
                    return render_template("user_login.html", error="⚠ Bu telefon numarası birden fazla hesapta kayıtlı. Lütfen e-posta ile giriş yapın.", lang=lang)

        if user and user.password and check_password_hash(user.password, password):
            session.permanent = True
            session["user_id"] = user.id
            return redirect(url_for("user_panel"))

        return render_template("user_login.html", error="⚠ Geçersiz bilgiler.", lang=lang)

    return render_template("user_login.html", lang=lang)


# ------------------- QR AKTİVASYON -------------------
@app.route("/activate/<qr_code_id>", methods=["GET", "POST"])
def activate_keychain(qr_code_id):
    lang = get_lang()
    keychain = Keychain.query.filter_by(qr_code_id=qr_code_id).first()

    if not keychain:
        return render_template("error.html", message="Bu QR sistemde kayıtlı değil.", lang=lang)

    if keychain.owner_id and keychain.status == "active":
        owner = db.session.get(User, keychain.owner_id)
        if not owner:
            return render_template("error.html", message="Bu QR için kullanıcı bulunamadı.", lang=lang)

        return render_template(
            "view.html",
            owner=owner,
            note=keychain.note,
            whatsapp_url=build_whatsapp_url(owner.phone),
            lang=lang
        )

    if keychain.owner_id and keychain.status == "inactive":
        return render_template(
            "error.html",
            message="Bu QR şu anda geçici olarak inaktif durumda. Sahibi tekrar aktif edene kadar bilgi gösterilmez.",
            lang=lang
        )

    if request.method == "POST":
        name = safe_strip(request.form.get("owner_name"))
        phone = normalize_phone(request.form.get("phone"))
        email_input = normalize_email(request.form.get("email"))
        password = request.form.get("password") or ""
        password_confirm = request.form.get("password_confirm") or ""
        note = sanitize_note(request.form.get("note"))

        print("AKTIVASYON DEBUG =>", {
            "qr_code_id": qr_code_id,
            "name": name,
            "phone": phone,
            "email": email_input,
            "password_len": len(password),
            "note": note
        })

        if not name:
            return render_template("activate.html", qr_code_id=qr_code_id, error="⚠ Ad soyad zorunludur.", lang=lang)

        if not phone:
            return render_template("activate.html", qr_code_id=qr_code_id, error="⚠ Geçerli bir telefon numarası giriniz.", lang=lang)

        if email_input and not is_valid_email(email_input):
            return render_template("activate.html", qr_code_id=qr_code_id, error="⚠ Geçerli bir e-posta giriniz.", lang=lang)

        if not is_valid_password(password):
            return render_template("activate.html", qr_code_id=qr_code_id, error="⚠ Şifre en az 6 karakter olmalıdır.", lang=lang)

        if password.strip() != password_confirm.strip():
            return render_template("activate.html", qr_code_id=qr_code_id, error="⚠ Şifreler eşleşmiyor.", lang=lang)

        existing_user, existing_user_error = find_existing_user_for_activation(email_input, phone)
        if existing_user_error:
            return render_template("activate.html", qr_code_id=qr_code_id, error=f"⚠ {existing_user_error}", lang=lang)

        try:
            if existing_user:
                if not existing_user.password or not check_password_hash(existing_user.password, password):
                    return render_template(
                        "activate.html",
                        qr_code_id=qr_code_id,
                        error="⚠ Bu hesap zaten kayıtlı. Mevcut hesabın şifresini doğru girmeniz gerekir.",
                        lang=lang
                    )

                if name and not existing_user.name:
                    existing_user.name = name

                if phone and not existing_user.phone:
                    existing_user.phone = phone

                if email_input and is_valid_email(email_input) and is_placeholder_email(existing_user.email):
                    email_owner = User.query.filter(User.email == email_input, User.id != existing_user.id).first()
                    if email_owner:
                        return render_template(
                            "activate.html",
                            qr_code_id=qr_code_id,
                            error="⚠ Bu e-posta başka bir hesapta kayıtlı.",
                            lang=lang
                        )
                    existing_user.email = email_input

                keychain.owner_id = existing_user.id
                keychain.status = "active"
                keychain.note = note

                db.session.commit()
                trigger_optional_verifications(existing_user.email, existing_user.phone)

                print("AKTIVASYON BASARILI => mevcut hesap", {
                    "user_id": existing_user.id,
                    "qr_code_id": qr_code_id,
                    "owner_id": keychain.owner_id,
                    "status": keychain.status
                })

                return render_template(
                    "view.html",
                    owner=existing_user,
                    note=note,
                    whatsapp_url=build_whatsapp_url(existing_user.phone),
                    lang=lang
                )

            final_email = resolve_email_for_storage(email_input, phone)

            same_email = User.query.filter_by(email=final_email).first()
            if same_email:
                final_email = build_placeholder_email(phone)

            user = User(
                name=name,
                email=final_email,
                phone=phone,
                password=generate_password_hash(password.strip())
            )
            db.session.add(user)
            db.session.flush()

            keychain.owner_id = user.id
            keychain.status = "active"
            keychain.note = note

            db.session.commit()
            trigger_optional_verifications(user.email, user.phone)

            print("AKTIVASYON BASARILI => yeni hesap", {
                "user_id": user.id,
                "qr_code_id": qr_code_id,
                "owner_id": keychain.owner_id,
                "status": keychain.status
            })

            return render_template(
                "view.html",
                owner=user,
                note=note,
                whatsapp_url=build_whatsapp_url(user.phone),
                lang=lang
            )

        except Exception as e:
            db.session.rollback()
            print("AKTIVASYON HATASI =>", repr(e))
            return render_template(
                "activate.html",
                qr_code_id=qr_code_id,
                error=f"❌ Aktivasyon sırasında hata oluştu: {str(e)}",
                lang=lang
            )

    return render_template("activate.html", qr_code_id=qr_code_id, lang=lang)


# ------------------- QR VIEW -------------------
@app.route("/view/<qr_code_id>")
def view_keychain(qr_code_id):
    keychain = Keychain.query.filter_by(qr_code_id=qr_code_id).first()

    if not keychain:
        return render_template("error.html", message="Bu QR sistemde kayıtlı değil.", lang=get_lang())

    if keychain.status != "active":
        return render_template(
            "error.html",
            message="Bu QR şu anda inaktif durumda.",
            lang=get_lang()
        )

    owner = db.session.get(User, keychain.owner_id)
    if not owner:
        return render_template(
            "error.html",
            message="Bu QR için kullanıcı bulunamadı.",
            lang=get_lang()
        )

    return render_template(
        "view.html",
        owner=owner,
        note=keychain.note,
        whatsapp_url=build_whatsapp_url(owner.phone),
        lang=get_lang()
    )


# ------------------- TEST EMAIL -------------------
@app.route("/test-email")
def test_email():
    ok = send_email_via_smtp(
        to_email=SMTP_FROM_EMAIL,
        subject="VistaQR Test",
        body_text="Email sistemi çalışıyor."
    )
    return "Mail gonderildi" if ok else "Mail gonderilemedi"


# ------------------- USER LOGOUT -------------------
@app.route("/logout")
def user_logout():
    session.pop("user_id", None)
    return redirect(url_for("user_login"))


# ------------------- ÇALIŞTIR -------------------
if __name__ == "__main__":
    ensure_qr_folder()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)