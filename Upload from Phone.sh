#!/bin/bash
# pi-paint VJ — upload videos from your phone.
# Double-click in the file manager and choose "Execute".
#
# What it does, in order:
#   1. Turns the Pi into its own WiFi hotspot (so it works at a campsite
#      with no router/internet). Your phone joins it like any WiFi.
#   2. Starts a little upload web page that drops videos straight into
#      assets/clips/.
#   3. Pops a dialog telling you the WiFi name, password, and the URL to
#      open on your phone. Leave that dialog OPEN while you upload.
#   4. When you click "Done", it stops the server and puts the Pi's WiFi
#      back the way it was.
#
# If the hotspot can't start (no NetworkManager, no spare WiFi radio),
# it falls back to plain mode: it just runs the upload page on whatever
# network the Pi is already on and shows you the address to type.
#
# After uploading, double-click "assets/Process Assets.sh" to make the clips
# ready to play.

cd "$(dirname "$0")"

# ── Hotspot settings (what your phone will see / type) ────────────────
HOTSPOT_SSID="VJ-PI"         # the WiFi network name your phone joins
HOTSPOT_PASS="campvibes"     # must be 8+ characters for WiFi
HOTSPOT_IP="10.42.42.42"     # the Pi's address -> you'll open http://<this>:<PORT>
PORT=8000                    # the port the upload page listens on
WIFI_IFACE="wlan0"
HOTSPOT_CON="VJ-Hotspot"     # internal NetworkManager profile name (leave as-is)

# Want a different address? Change HOTSPOT_IP above to anything you like,
# e.g. 192.168.4.1 or 10.0.0.1. The phone joins the WiFi and you open
# http://<HOTSPOT_IP>:<PORT>. The Pi hands your phone a matching address
# automatically. (Use a private range: 10.x.x.x, 192.168.x.x, or
# 172.16–31.x.x.)

LOG="$(pwd)/vj_last_upload.log"
: >"$LOG"
date '+[VJ] upload start: %Y-%m-%d %H:%M:%S' >>"$LOG"

show_error() {
  local title="$1" body="$2"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --width=720 --title="$title" --text="$body" 2>/dev/null
  elif command -v xmessage >/dev/null 2>&1; then
    printf '%s\n\n%s\n' "$title" "$body" | xmessage -file - 2>/dev/null
  else
    printf '%s\n\n%s\n' "$title" "$body" >&2
  fi
}

# ── Pick a Python (stdlib only, so the venv is optional) ──────────────
if [ -x "./venv/bin/python" ]; then
  PY="./venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  show_error "VJ-pi: Python missing" \
    "Couldn't find python3.\n\nDouble-click setup.sh first.\n\nLog: $LOG"
  exit 1
fi

# ── 1. Pick a mode: Pi hotspot (camp) or existing WiFi (home) ─────────
# Hotspot mode makes the Pi its own WiFi — great at a campsite, but it
# kicks the Pi off whatever WiFi it's currently on (one radio). At home,
# where the Pi and phone already share a WiFi, you want "this WiFi" so
# nothing switches and your phone keeps its internet.
HOTSPOT_UP=0
PREV_WIFI=""
GATEWAY_IP=""
WANT_HOTSPOT=1

if command -v zenity >/dev/null 2>&1; then
  MODE=$(zenity --list --radiolist \
    --title="Upload from Phone" \
    --text="How should your phone reach the Pi?" \
    --column="" --column="mode" --column="Choose one" \
    TRUE  hotspot "Pi makes its OWN WiFi  —  camping / no router" \
    FALSE wifi    "Use the WiFi the Pi is ALREADY on  —  home" \
    --hide-column=2 --print-column=2 \
    --width=540 --height=260 --ok-label="Start" 2>/dev/null)
  # Cancel or window-close -> abort without touching anything.
  if [ -z "$MODE" ]; then
    echo "[VJ] mode picker cancelled — exiting" >>"$LOG"
    exit 0
  fi
  [ "$MODE" = "wifi" ] && WANT_HOTSPOT=0
fi
echo "[VJ] mode: $([ "$WANT_HOTSPOT" = 1 ] && echo hotspot || echo existing-wifi)" >>"$LOG"

if [ "$WANT_HOTSPOT" = "1" ] && command -v nmcli >/dev/null 2>&1; then
  echo "[VJ] bringing up hotspot '$HOTSPOT_SSID' at $HOTSPOT_IP on $WIFI_IFACE" >>"$LOG"
  # Remember the WiFi network we're currently on so we can restore it.
  PREV_WIFI=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null \
                | awk -F: '$2=="802-11-wireless"{print $1; exit}')
  echo "[VJ] previous wifi connection: ${PREV_WIFI:-<none>}" >>"$LOG"

  # Create (or update) a dedicated access-point profile with a FIXED IP,
  # so the upload URL is always the same address you chose above.
  # ipv4.method=shared runs NetworkManager's built-in dnsmasq, which hands
  # the phone a matching DHCP lease in this subnet automatically.
  if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$HOTSPOT_CON"; then
    nmcli connection modify "$HOTSPOT_CON" \
      802-11-wireless.ssid "$HOTSPOT_SSID" \
      802-11-wireless.mode ap 802-11-wireless.band bg \
      ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASS" \
      connection.autoconnect no >>"$LOG" 2>&1
  else
    nmcli connection add type wifi ifname "$WIFI_IFACE" con-name "$HOTSPOT_CON" \
      autoconnect no ssid "$HOTSPOT_SSID" >>"$LOG" 2>&1
    nmcli connection modify "$HOTSPOT_CON" \
      802-11-wireless.mode ap 802-11-wireless.band bg \
      ipv4.method shared ipv4.addresses "$HOTSPOT_IP/24" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASS" >>"$LOG" 2>&1
  fi

  if nmcli connection up "$HOTSPOT_CON" >>"$LOG" 2>&1; then
    HOTSPOT_UP=1
    sleep 2
    GATEWAY_IP="$HOTSPOT_IP"
    echo "[VJ] hotspot up at $GATEWAY_IP" >>"$LOG"
  else
    echo "[VJ] hotspot failed to start — falling back to plain mode" >>"$LOG"
  fi
elif [ "$WANT_HOTSPOT" = "1" ]; then
  echo "[VJ] nmcli not found — using existing WiFi instead" >>"$LOG"
fi

# ── 2. Start the upload server ────────────────────────────────────────
"$PY" upload_server.py --port "$PORT" --host 0.0.0.0 >>"$LOG" 2>&1 &
SERVER_PID=$!
sleep 1

cleanup() {
  echo "[VJ] stopping server (pid $SERVER_PID)" >>"$LOG"
  kill "$SERVER_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  if [ "$HOTSPOT_UP" = "1" ]; then
    echo "[VJ] taking hotspot down" >>"$LOG"
    nmcli connection down "$HOTSPOT_CON" >>"$LOG" 2>&1
    if [ -n "$PREV_WIFI" ]; then
      echo "[VJ] restoring wifi '$PREV_WIFI'" >>"$LOG"
      nmcli connection up "$PREV_WIFI" >>"$LOG" 2>&1
    fi
  fi
  date '+[VJ] upload end: %Y-%m-%d %H:%M:%S' >>"$LOG"
}
trap cleanup EXIT

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  TAIL=$(tail -25 "$LOG" 2>/dev/null)
  show_error "VJ-pi: upload server didn't start" \
    "The upload page failed to start (port $PORT may be in use).\n\nLog: $LOG\n\nLast lines:\n\n$TAIL"
  exit 1
fi

# ── 3. Build the instructions and show them ───────────────────────────
if [ "$HOTSPOT_UP" = "1" ]; then
  URL="http://${GATEWAY_IP}:${PORT}"
  BODY="<b>Step 1.</b> On your phone, open WiFi settings and join:\n\n   Network:  <b>${HOTSPOT_SSID}</b>\n   Password: <b>${HOTSPOT_PASS}</b>\n\n<b>Step 2.</b> Open your phone's browser and go to:\n\n   <b>${URL}</b>\n\n<b>Step 3.</b> Pick where the videos go (2K / 4K / portrait),\nthen tap “Choose videos” and select your clips.\n\nLeave THIS window open while you upload.\nClick <b>Done</b> below when you've finished — that stops the\nserver and puts the Pi's WiFi back to normal.\n\nFinished HEVC clips play right away. Anything raw\n(2K / 4K / portrait) needs <b>assets/Process All Assets.sh</b> next."
  TITLE="📲 Upload from Phone — hotspot ready"
else
  IPS=$(hostname -I 2>/dev/null)
  URLLIST=""
  for ip in $IPS; do
    case "$ip" in
      *.*) URLLIST="${URLLIST}   <b>http://${ip}:${PORT}</b>\n" ;;
    esac
  done
  [ -z "$URLLIST" ] && URLLIST="   (couldn't detect this Pi's IP — check the network)\n"
  if [ "$WANT_HOTSPOT" = "1" ]; then
    INTRO="The Pi's own hotspot couldn't start, so the upload page is\nrunning on the network the Pi is ALREADY connected to."
  else
    INTRO="The upload page is running on the WiFi the Pi is\nALREADY connected to — nothing on the Pi changed."
  fi
  BODY="${INTRO}\n\n<b>Make sure your phone is on the same WiFi as the Pi</b>,\nthen open your phone's browser and go to one of:\n\n${URLLIST}\nPick where the videos go (2K / 4K / portrait), then tap\n“Choose videos” and select your clips.\n\nLeave THIS window open while you upload. Click <b>Done</b>\nbelow when finished.\n\nFinished HEVC clips play right away. Anything raw needs\n<b>assets/Process All Assets.sh</b> next."
  TITLE="📲 Upload from Phone — server running"
fi

if command -v zenity >/dev/null 2>&1; then
  zenity --info --width=600 --title="$TITLE" --text="$BODY" \
    --ok-label="Done — stop server" 2>/dev/null
else
  # No zenity: fall back to waiting on the server in the foreground.
  show_error "$TITLE" "$BODY\n\n(Close this to stop the server.)"
fi

# Dialog closed -> trap cleanup runs on exit.
exit 0
