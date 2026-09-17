#!/usr/bin/env bash
set -euo pipefail

expected_version="6.6.87.2-microsoft-standard-WSL2-pixav-redroid"

fail() {
    echo "WSL kernel runtime gate: FAIL — $*" >&2
    exit 1
}

actual_version="$(uname -r)"
[[ "${actual_version}" == "${expected_version}" ]] || \
    fail "running ${actual_version}; expected ${expected_version}"

[[ -r /proc/config.gz ]] || fail "/proc/config.gz is unavailable"
runtime_config="$(gzip -dc /proc/config.gz)"

required_lines=(
    'CONFIG_ANDROID_BINDER_IPC=y'
    'CONFIG_ANDROID_BINDERFS=y'
    'CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"'
    'CONFIG_DMABUF_HEAPS=y'
    'CONFIG_DMABUF_HEAPS_SYSTEM=y'
    'CONFIG_ISO9660_FS=y'
    'CONFIG_TUN=y'
    'CONFIG_LLC=y'
    'CONFIG_STP=y'
    'CONFIG_BRIDGE=y'
    'CONFIG_BRIDGE_NETFILTER=y'
    'CONFIG_IP_NF_IPTABLES=y'
    'CONFIG_IP_NF_FILTER=y'
    'CONFIG_IP_NF_NAT=y'
    'CONFIG_IP_NF_MANGLE=y'
    'CONFIG_IP_NF_RAW=y'
    'CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=y'
    'CONFIG_NETFILTER_XT_MATCH_COMMENT=y'
    'CONFIG_NETFILTER_XT_MATCH_CONNTRACK=y'
    'CONFIG_NETFILTER_XT_MATCH_MULTIPORT=y'
    'CONFIG_NETFILTER_XT_NAT=y'
    'CONFIG_NETFILTER_XT_TARGET_MASQUERADE=y'
)

for required in "${required_lines[@]}"; do
    grep -Fxq "${required}" <<<"${runtime_config}" || fail "missing ${required}"
done

grep -Eq '(^|[[:space:]])binder$' /proc/filesystems || fail "binderfs is not registered"
grep -Eq '(^|[[:space:]])iso9660$' /proc/filesystems || fail "iso9660 is not registered"

# Docker Desktop owns the namespace in which Compose resolves ``devices:`` and
# gluetun programs its firewall. The calling Ubuntu distro has a separate /dev
# and network namespace: it may legitimately have no /dev/net/tun node, and an
# unprivileged host iptables invocation always returns EPERM. Probe the actual
# deployment boundary instead, with networking disabled so this check cannot
# establish a VPN or other external connection.
gluetun_image="${PIXAV_GLUETUN_IMAGE:-qmcgaw/gluetun:v3}"
command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable"
docker image inspect "${gluetun_image}" >/dev/null 2>&1 || \
    fail "gluetun probe image is unavailable locally: ${gluetun_image}"
docker run --rm --network none --cap-add NET_ADMIN --device /dev/net/tun \
    --entrypoint /bin/sh "${gluetun_image}" -ec \
    'test -c /dev/net/tun; iptables -t filter -L -n >/dev/null; iptables -t nat -L -n >/dev/null' \
    >/dev/null 2>&1 || fail "Docker TUN/netfilter probe failed"

echo "WSL kernel runtime gate: PASS"
echo "kernel=${actual_version}"
echo "binderfs=registered dmabuf_system_heap=configured docker_tun=present docker_netfilter=ready"
