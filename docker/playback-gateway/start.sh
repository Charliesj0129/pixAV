#!/bin/sh
set -eu
umask 077
PIXAV_RESOLVER_BEARER=$(cat /run/secrets/resolver_token)
case "$PIXAV_RESOLVER_BEARER" in
    ''|*[!A-Za-z0-9_-]*) exit 1 ;;
esac
[ "${#PIXAV_RESOLVER_BEARER}" -ge 43 ]
[ "${#PIXAV_RESOLVER_BEARER}" -le 128 ]
export PIXAV_RESOLVER_BEARER
envsubst '${PIXAV_RESOLVER_BEARER}' < /etc/nginx/pixav.conf.template > /etc/nginx/conf.d/default.conf
unset PIXAV_RESOLVER_BEARER
exec nginx -g 'daemon off;'
