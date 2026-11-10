from app.db import connect
from app.security import hash_password

h = hash_password("admin123")
with connect() as conn:
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE username = %s", (h, "admin"))
    conn.commit()
print("SUCCESS: admin password updated to admin123")
