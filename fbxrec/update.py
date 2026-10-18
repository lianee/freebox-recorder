"""Web-triggered maintenance: latest GitHub release check, install.sh --update run as root
through sudo in its own transient unit (it outlives the fbxrec restart it causes), and the
VM reboot Debian asks for after a kernel update."""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from . import REPO, __version__

APP_DIR = "/opt/fbxrec"
UNIT = "fbxrec-update"
run = subprocess.run   # replaced in tests
# touched by unattended-upgrades' kernel hook (/etc/kernel/postinst.d), gone after a reboot
REBOOT_FLAG = "/var/run/reboot-required"


def parse_version(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        raise ValueError(f"unexpected version tag {tag!r}")
    return tuple(int(x) for x in m.groups())


def latest(repo=None, opener=urllib.request.urlopen):
    """{"current", "latest", "newer"}; latest None when nothing is published yet."""
    repo = repo or os.environ.get("FBXREC_REPO", REPO)
    out = {"current": __version__, "latest": None, "newer": False}
    try:
        with opener(f"https://api.github.com/repos/{repo}/releases/latest", timeout=15) as r:
            tag = json.load(r)["tag_name"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return out   # no release yet
        raise
    out["latest"] = tag
    out["newer"] = parse_version(tag) > parse_version(__version__)
    return out


def allowed(*cmd):
    """True when the service user may run cmd (default: the installer) as root without a password."""
    try:
        return run(["sudo", "-n", "-l", *(cmd or [f"{APP_DIR}/install.sh"])],
                   capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start(log_path):
    """Starts the update; raises RuntimeError with the reason when it cannot start."""
    cmd = ["sudo", "-n", "systemd-run", f"--unit={UNIT}", "--collect", "--quiet"]
    for k in ("FBXREC_REPO", "FBXREC_TARBALL", "FBXREC_VERSION"):
        if os.environ.get(k):
            cmd.append(f"--setenv={k}={os.environ[k]}")
    cmd += ["/bin/sh", "-c", f'{APP_DIR}/install.sh --update > "$0" 2>&1', log_path]
    r = run(cmd, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "already" in err or "exists" in err:
            raise RuntimeError("une mise à jour est déjà en cours")
        raise RuntimeError(err or f"systemd-run a échoué ({r.returncode})")


def log_tail(log_path, n=15):
    try:
        with open(log_path, errors="replace") as f:
            return f.readlines()[-n:]
    except FileNotFoundError:
        return []


def reboot_required():
    """None when no reboot is pending, else the packages that asked for it."""
    if not os.path.exists(REBOOT_FLAG):
        return None
    try:
        with open(REBOOT_FLAG + ".pkgs") as f:
            return [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        return []


def reboot():
    r = run(["sudo", "-n", "systemctl", "reboot"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"systemctl reboot a échoué ({r.returncode})")
