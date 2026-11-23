#!/bin/sh
set -eu

url=${TTS_CLIPS_BACKEND_URL:-}
case "$url" in
    ''|*[!a-zA-Z0-9.:/-]*) valid=no ;;
    *) valid=yes ;;
esac
port=${url##*:}
if [ "$valid" != yes ] || ! printf '%s\n' "$url" |
    grep -Eq '^http://[a-zA-Z0-9][a-zA-Z0-9.-]*:[0-9]{1,5}$' ||
    [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    echo >&2 'Set TTS_CLIPS_BACKEND_URL to http://<internal DNS name or IPv4>:8765.'
    exit 1
fi
