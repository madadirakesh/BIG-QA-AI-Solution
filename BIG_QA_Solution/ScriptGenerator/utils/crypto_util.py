"""
Credential encryption for generated automation projects.

WHY THIS EXISTS
---------------
The bootstrapper writes the app-under-test login PASSWORD into each generated project's .env
file. We want it encrypted *at rest* rather than sitting there in plaintext, but the generated
test project must be able to use the real password at run time WITHOUT this Flask app present
(e.g. run from an IDE, a terminal, or CI). So the generated project decrypts the value itself.

DESIGN — per-project key, AES-256-GCM, self-decrypting projects
---------------------------------------------------------------
  - At scaffold time the bootstrapper generates a random 256-bit key PER project and writes it
    into that project's .env as ``CRED_KEY=<base64>``. The password is stored next to it as
    ``PASSWORD=ENC:<token>``.
  - Every language template (Java/C#/TypeScript/Python) decrypts ``ENC:`` values at run time
    using CRED_KEY from its own .env — natively, with no third-party crypto library:
        Java   -> javax.crypto (JDK)         C#     -> System.Security.Cryptography.AesGcm (BCL)
        Node   -> crypto (built-in)          Python -> cryptography (already a dependency)

  AES-256-GCM was chosen specifically because it is available out-of-the-box in all four
  ecosystems, so no template gains a new dependency just to decrypt (Python aside, which already
  ships cryptography for other reasons).

TOKEN FORMAT (must stay identical across every language implementation)
-----------------------------------------------------------------------
  value  = "ENC:" + base64( nonce(12 bytes) || ciphertext || gcm_tag(16 bytes) )
  key    = base64( 32 raw bytes )            # stored in .env as CRED_KEY
Python's cryptography AESGCM.encrypt() returns ciphertext-with-the-tag-appended, which is the
exact layout Java's AES/GCM/NoPadding doFinal() and the others expect, so the formats line up.

SECURITY NOTE (intentional trade-off)
-------------------------------------
Because the key lives in the same project as the ciphertext (so the project can self-decrypt
anywhere), this is obfuscation of the at-rest value rather than protection against an attacker
who already has the project files — it stops casual viewing / accidental sharing / a value
showing up plainly in a screenshot or log, and .env is git-ignored so neither key nor ciphertext
is committed. This is the explicit trade-off for "the generated project must run standalone".
"""

import os
import base64
import logging
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("CredCrypto")

# Secrets in a generated .env are tagged with this prefix so a reader can tell an encrypted value
# apart from legacy plaintext and from non-secret keys (APP_URL, USER, CRED_KEY, ...).
ENC_PREFIX = "ENC:"

_NONCE_BYTES = 12  # standard/recommended GCM nonce size


def generate_key():
    """Return a fresh base64-encoded 256-bit key, suitable for storing in .env as CRED_KEY."""
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def encrypt_secret(plaintext, key_b64):
    """
    Encrypt ``plaintext`` to an ``ENC:<token>`` string using the project key ``key_b64``.

    Empty/None is returned unchanged so a blank field never becomes a bogus "ENC:" value. The
    nonce is random per call, so encrypting the same password twice yields different tokens (a
    deliberate GCM property), all of which decrypt back to the same plaintext.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    key = base64.b64decode(key_b64)
    nonce = os.urandom(_NONCE_BYTES)
    # AESGCM.encrypt returns ciphertext with the 16-byte auth tag appended.
    ct_and_tag = AESGCM(key).encrypt(nonce, str(plaintext).encode("utf-8"), None)
    return ENC_PREFIX + base64.b64encode(nonce + ct_and_tag).decode()


def decrypt_secret(value, key_b64):
    """
    Inverse of encrypt_secret(). Values without the ENC: prefix are returned unchanged, so this
    is safe to call on any .env value. Mirrors exactly what each language template does at run
    time — kept here for the app's own use and for the interop tests.
    """
    if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
        return value
    key = base64.b64decode(key_b64)
    raw = base64.b64decode(value[len(ENC_PREFIX):])
    nonce, ct_and_tag = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct_and_tag, None).decode("utf-8")


# ── App-level (database) encryption ────────────────────────────────────────────────────────────
# The functions above use a PER-PROJECT key that travels in the generated project's .env so the
# project can self-decrypt. The app ALSO keeps a copy of the password in its own database
# (ProjectData.password). That copy is read only by this app, never by a generated project, so it
# is encrypted with a single APP MASTER KEY held in the Flask app's own .env (CRED_ENC_KEY) and
# never shipped anywhere. That separation makes the database copy genuinely protected: a DB dump
# (or the in-app DB viewer) shows ciphertext, and the key is not in the database.
_APP_KEY_VAR = "CRED_ENC_KEY"
# crypto_util.py is at ScriptGenerator/utils/, so the Flask .env is one directory up from BASE.
_FLASK_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _persist_app_key(key_str):
    """Append CRED_ENC_KEY to the Flask app's .env (once) so the master key survives restarts."""
    try:
        existing = _FLASK_ENV_PATH.read_text(encoding="utf-8") if _FLASK_ENV_PATH.exists() else ""
        if _APP_KEY_VAR not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            with open(_FLASK_ENV_PATH, "a", encoding="utf-8") as f:
                f.write(f"{sep}{_APP_KEY_VAR}={key_str}\n")
    except Exception as e:
        logger.error(f"Could not persist {_APP_KEY_VAR} to {_FLASK_ENV_PATH}: {e}")


def _get_app_key():
    """Return the app master key, minting + persisting one on first use (zero manual setup)."""
    key = os.environ.get(_APP_KEY_VAR)
    if not key:
        key = generate_key()
        os.environ[_APP_KEY_VAR] = key
        _persist_app_key(key)
        logger.info("Generated a new CRED_ENC_KEY (app master key) for database encryption.")
    return key


def encrypt_for_app(plaintext):
    """
    Encrypt a secret for storage in THIS app's database, using the app master key. Empty/None
    passes through unchanged so a blank password never becomes a bogus "ENC:" value.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    return encrypt_secret(plaintext, _get_app_key())


def decrypt_for_app(value):
    """
    Inverse of encrypt_for_app(), with backward compatibility: values WITHOUT the ENC: prefix are
    assumed to be legacy plaintext passwords written before encryption existed and are returned
    as-is; ENC: values are decrypted with the app master key. So old rows keep working untouched
    and only get encrypted the next time they are saved.
    """
    if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
        return value
    try:
        return decrypt_secret(value, _get_app_key())
    except Exception as e:
        # Wrong/rotated key or corrupt value — log and return the stored value rather than 500,
        # so the config screen still loads (the password field will just show the ciphertext).
        logger.error(f"Failed to decrypt app secret from database: {e}")
        return value
