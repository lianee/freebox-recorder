"""Optional web password (HTTP Basic). No password file = no auth (LAN only);
an empty or malformed file refuses everything.

The file holds one line "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>". It is reread
when it changes, so setting or removing the password never needs a restart (a restart
would cut a running recording). Set it with: sudo python3 -m fbxrec.auth
"""
import base64
import binascii
import getpass
import hashlib
import hmac
import os
import re
import sys
import threading

ITERATIONS = 200_000


def make_hash(password, salt=None, iterations=ITERATIONS):
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def check(password, stored):
    algo, it, salt, h = stored.split("$")
    if algo != "pbkdf2_sha256":
        raise ValueError(f"unknown hash algorithm {algo!r}")
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
    return hmac.compare_digest(got.hex(), h)


class Guard:
    """Checks Authorization headers against the password file."""

    def __init__(self, path):
        self.path = path
        self._stamp, self._hash = None, None
        self._good = set()            # sha256 of headers already verified for the current hash
        self._slow = threading.Lock()  # one PBKDF2 at a time: guessing cannot eat every core

    def _load(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._stamp, self._hash, self._good = None, None, set()
            return None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp != self._stamp:
            with open(self.path) as f:
                line = f.read().strip()
            if not re.fullmatch(r"pbkdf2_sha256\$\d+\$[0-9a-f]+\$[0-9a-f]{64}", line):
                # fail closed and loudly: a broken file must never mean "no password"
                raise ValueError(f"malformed password file {self.path}")
            self._stamp, self._hash, self._good = stamp, line, set()
        return self._hash

    def enabled(self):
        return self._load() is not None

    def allows(self, header):
        stored = self._load()
        if stored is None:
            return True
        if not header or not header.startswith("Basic "):
            return False
        key = hashlib.sha256(header.encode()).hexdigest()
        if key in self._good:
            return True
        try:
            user_pw = base64.b64decode(header[6:].strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return False
        pw = user_pw.split(":", 1)[1] if ":" in user_pw else ""
        with self._slow:
            ok = check(pw, stored)
        if ok:
            self._good.add(key)
        return ok


def check_new_password(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise ValueError("8 caractères minimum")
    if len(pw) > 200:
        raise ValueError("200 caractères maximum")


def write_password(path, pw):
    check_new_password(pw)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(make_hash(pw) + "\n")
    st = os.stat(os.path.dirname(path))
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)   # set by root: readable by the service user
    os.replace(tmp, path)


def remove_password(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def ask_password(prompt=getpass.getpass, out=print):
    """Asks until two matching entries of 8+ characters; empty entry = cancel (None)."""
    while True:
        pw = prompt("Nouveau mot de passe web (vide pour annuler) : ")
        if not pw:
            return None
        try:
            check_new_password(pw)
        except ValueError as e:
            out(f"refusé : {e}, recommencez")
            continue
        if prompt("Confirmation : ") != pw:
            out("les deux saisies diffèrent, recommencez")
            continue
        return pw


def main():
    path = os.environ.get("FBXREC_PASSWORD_FILE",
                          os.path.join(os.environ.get("FBXREC_DATA", "/var/lib/fbxrec"), "password"))
    if sys.argv[1:] == ["--remove"]:
        remove_password(path)
        print("mot de passe supprimé, l'interface est ouverte à tout le réseau local")
        return
    pw = ask_password()
    if pw is None:
        sys.exit("aucun mot de passe défini (plus tard : sudo fbxrec-password)")
    write_password(path, pw)
    print("mot de passe enregistré (identifiant : n'importe lequel), actif immédiatement")


if __name__ == "__main__":
    main()
