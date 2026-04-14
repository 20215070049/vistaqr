from app import app, db, User
from werkzeug.security import generate_password_hash

# Flask uygulama bağlamında çalış
with app.app_context():
    users = User.query.all()
    for u in users:
        # Şifre zaten hash'lenmişse geç
        if not u.password.startswith(('pbkdf2:', 'scrypt:')):
            eski_sifre = u.password
            u.password = generate_password_hash(u.password)
            print(f"✅ {u.email} kullanıcısının şifresi hash'lendi ({eski_sifre} -> {u.password[:25]}...)")

    db.session.commit()
    print("\n🎉 Tüm düz şifreler güvenli hale getirildi!")
