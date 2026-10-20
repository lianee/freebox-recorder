#!/bin/bash
# fbxrec: install or update the TV recorder on a Debian VM of a Freebox Delta or Ultra.
#
#   sudo ./install.sh                 install/upgrade from this directory (release tarball or checkout)
#   curl -fsSL <raw url>/install.sh | sudo bash
#                                     download the latest release, then install it
#   sudo fbxrec-update                same as above once installed (alias of install.sh --update)
#
# Never restarts the service while a recording runs (a restart cuts it): refuses instead,
# FORCE=1 overrides. Refuses as well if the test suite fails on this machine.
#
# Environment (all optional):
#   FBXREC_OUT      output folder; default "<Freebox disk>/Enregistrements TV", disk detected
#   FBXREC_SHARE    Freebox disk share name to mount when none is mounted (Ultra: "interne", Delta: "Freebox")
#   FBXREC_USER     service user; default the owner of the Freebox disk mount, else "freebox"
#   FBXREC_REPO     GitHub "owner/name" releases come from
#   FBXREC_TARBALL  URL or local path of a .tar.gz to install instead of the latest release
#   FBXREC_VERSION  release tag to install instead of the latest
#   FBXREC_BRANCH   branch to install instead of a release (test installs, e.g. master)
#   FORCE=1         install even during a recording
set -euo pipefail

REPO=${FBXREC_REPO:-lianee/freebox-recorder}
FBX_HOST=mafreebox.freebox.fr
SHARE_CANDIDATES=("interne" "Freebox" "Disque dur")   # Ultra: interne, Delta: Freebox (seen in its default cloud-init 2026-10-20)
ENV_FILE=/etc/fbxrec.env
APP_DIR=/opt/fbxrec
DATA_DIR=/var/lib/fbxrec
OUT_SUBDIR="Enregistrements TV"

# elapsed time on every message; exported so the re-run after a download keeps counting
FBXREC_T0=${FBXREC_T0:-$(date +%s)}
export FBXREC_T0
elapsed() { local e=$(( $(date +%s) - FBXREC_T0 )); printf '%d:%02d' $((e / 60)) $((e % 60)); }
say() { echo "fbxrec [$(elapsed)]: $*" >&2; }
die() { say "ERREUR : $*"; exit 1; }

# ask PROMPT: reads one answer from the terminal, even under "curl | bash" (stdin is the script)
ask() {
  local a
  { exec 3</dev/tty; } 2>/dev/null || return 1
  read -r -u 3 -p "$1" a || { exec 3<&-; return 1; }
  exec 3<&-
  printf '%s' "$a"
}

# freebox_mounts: mount points of CIFS shares served by the Freebox, one per line
freebox_mounts() {
  local src target
  findmnt -t cifs -n -r -o SOURCE,TARGET | while read -r src target; do
    src=$(printf '%b' "$src"); target=$(printf '%b' "$target")   # -r escapes spaces as \x20
    case "${src,,}" in "//$FBX_HOST/"*) printf '%s\n' "$target" ;; esac
  done
}

# mount_point_for SHARE: /mnt/<share in lower case, spaces as dashes>
mount_point_for() { local s=${1,,}; printf '/mnt/%s' "${s// /-}"; }

# valid_share NAME: a bare share name (no path separator)
valid_share() { [ -n "$1" ] && [[ "$1" != */* && "$1" != *\\* ]]; }

# fstab_escape STRING: spaces as \040, the only special character in a share name here
fstab_escape() { printf '%s' "${1// /\\040}"; }

SMB_HINT="vérifiez dans Freebox OS (Paramètres de la Freebox > Partages Windows) que les partages sont activés, avec SMBv2/SMBv3 (pas activés d'office sur Delta)"

# try_share SHARE: succeeds when the Freebox lets a guest mount SHARE, else prints the
# first line of mount's error
try_share() {
  local tmp err rc=0
  tmp=$(mktemp -d)
  if err=$(mount -t cifs "//$FBX_HOST/$1" "$tmp" -o guest,ro 2>&1); then
    umount "$tmp"
  else
    rc=1; printf '%s' "${err%%$'\n'*}"
  fi
  rmdir "$tmp"
  return $rc
}

# probe_shares: candidate share names the Freebox accepts as guest, one per line
probe_shares() {
  local s
  for s in "${SHARE_CANDIDATES[@]}"; do
    try_share "$s" >/dev/null && printf '%s\n' "$s"
  done
  return 0
}

# recording_now: prints the running recordings (JSON) and succeeds when there is at least one
recording_now() {
  systemctl is-active -q fbxrec 2>/dev/null || return 1
  local port running
  port=$(sed -n 's/^FBXREC_PORT=//p' "$ENV_FILE" 2>/dev/null); port=${port:-8080}
  if ! running=$(curl -sf -m 10 -H "Host: localhost:$port" "http://localhost:$port/api/running"); then
    echo "(service actif mais muet, impossible de savoir)"; return 0   # unknown = assume busy
  fi
  [ "$running" = "[]" ] && return 1
  echo "$running"
}

guard_idle() {
  [ "${FORCE:-0}" = 1 ] && return 0
  local r
  if r=$(recording_now); then
    die "enregistrement en cours, rien n'est modifié (FORCE=1 pour forcer) : $r"
  fi
}

# fetch DEST: download and unpack the release to install into DEST
fetch() {
  local dest=$1 src=${FBXREC_TARBALL:-} tag
  if [ -z "$src" ] && [ -n "${FBXREC_BRANCH:-}" ]; then
    # development state of a branch instead of a release (test installs)
    [[ "$FBXREC_BRANCH" =~ ^[A-Za-z0-9._-]+$ ]] || die "nom de branche invalide : $FBXREC_BRANCH"
    src="https://github.com/$REPO/archive/refs/heads/$FBXREC_BRANCH.tar.gz"
    say "téléchargement de la branche $FBXREC_BRANCH (version de développement)"
  fi
  if [ -z "$src" ]; then
    tag=${FBXREC_VERSION:-}
    if [ -z "$tag" ]; then
      local meta
      meta=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest") \
        || die "impossible de trouver la dernière version de $REPO (pas de version publiée ?)"
      tag=$(printf '%s' "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
        || die "réponse inattendue de GitHub pour $REPO"
    fi
    src="https://github.com/$REPO/archive/refs/tags/$tag.tar.gz"
    say "téléchargement de $tag"
  fi
  if [ -f "$src" ]; then
    tar xzf "$src" -C "$dest" --strip-components=1 || die "archive illisible : $src"
  else
    curl -fsSL "$src" | tar xzf - -C "$dest" --strip-components=1 || die "téléchargement impossible : $src"
  fi
  [ -f "$dest/install.sh" ] && [ -d "$dest/fbxrec" ] || die "archive inattendue : $src"
}

install_packages() {
  local need=() p
  for p in python3 ffmpeg cifs-utils curl; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || need+=("$p")
  done
  [ ${#need[@]} -eq 0 ] && return 0
  say "installation des paquets : ${need[*]} (quelques minutes, bien plus sur Freebox Delta)"
  # cloud-init has usually just refreshed the package lists: skip a second update
  if ! find /var/lib/apt/lists -maxdepth 1 -name '*_Packages*' -newermt '-60 min' 2>/dev/null | grep -q .; then
    DEBIAN_FRONTEND=noninteractive apt-get update -q
  fi
  # no "recommended" extras (about 65 packages ffmpeg does not need for stream copy), and no
  # fsync per file: on the Delta the VM disk is a file on a hard drive, where packages took
  # 9 min 20 (2026-10-20, Ultra on SSD: about 2). Worst case of a power cut meanwhile:
  # recreate the VM.
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    -o Dpkg::Options::=--force-unsafe-io -o Dpkg::Progress-Fancy=1 "${need[@]}"
}

# choose_disk: prints the mount point of the Freebox disk, mounting it (and adding it to
# /etc/fstab) when the VM does not mount one yet
choose_disk() {
  local mounts n share mp user err
  mapfile -t mounts < <(freebox_mounts)
  n=${#mounts[@]}
  if [ "$n" -eq 1 ]; then printf '%s' "${mounts[0]}"; return; fi
  if [ "$n" -gt 1 ]; then
    say "plusieurs disques Freebox montés : ${mounts[*]}"
    die "précisez le dossier de sortie, par exemple FBXREC_OUT=\"${mounts[0]}/$OUT_SUBDIR\""
  fi
  share=${FBXREC_SHARE:-}
  if [ -n "$share" ]; then
    valid_share "$share" || die "nom de partage invalide : '$share'"
    err=$(try_share "$share") || die "montage de //$FBX_HOST/$share impossible ($err) ; $SMB_HINT"
  else
    mapfile -t found < <(probe_shares)
    if [ ${#found[@]} -eq 1 ]; then
      share=${found[0]}
    else
      if [ ${#found[@]} -gt 1 ]; then
        say "partages trouvés : ${found[*]}"
      else
        say "aucun disque Freebox trouvé sous les noms habituels (${SHARE_CANDIDATES[*]}) ; $SMB_HINT"
      fi
      while :; do
        share=$(ask "Nom du partage (\\\\Freebox_Server\\<partage> : seulement <partage>, vide pour abandonner) : ") \
          || die "pas de terminal pour demander le partage ; relancez avec FBXREC_SHARE=<nom du partage>"
        [ -n "$share" ] || die "abandon ; relancez l'installation une fois le partage trouvé"
        if ! valid_share "$share"; then
          say "nom invalide : donnez seulement le nom du partage, sans \\ ni /"
        elif err=$(try_share "$share"); then
          break
        else
          say "montage de //$FBX_HOST/$share impossible ($err)"
        fi
      done
    fi
  fi
  mp=$(mount_point_for "$share")
  user=${FBXREC_USER:-freebox}
  id "$user" >/dev/null 2>&1 || die "utilisateur $user inconnu (FBXREC_USER=...)"
  mkdir -p "$mp"
  if ! grep -q "[[:space:]]$(fstab_escape "$mp")[[:space:]]" /etc/fstab; then
    printf '%s\t%s\tcifs\tguest,uid=%s,gid=%s,_netdev,nofail\t0\t0\n' \
      "$(fstab_escape "//$FBX_HOST/$share")" "$(fstab_escape "$mp")" "$user" "$(id -gn "$user")" >> /etc/fstab
    systemctl daemon-reload
  fi
  mount "$mp" 2>/dev/null || mountpoint -q "$mp" || die "montage de //$FBX_HOST/$share impossible"
  say "disque Freebox //$FBX_HOST/$share monté sur $mp"
  printf '%s' "$mp"
}

write_config() {
  [ -f "$ENV_FILE" ] && { say "configuration existante gardée : $ENV_FILE"; return; }
  local out=${FBXREC_OUT:-}
  [ -n "$out" ] || out="$(choose_disk)/$OUT_SUBDIR"
  cat > "$ENV_FILE" <<EOF
# fbxrec settings, read by systemd at start (sudo systemctl restart fbxrec, never during a recording)
FBXREC_OUT=$out
FBXREC_PORT=8080
# extra Host names allowed for the web UI, comma separated (the VM IP and name always are)
#FBXREC_HOSTS=
EOF
  say "configuration écrite : $ENV_FILE (enregistrements dans $out)"
}

service_user() {
  local out owner
  [ -n "${FBXREC_USER:-}" ] && { printf '%s' "$FBXREC_USER"; return; }
  out=$(sed -n 's/^FBXREC_OUT=//p' "$ENV_FILE")
  # the deepest existing parent of the output folder decides: on CIFS only its uid can write
  while [ ! -d "$out" ]; do out=$(dirname "$out"); done
  owner=$(stat -c %U "$out")
  if [ "$owner" = root ]; then owner=freebox; fi
  printf '%s' "$owner"
}

run_tests() {
  local log=/tmp/fbxrec-tests.log
  say "tests..."
  if ! (cd "$1" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t . > "$log" 2>&1); then
    grep -A20 "^FAIL:\|^ERROR:" "$log" | head -60 >&2
    die "les tests échouent sur cette machine, rien n'est installé (journal : $log)"
  fi
  say "$(tail -1 "$log") ($(grep -o '^Ran [0-9]* tests' "$log"))"
}

# install_commands DIR: the fbxrec-update and fbxrec-password commands
install_commands() {
  cat > $1/fbxrec-update <<EOF
#!/bin/sh
exec $APP_DIR/install.sh --update "\$@"
EOF
  cat > $1/fbxrec-password <<EOF
#!/bin/sh
# set (no argument) or remove (--remove) the web password; effective at once, no restart
# not sourced: values may hold spaces ("Enregistrements TV"), only the data dir matters here
d=\$(sed -n 's/^FBXREC_DATA=//p' $ENV_FILE 2>/dev/null)
[ -n "\$d" ] && export FBXREC_DATA="\$d"
cd $APP_DIR && exec python3 -m fbxrec.auth "\$@"
EOF
  chmod 755 $1/fbxrec-update $1/fbxrec-password
}

# selftest USER OUT: a real 5 s capture of France 2 written where recordings go, as the
# service user. Proves the TV stream, ffmpeg and the Freebox disk together. Warns only:
# the recorder is installed either way.
SELFTEST_URL="rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201&flavour=sd"
selftest() {
  local user=$1 t="$2/.fbxrec-selftest.ts" size
  say "contrôle : capture de 5 s de France 2 vers le disque Freebox..."
  sudo -u "$user" timeout 40 ffmpeg -nostdin -v error -rtsp_transport udp -i "$SELFTEST_URL" \
    -map 0:v:0 -map "0:a:m:language:fra" -t 5 -c copy -f mpegts -y "$t" >/dev/null 2>&1 || true
  size=$(stat -c %s "$t" 2>/dev/null || echo 0)
  rm -f "$t"
  if [ "$size" -gt 100000 ]; then
    say "contrôle OK : $((size / 1024)) Ko enregistrés"
  else
    say "ATTENTION : le contrôle n'a rien enregistré ($size octets). Vérifiez le disque de la Freebox et la réception TV."
  fi
}

install_from() {
  local src=$1 user out
  install_packages
  guard_idle
  run_tests "$src"
  write_config
  user=$(service_user)
  id "$user" >/dev/null 2>&1 || die "utilisateur de service $user inconnu (FBXREC_USER=...)"
  out=$(sed -n 's/^FBXREC_OUT=//p' "$ENV_FILE")
  sudo -u "$user" mkdir -p "$out" || die "$user ne peut pas créer $out"
  install -d -o "$user" -g "$(id -gn "$user")" "$DATA_DIR"
  guard_idle   # again: the tests take a while, a recording may have started meanwhile

  mkdir -p "$APP_DIR"
  rm -rf "$APP_DIR/fbxrec.new"
  cp -a "$src/fbxrec" "$APP_DIR/fbxrec.new"
  find "$APP_DIR/fbxrec.new" -name __pycache__ -prune -exec rm -rf {} +
  rm -rf "$APP_DIR/fbxrec.old"
  [ -d "$APP_DIR/fbxrec" ] && mv "$APP_DIR/fbxrec" "$APP_DIR/fbxrec.old"
  mv "$APP_DIR/fbxrec.new" "$APP_DIR/fbxrec"
  install -m 755 "$src/install.sh" "$APP_DIR/install.sh"
  [ -f "$src/VERSION" ] && install -m 644 "$src/VERSION" "$APP_DIR/VERSION"

  install -m 644 "$src/systemd/fbxrec.service" /etc/systemd/system/fbxrec.service
  if [ "$user" != freebox ]; then
    mkdir -p /etc/systemd/system/fbxrec.service.d
    printf '[Service]\nUser=%s\n' "$user" > /etc/systemd/system/fbxrec.service.d/user.conf
  else
    rm -f /etc/systemd/system/fbxrec.service.d/user.conf
  fi
  install_commands /usr/local/bin

  systemctl daemon-reload
  systemctl enable -q fbxrec
  systemctl restart fbxrec
  sleep 3
  systemctl is-active -q fbxrec || { journalctl -u fbxrec -n 20 --no-pager >&2; die "le service ne démarre pas"; }
  local port ip
  port=$(sed -n 's/^FBXREC_PORT=//p' "$ENV_FILE"); port=${port:-8080}
  curl -s -o /dev/null -m 10 -H "Host: localhost:$port" "http://localhost:$port/" \
    || die "le service tourne mais ne répond pas sur le port $port"
  selftest "$user" "$out"
  ip=$(hostname -I | awk '{print $1}')
  say "OK, interface web : http://$ip:$port/ (installation terminée en $(elapsed))"
  if [ ! -f "$DATA_DIR/password" ]; then
    case "$(ask "Protéger l'interface web par un mot de passe ? (recommandé) [O/n] " || echo n)" in
      ""|o|O|y|Y) /usr/local/bin/fbxrec-password < /dev/tty || true ;;   # it says why itself
      *) say "sans mot de passe web : à définir dans la page, section Paramètres" ;;
    esac
  fi
}

main() {
  [ "$(id -u)" = 0 ] || die "à lancer en root : sudo $0"
  command -v systemctl >/dev/null || die "systemd requis"
  local here update=0
  [ "${1:-}" = --update ] && update=1
  here=$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
  if [ "$update" = 0 ] && [ -n "$here" ] && [ -d "$here/fbxrec" ] && [ -f "$here/systemd/fbxrec.service" ]; then
    install_from "$here"
    return
  fi
  guard_idle   # before downloading anything
  command -v curl >/dev/null || install_packages
  # global, not local: the EXIT trap runs after main has returned
  FBXREC_TMP=$(mktemp -d)
  trap 'rm -rf "$FBXREC_TMP"' EXIT
  local tmp=$FBXREC_TMP
  fetch "$tmp"
  bash "$tmp/install.sh"
}

[ "${FBXREC_SOURCE_ONLY:-0}" = 1 ] || main "$@"
