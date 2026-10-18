FROM nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94

COPY docker/playback-gateway/nginx.conf /etc/nginx/nginx.conf
COPY docker/playback-gateway/default.conf.template /etc/nginx/pixav.conf.template
COPY docker/playback-gateway/jellyfin-auth.conf /etc/nginx/pixav-jellyfin-auth.conf
COPY --chmod=755 docker/playback-gateway/start.sh /usr/local/bin/pixav-playback-gateway
COPY --chmod=755 docker/playback-gateway/jellyfin-start.sh /usr/local/bin/pixav-jellyfin-gateway

USER 1000:1000
ENTRYPOINT ["/usr/local/bin/pixav-playback-gateway"]
CMD []
