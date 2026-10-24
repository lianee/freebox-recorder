"""Alerts by SMS through Free Mobile's own API (subscribers enable "Notifications par SMS" in
their Free Mobile account and get an identifier and a key; the SMS goes to their own number).

No app to install and nothing besides Free's servers. Credentials are POSTed as JSON (the API
accepts it, checked 2026-10-20) so they never appear in a URL, and are stored readable by the
service user only."""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("fbxrec")
API = os.environ.get("FBXREC_SMS_URL", "https://smsapi.free-mobile.fr/sendmsg")
DAILY_MAX = 10   # alerts per 24 h at most: a fault repeating every minute must not flood the phone
ERRORS = {400: "paramètre manquant", 402: "trop de SMS envoyés, réessayez plus tard",
          403: "identifiant ou clé refusés, ou option « Notifications par SMS » désactivée dans l'espace Free Mobile",
          500: "erreur du serveur Free Mobile, réessayez plus tard"}


class NotifyError(Exception):
    pass


def validate(user, key):
    if not isinstance(user, str) or not re.match(r"^\d{8}$", user):
        raise NotifyError("identifiant Free Mobile : 8 chiffres")
    if not isinstance(key, str) or not re.match(r"^[A-Za-z0-9]{6,64}$", key):
        raise NotifyError("clé : lettres et chiffres, telle qu'affichée par Free Mobile")


class Sms:
    def __init__(self, path):
        self.path, self.lock, self.sent = path, threading.Lock(), []

    def config(self):
        try:
            with open(self.path) as f:
                c = json.load(f)
        except FileNotFoundError:
            return None
        except ValueError:
            raise NotifyError("fichier de configuration SMS illisible") from None
        if not isinstance(c, dict) or not isinstance(c.get("user"), str) or not isinstance(c.get("key"), str):
            raise NotifyError("fichier de configuration SMS illisible")
        return c

    def save(self, user, key):
        validate(user, key)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"user": user, "key": key}, f)
        os.replace(tmp, self.path)

    def remove(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def send(self, msg, cfg=None):
        """Send now; raises NotifyError with a message for the page."""
        cfg = cfg or self.config()
        if not cfg:
            raise NotifyError("SMS non configuré")
        body = json.dumps({"user": cfg["user"], "pass": cfg["key"], "msg": msg[:900]}).encode()
        req = urllib.request.Request(API, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                return
        except urllib.error.HTTPError as e:
            raise NotifyError(ERRORS.get(e.code, f"réponse {e.code} de Free Mobile")) from None
        except OSError as e:
            raise NotifyError(f"Free Mobile injoignable ({e})") from None

    def alert(self, msg):
        """Best effort, in the background, capped per day; silently off when not configured."""
        with self.lock:
            now = time.time()
            self.sent = [t for t in self.sent if now - t < 86400]
            if len(self.sent) >= DAILY_MAX:
                log.warning("SMS alert dropped (%d in 24 h): %s", DAILY_MAX, msg)
                return
            self.sent.append(now)

        def run():
            try:
                if self.config():
                    self.send(msg)
                    log.info("SMS alert sent: %s", msg)
            except NotifyError as e:
                log.warning("SMS alert failed (%s): %s", e, msg)
        threading.Thread(target=run, daemon=True).start()
