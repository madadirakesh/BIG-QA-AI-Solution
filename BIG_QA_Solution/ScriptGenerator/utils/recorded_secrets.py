"""
recorded_secrets.py
-------------------
Keep the passwords typed during a recording, so the script can replay them.

A recorded script never holds a password: it reads it from an environment
variable (PERF_RECORDED_PASSWORD, then PERF_RECORDED_PASSWORD_2, ... for every
further distinct password the journey typed). Nothing used to set those
variables, so every replay typed an empty password and the journey failed at
its login. The values are now stored per script at record time and handed to
the run as environment variables by performance_runner.

Storage: <perf project>/.bigqa/recorded_secrets.json, keyed by script stem,
each value encrypted with the app master key (utils/crypto_util), so the file
is useless without this application. The folder carries its own `.gitignore`,
so it is never committed. A variable already set in the app's environment wins
over the stored value, which keeps a hand-set password (or a CI secret) in
charge.
"""

import json
import os
from pathlib import Path

from utils.crypto_util import decrypt_for_app, encrypt_for_app

PASSWORD_ENV_VAR = "PERF_RECORDED_PASSWORD"
STORE_DIRNAME = ".bigqa"
STORE_FILENAME = "recorded_secrets.json"


def password_env_var(index):
    """Environment variable for the recording's `index`-th distinct password (0-based)."""
    return PASSWORD_ENV_VAR if index == 0 else f"{PASSWORD_ENV_VAR}_{index + 1}"


def _store_path(perf_dir):
    return os.path.join(perf_dir, STORE_DIRNAME, STORE_FILENAME)


def _key(script_file):
    return Path(os.path.basename(script_file or "")).stem


def _load(perf_dir):
    try:
        with open(_store_path(perf_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(perf_dir, data):
    folder = os.path.join(perf_dir, STORE_DIRNAME)
    os.makedirs(folder, exist_ok=True)
    ignore = os.path.join(folder, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Created by BIG QA - local, encrypted recording secrets. Never commit.\n*\n")
    with open(_store_path(perf_dir), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)


def store(perf_dir, script_file, secrets):
    """Remember the passwords (in replay order) a recorded script types."""
    key = _key(script_file)
    values = [s for s in (secrets or []) if s]
    if not key:
        return
    data = _load(perf_dir)
    if values:
        data[key] = [encrypt_for_app(value) for value in values]
    elif key not in data:
        return
    else:
        data.pop(key)
    _save(perf_dir, data)


def forget(perf_dir, script_file):
    """Drop a deleted script's passwords."""
    store(perf_dir, script_file, [])


def has_secrets(perf_dir, script_file):
    return bool(_load(perf_dir).get(_key(script_file)))


def run_env(perf_dir, script_file):
    """
    {variable: password} for a run of `script_file`. Variables already set in
    this process's environment are left out, so they keep precedence.
    """
    env = {}
    for index, value in enumerate(_load(perf_dir).get(_key(script_file)) or []):
        name = password_env_var(index)
        if os.environ.get(name):
            continue
        plain = decrypt_for_app(value)
        if plain and plain != value:
            env[name] = plain
    return env
