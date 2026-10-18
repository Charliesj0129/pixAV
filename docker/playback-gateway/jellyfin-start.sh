#!/bin/sh
set -eu
umask 077
cat /etc/nginx/pixav-jellyfin-auth.conf > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
