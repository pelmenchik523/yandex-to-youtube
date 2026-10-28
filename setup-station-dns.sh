#!/bin/sh
# Blackhole Yandex Music audio streams on the Station.
#
# The Station keeps resolving normal names, but every *.strm.yandex.net name
# is answered with 127.0.0.1.  Alice still recognises the request and sends the
# AUDIO_PLAY directive; only the native music download fails, so the bridge can
# replace it with YouTube.  TTS and Alice's other services use different hosts
# and keep working.
set -eu

cd "$(dirname "$0")"
[ -f station.env ] && . ./station.env

STATION_HOST="${STATION_HOST:-192.168.1.205}"
STATION_PORT="${STATION_PORT:-2222}"
STATION_USER="${STATION_USER:-root}"
CONF="/data/unbound/unbound.conf"
REMOTE_TMP="/tmp/unbound.conf.ytstation"
SSH="ssh -T -p $STATION_PORT -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 $STATION_USER@$STATION_HOST"

if ! $SSH "test -f $CONF"; then
    echo "Не найден $CONF на станции $STATION_HOST." >&2
    exit 1
fi

if $SSH "grep -q 'strm.yandex.net' $CONF"; then
    if $SSH "nslookup strm.yandex.net 2>/dev/null | grep -q '127.0.0.1'" >/dev/null 2>&1; then
        echo "Блокировка Яндекс Музыки уже настроена."
        echo "DNS: *.strm.yandex.net -> 127.0.0.1"
        exit 0
    fi
    echo "Настройка в конфиге есть, перезапускаю unbound..."
    $SSH "/etc/init.d/S70unbound restart" >/dev/null
    sleep 1
    if $SSH "nslookup strm.yandex.net 2>/dev/null | grep -q '127.0.0.1'"; then
        echo "DNS: *.strm.yandex.net -> 127.0.0.1"
        exit 0
    fi
    echo "В конфиге блокировка есть, но DNS не отвечает 127.0.0.1." >&2
    exit 1
fi

LOCAL_TMP="$(mktemp)"
LOCAL_NEW="$(mktemp)"
trap 'rm -f "$LOCAL_TMP" "$LOCAL_NEW"' EXIT

$SSH "cat $CONF" > "$LOCAL_TMP"

# Insert the local zone before the forward-zone section.  Unbound's parser on
# the Station rejects top-level local-zone lines placed after forward-zone.
awk '
    BEGIN { inserted = 0 }
    /^forward-zone:[[:space:]]*$/ && !inserted {
        print "# Plan B: native Yandex Music streams are blackholed; the bridge plays YouTube."
        print "local-zone: \"strm.yandex.net.\" redirect"
        print "local-data: \"strm.yandex.net. A 127.0.0.1\""
        print ""
        inserted = 1
    }
    { print }
    END { if (!inserted) exit 2 }
' "$LOCAL_TMP" > "$LOCAL_NEW" || {
    echo "В $CONF не найден forward-zone; не трогаю конфиг." >&2
    exit 1
}

cat "$LOCAL_NEW" | $SSH "cat > $REMOTE_TMP"

if ! $SSH "unbound-checkconf $REMOTE_TMP >/dev/null 2>&1"; then
    echo "unbound-checkconf отклонил новый конфиг; изменения не применены." >&2
    exit 1
fi

$SSH "cp $CONF $CONF.bak-ytstation && cp $REMOTE_TMP $CONF && /etc/init.d/S70unbound restart" >/dev/null

if $SSH "nslookup strm.yandex.net 2>/dev/null | grep -q '127.0.0.1'"; then
    echo "Готово: *.strm.yandex.net -> 127.0.0.1 на станции $STATION_HOST."
    echo "Нативная Яндекс Музыка больше не звучит; TTS и Алиса работают как раньше."
else
    echo "Конфиг применён, но проверка DNS не прошла. Проверьте unbound на станции." >&2
    exit 1
fi
