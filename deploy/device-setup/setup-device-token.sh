#!/usr/bin/env bash
# Headroom device-token setup for macOS and Linux.
#
#   ./setup-device-token.sh --url https://headroom.example.com            # prompts for token
#   ./setup-device-token.sh --url https://... --token hrt_xxx             # non-interactive
#   ./setup-device-token.sh --uninstall                                    # remove it again
#
# What it does (and nothing else):
#   1. Verifies the token against <url>/headroom/whoami. If that fails, it
#      changes NOTHING.
#   2. Backs up your shell rc file, then adds ONE marked block that APPENDS the
#      token header to ANTHROPIC_CUSTOM_HEADERS. Your existing headers (e.g.
#      X-Headroom-Project) are preserved, never replaced.
#
# It never touches ANTHROPIC_BASE_URL, ~/.claude/settings.json, or any project.
set -euo pipefail

BEGIN_MARK="# >>> headroom-device-token >>>"
END_MARK="# <<< headroom-device-token <<<"
HEADER_NAME="x-headroom-device-token"

URL=""
TOKEN="${HEADROOM_DEVICE_TOKEN:-}"
RC_FILE=""
UNINSTALL=0

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --url)       URL="${2:-}"; shift 2 ;;
    --token)     TOKEN="${2:-}"; shift 2 ;;
    --rc)        RC_FILE="${2:-}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 2 ;;
  esac
done

if [ -z "$RC_FILE" ]; then
  case "$(basename "${SHELL:-}")" in
    zsh)  RC_FILE="$HOME/.zshrc" ;;
    bash) if [ "$(uname -s)" = "Darwin" ]; then RC_FILE="$HOME/.bash_profile"; else RC_FILE="$HOME/.bashrc"; fi ;;
    *)    echo "Unsupported shell '${SHELL:-unknown}'. Re-run with --rc /path/to/rcfile" >&2; exit 2 ;;
  esac
fi

remove_block() {  # $1 = file ; prints result on stdout
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}' "$1"
}

backup() {
  local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
  cp -p "$RC_FILE" "$RC_FILE.headroom-backup-$stamp"
  echo "Backup written: $RC_FILE.headroom-backup-$stamp"
}

if [ "$UNINSTALL" = 1 ]; then
  [ -f "$RC_FILE" ] || { echo "$RC_FILE not found; nothing to remove."; exit 0; }
  if ! grep -qF "$BEGIN_MARK" "$RC_FILE"; then echo "No Headroom token block in $RC_FILE."; exit 0; fi
  backup
  tmp="$(mktemp)"; remove_block "$RC_FILE" > "$tmp"; cat "$tmp" > "$RC_FILE"; rm -f "$tmp"
  echo "Removed. Open a NEW terminal for it to take effect."
  exit 0
fi

[ -n "$URL" ] || { echo "Missing --url (the Headroom server, e.g. https://headroom.example.com)" >&2; usage 2; }
URL="${URL%/}"
case "$URL" in
  https://*) ;;
  http://localhost*|http://127.0.0.1*) ;;
  http://*) echo "WARNING: $URL is plain HTTP. Your token travels unencrypted. Ask your admin for the https:// address." >&2 ;;
  *) echo "--url must start with https:// (or http://)" >&2; exit 2 ;;
esac

if [ -z "$TOKEN" ]; then
  printf "Paste your device token (input hidden): "
  read -r -s TOKEN; echo
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
case "$TOKEN" in hrt_*) ;; *) echo "That doesn't look like a Headroom device token (should start with hrt_)." >&2; exit 2 ;; esac
command -v curl >/dev/null || { echo "curl is required." >&2; exit 2; }

echo "Checking token with $URL/headroom/whoami ..."
RESP="$(curl -fsS --max-time 15 -H "$HEADER_NAME: $TOKEN" "$URL/headroom/whoami" 2>&1)" || {
  echo "FAIL: could not reach $URL/headroom/whoami: $RESP" >&2
  echo "Nothing was changed." >&2; exit 1; }
if ! printf '%s' "$RESP" | grep -q '"valid":[[:space:]]*true'; then
  echo "FAIL: the server does not recognise this token (revoked, expired or mistyped)." >&2
  echo "Nothing was changed." >&2; exit 1
fi
DEVICE="$(printf '%s' "$RESP" | sed -n 's/.*"device":[[:space:]]*"\([^"]*\)".*/\1/p')"
echo "OK: token accepted for device '${DEVICE:-unknown}'."

[ -f "$RC_FILE" ] || touch "$RC_FILE"
backup
tmp="$(mktemp)"
remove_block "$RC_FILE" > "$tmp"     # idempotent: replace any previous block
{
  cat "$tmp"
  printf '\n%s\n' "$BEGIN_MARK"
  printf '%s\n' "# Appends the device token; existing ANTHROPIC_CUSTOM_HEADERS (e.g. X-Headroom-Project) are kept."
  printf '%s\n' "case \"\${ANTHROPIC_CUSTOM_HEADERS:-}\" in"
  printf '%s\n' "  *$HEADER_NAME*) ;;"
  # A literal newline inside the quotes (portable to bash and zsh) separates headers.
  printf '%s\n' "  *) export ANTHROPIC_CUSTOM_HEADERS=\"\${ANTHROPIC_CUSTOM_HEADERS:+\$ANTHROPIC_CUSTOM_HEADERS"
  printf '%s\n' "}$HEADER_NAME: $TOKEN\" ;;"
  printf '%s\n' "esac"
  printf '%s\n' "$END_MARK"
} > "$RC_FILE.new"
rm -f "$tmp"
chmod 600 "$RC_FILE.new" 2>/dev/null || true
# keep original permissions of the rc file
if [ -f "$RC_FILE" ]; then chmod --reference="$RC_FILE" "$RC_FILE.new" 2>/dev/null || true; fi
cat "$RC_FILE.new" > "$RC_FILE"; rm -f "$RC_FILE.new"

echo "Done. Open a NEW terminal (or run: source $RC_FILE) and start Claude Code."
echo "Keep ANY line that sets ANTHROPIC_CUSTOM_HEADERS (project header) ABOVE the Headroom block."
