#!/usr/bin/env bash
# Odin raz na GPU-hoste: podgotovit' CIFS-maunt samba-shary dlya vygruzki.
# Skript NE virypolnyaet pravki fstab molcha — vyvodyt gotovuyu instruktsiyu
# i vypolnyaet tol'ko ustanovku cifs-utils i (posle podtverzhdeniya) mount -a.
#
# Ispol'zovanie:
#   SERVER=//fileserver/mnema SHARE-ne-vazhno, sm. polnyy put' nizhe
#   SMB_PATH='//fileserver/mnema-export' SMB_USER=mnema SMB_DOMAIN=WORKGROUP ./scripts/smb-mount.sh
#
# Paroli v git NE popadayut: tol'ko /etc/samba/creds.mnema na hoste i .env (untracked).
set -euo pipefail

MOUNT_POINT="${MOUNT_POINT:-/mnt/mnema-export}"
CREDS_FILE="${CREDS_FILE:-/etc/samba/creds.mnema}"
SMB_PATH="${SMB_PATH:?SKAZhITE SMB_PATH, napr. //fileserver/share}"
SMB_USER="${SMB_USER:?SKAZhITE SMB_USER}"
SMB_DOMAIN="${SMB_DOMAIN:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Zapustite ot root (skript trogaet /etc): sudo $0" >&2
  exit 1
fi

echo "==> 1. cifs-utils"
if ! command -v mount.cifs >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y cifs-utils
else
  echo "uzhe ustanovlen"
fi

echo "==> 2. credentials: $CREDS_FILE (interaktivno, bez parametrov komandnoy stroki)"
if [[ ! -f "$CREDS_FILE" ]]; then
  mkdir -p "$(dirname "$CREDS_FILE")"
  umask 077
  {
    echo "username=$SMB_USER"
    read -rs -p "Password: " pw && echo "password=$pw"
    [[ -n "$SMB_DOMAIN" ]] && echo "domain=$SMB_DOMAIN"
  } > "$CREDS_FILE"
  chmod 600 "$CREDS_FILE"
  echo "sozdan $CREDS_FILE (chmod 600)"
else
  echo "uzhe est' — propuskayu (pravte vruchnuyu pri neobhodimosti)"
fi

FSTAB_LINE="$SMB_PATH $MOUNT_POINT cifs credentials=$CREDS_FILE,vers=3.0,iocharset=utf8,file_mode=0664,dir_mode=0775,_netdev,noserverino 0 0"

echo
echo "==> 3. Dobav'te v /etc/fstab (skript NE pravitt fstab avtomaticheskuyu):"
echo "    $FSTAB_LINE"
echo
read -r -p "Vypolnit' mkdir + mount -a seychas? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  mkdir -p "$MOUNT_POINT"
  mount -a
  TEST_FILE="$MOUNT_POINT/.mnema-write-test.$$"
  if touch "$TEST_FILE" 2>/dev/null; then
    rm -f "$TEST_FILE"
    echo "OK: $MOUNT_POINT dostupen na zapisi"
  else
    echo "NASTOROZHENNOST': $MOUNT_POINT NE dostupen na zapisi" >&2
    exit 1
  fi
else
  echo "Nastroyka fstab ostanovlena vruchnuyu; posle pravki — sudo mount -a"
fi

echo
echo "==> 4. .env docker compose (na servere, untracked) — dobavit': "
echo "    EXPORT_HOST_DIR=$MOUNT_POINT"
echo "    EXPORT_DIR=/export"
echo "    zanovo: docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.override.yml up -d"
