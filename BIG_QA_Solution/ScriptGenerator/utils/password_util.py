"""Portal login password hashing (storage only; transit is HTTPS's job)."""

from werkzeug.security import generate_password_hash, check_password_hash

# Prefixes werkzeug puts on its hashes; used to tell a hash from legacy plaintext.
_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def is_hashed(value):
    return isinstance(value, str) and value.startswith(_HASH_PREFIXES) and "$" in value


def hash_password(plain):
    return generate_password_hash(plain)


def verify_password(stored, provided):
    # Accepts both hashed rows and legacy plaintext rows.
    if not stored or provided is None:
        return False
    if is_hashed(stored):
        try:
            return check_password_hash(stored, provided)
        except Exception:
            return False
    return stored == provided
