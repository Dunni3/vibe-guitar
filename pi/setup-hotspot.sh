#!/bin/bash
# Set up the guitar FX box's offline Wi-Fi on the Pi (idempotent; safe to re-run).
#
#   sudo ./setup-hotspot.sh                  install / update everything
#   sudo ./setup-hotspot.sh --new-password   also change the hotspot password
#   sudo ./setup-hotspot.sh --password-file F   read the password from root-only file F
#   sudo ./setup-hotspot.sh --uninstall      remove it all (home Wi-Fi is untouched)
#
# Installs:
#   NetworkManager profile 'fxbox-hotspot': SSID fxbox, WPA2-PSK (AES), 2.4 GHz
#     channel 6, shared IPv4 on 10.42.0.1/24 (NM runs DHCP + DNS), not autoconnect.
#     The password is asked for once and kept only in the root-only (0600) keyfile
#     /etc/NetworkManager/system-connections/fxbox-hotspot.nmconnection.
#   /usr/local/bin/fxbox-net                 the switch: status|home|hotspot|auto|trial
#   /etc/systemd/system/fxbox-net-fallback.service   at boot: home Wi-Fi, else hotspot
#   /etc/default/fxbox-net                   settings (kept if it already exists)
#
# It does not change the current network or the home Wi-Fi profile, so it is
# safe to run over SSH.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
CON=fxbox-hotspot
SSID=fxbox
KEYFILE=/etc/NetworkManager/system-connections/$CON.nmconnection
BIN=/usr/local/bin/fxbox-net
UNIT=/etc/systemd/system/fxbox-net-fallback.service
DEFAULTS=/etc/default/fxbox-net

new_password=0
password_file=""
uninstall=0
while [ $# -gt 0 ]; do
    case $1 in
        --new-password) new_password=1 ;;
        --password-file) password_file=${2:?--password-file needs a path}; shift ;;
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { echo "==> $*"; }
warn() { echo "warning: $*" >&2; }
die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
command -v nmcli >/dev/null || die "NetworkManager (nmcli) is not installed"

if [ "$uninstall" = 1 ]; then
    if nmcli -t -f NAME con show --active | grep -qx "$CON"; then
        say "hotspot is active; rejoining home Wi-Fi first"
        "$BIN" home || true
    fi
    systemctl disable --now fxbox-net-fallback.service 2>/dev/null || true
    rm -f "$UNIT" "$BIN" "$KEYFILE"
    systemctl daemon-reload
    nmcli con reload
    say "removed (left $DEFAULTS in place)"
    exit 0
fi

for f in fxbox-net fxbox-net-fallback.service fxbox-net.default; do
    [ -f "$HERE/$f" ] || die "missing $HERE/$f (copy the whole pi/ directory)"
done

# --- checks ---------------------------------------------------------------
nmcli -t -f DEVICE,TYPE dev | grep -qx 'wlan0:wifi' || die "no wlan0 Wi-Fi device in NetworkManager"
command -v dnsmasq >/dev/null || [ -x /usr/sbin/dnsmasq ] ||
    die "dnsmasq is missing; NM's shared mode needs it: apt install dnsmasq-base"
if [ -x /usr/sbin/iw ] && /usr/sbin/iw reg get | grep -q 'country 00'; then
    warn "Wi-Fi country is not set; the AP may not start. Set it: raspi-config nonint do_wifi_country US"
fi
systemctl is-active --quiet avahi-daemon ||
    warn "avahi-daemon is not running, so fxbox.local won't resolve; use http://10.42.0.1:8080"

# --- password -------------------------------------------------------------
valid_psk() {
    # WPA2 passphrase: 8-63 printable ASCII; no leading/trailing spaces (keeps the keyfile simple).
    [ ${#1} -ge 8 ] && [ ${#1} -le 63 ] && LC_ALL=C grep -qx '[ -~]*' <<<"$1" &&
        [ "${1# }" = "$1" ] && [ "${1% }" = "$1" ]
}

psk=""
if [ -n "$password_file" ]; then
    [ -r "$password_file" ] || die "cannot read $password_file"
    psk=$(head -n1 "$password_file")
elif [ "$new_password" = 0 ] && [ -f "$KEYFILE" ]; then
    # Keep the existing password (stored with backslashes doubled; undo that).
    psk=$(sed -n 's/^psk=//p' "$KEYFILE" | head -n1 | sed 's/\\\\/\\/g')
fi
if [ -z "$psk" ]; then
    [ -r /dev/tty ] || die "no terminal to ask for the password; use --password-file"
    while :; do
        read -rsp "Hotspot password for Wi-Fi '$SSID' (8-63 characters): " p1 </dev/tty; echo
        read -rsp "Same again: " p2 </dev/tty; echo
        if [ "$p1" != "$p2" ]; then echo "They don't match; try again."; continue; fi
        if ! valid_psk "$p1"; then echo "Needs 8-63 plain ASCII characters, no leading/trailing spaces."; continue; fi
        psk=$p1; break
    done
    unset p1 p2
fi
valid_psk "$psk" || die "the hotspot password must be 8-63 plain ASCII characters"

# --- hotspot profile ------------------------------------------------------
uuid=""
[ -f "$KEYFILE" ] && uuid=$(sed -n 's/^uuid=//p' "$KEYFILE" | head -n1)
[ -n "$uuid" ] || uuid=$(cat /proc/sys/kernel/random/uuid)

# Keyfile strings escape backslashes as \\.
bs='\'
psk_esc=${psk//"$bs"/"$bs$bs"}
unset psk

say "writing hotspot profile $KEYFILE"
umask 077
tmp=$(mktemp "$KEYFILE.XXXXXX")
trap 'rm -f "$tmp"' EXIT
# Written as a keyfile (not with nmcli) so the password never appears on a
# command line, and so NM's netplan integration doesn't copy it elsewhere.
cat >"$tmp" <<EOF
[connection]
id=$CON
uuid=$uuid
type=wifi
interface-name=wlan0
autoconnect=false

[wifi]
mode=ap
ssid=$SSID
# 2.4 GHz channel 6: every phone supports it and it is never a DFS channel.
band=bg
channel=6
powersave=2

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
# PMF off: the Pi's brcmfmac AP is unreliable with it for some phones.
pmf=1
psk=$psk_esc

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=disabled
EOF
unset psk_esc
chmod 600 "$tmp"
mv "$tmp" "$KEYFILE"
trap - EXIT
nmcli con load "$KEYFILE"
nmcli -t -f NAME con show | grep -qx "$CON" || die "NetworkManager did not load $KEYFILE"
if nmcli -t -f NAME con show --active | grep -qx "$CON"; then
    warn "hotspot is running; changes apply next time it starts"
fi

# --- switch + boot fallback ----------------------------------------------
say "installing $BIN, $UNIT"
install -m 755 "$HERE/fxbox-net" "$BIN"
install -m 644 "$HERE/fxbox-net-fallback.service" "$UNIT"
if [ -f "$DEFAULTS" ]; then
    say "keeping existing $DEFAULTS"
else
    install -m 644 "$HERE/fxbox-net.default" "$DEFAULTS"
fi
systemctl daemon-reload
# Enable only: starting it now would just confirm we're on home Wi-Fi.
systemctl enable fxbox-net-fallback.service

boot_wait=$(. "$DEFAULTS"; echo "${BOOT_WAIT:-40}")
say "done. Current network:"
"$BIN" status
cat <<EOF

Next boot: home Wi-Fi if it's there within ${boot_wait}s, otherwise hotspot '$SSID'.
  fxbox-net status          where am I
  sudo fxbox-net hotspot    force the hotspot (cuts SSH over home Wi-Fi)
  sudo fxbox-net home       back to home Wi-Fi (falls back to the hotspot if it fails)
  sudo fxbox-net trial 300  self-test: fallback, hold 5 min, return home
EOF
