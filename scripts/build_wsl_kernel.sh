#!/usr/bin/env bash
set -euo pipefail

kernel_tag="linux-msft-wsl-6.6.87.2"
kernel_commit="427645e3db3a8896714f22a3d3fe0c3f7b317ad4"
workspace="${1:-build/wsl2-kernel}"
source_dir="${workspace}/source"
output_dir="${workspace}/out"

mkdir -p "${workspace}"
if [[ ! -d "${source_dir}/.git" ]]; then
    git clone --filter=blob:none --depth 1 --branch "${kernel_tag}" \
        https://github.com/microsoft/WSL2-Linux-Kernel.git "${source_dir}"
fi

actual_commit="$(git -C "${source_dir}" rev-parse HEAD)"
if [[ "${actual_commit}" != "${kernel_commit}" ]]; then
    echo "unexpected ${kernel_tag} commit: ${actual_commit}" >&2
    exit 1
fi

docker build -t pixav-wsl-kernel-builder:24.04 docker/wsl-kernel
mkdir -p "${output_dir}"

source_abs="$(realpath "${source_dir}")"
output_abs="$(realpath "${output_dir}")"
source_epoch="$(git -C "${source_dir}" show -s --format=%ct HEAD)"

docker run --rm --user "$(id -u):$(id -g)" \
    -v "${source_abs}:/src:ro" \
    -v "${output_abs}:/out" \
    -w /src \
    pixav-wsl-kernel-builder:24.04 \
    bash -euc '
        cp Microsoft/config-wsl /out/.config
        scripts/config --file /out/.config \
            --set-str LOCALVERSION "-microsoft-standard-WSL2-pixav-redroid" \
            --disable LOCALVERSION_AUTO \
            --enable ANDROID_BINDER_IPC \
            --enable ANDROID_BINDERFS \
            --set-str ANDROID_BINDER_DEVICES "binder,hwbinder,vndbinder" \
            --enable DMABUF_HEAPS \
            --enable DMABUF_HEAPS_SYSTEM \
            --enable ISO9660_FS \
            --enable TUN \
            --enable LLC \
            --enable STP \
            --enable BRIDGE \
            --enable BRIDGE_NETFILTER \
            --enable IP_NF_IPTABLES \
            --enable IP_NF_FILTER \
            --enable IP_NF_NAT \
            --enable IP_NF_MANGLE \
            --enable IP_NF_RAW \
            --enable NETFILTER_XT_MATCH_ADDRTYPE \
            --enable NETFILTER_XT_MATCH_COMMENT \
            --enable NETFILTER_XT_MATCH_CONNTRACK \
            --enable NETFILTER_XT_MATCH_MULTIPORT \
            --enable NETFILTER_XT_NAT \
            --enable NETFILTER_XT_TARGET_MASQUERADE
        make O=/out olddefconfig
    '

docker run --rm --user "$(id -u):$(id -g)" \
    -e KBUILD_BUILD_USER=pixav \
    -e KBUILD_BUILD_HOST=phase0 \
    -e LOCALVERSION= \
    -e SOURCE_DATE_EPOCH="${source_epoch}" \
    -v "${source_abs}:/src:ro" \
    -v "${output_abs}:/out" \
    -w /src \
    pixav-wsl-kernel-builder:24.04 \
    make O=/out -j"$(nproc)" bzImage

artifact="${output_dir}/arch/x86/boot/bzImage"
"${source_dir}/scripts/extract-ikconfig" "${artifact}" > "${workspace}/embedded.config"
sha256sum "${artifact}" > "${workspace}/bzImage.sha256"

grep -E '^(CONFIG_LOCALVERSION=|CONFIG_ANDROID_BINDER_IPC=|CONFIG_ANDROID_BINDERFS=|CONFIG_ANDROID_BINDER_DEVICES=|CONFIG_DMABUF_HEAPS=|CONFIG_DMABUF_HEAPS_SYSTEM=|CONFIG_ISO9660_FS=|CONFIG_TUN=|CONFIG_LLC=|CONFIG_STP=|CONFIG_BRIDGE=|CONFIG_BRIDGE_NETFILTER=|CONFIG_IP_NF_IPTABLES=|CONFIG_IP_NF_FILTER=|CONFIG_IP_NF_NAT=|CONFIG_IP_NF_MANGLE=|CONFIG_IP_NF_RAW=|CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=|CONFIG_NETFILTER_XT_MATCH_COMMENT=|CONFIG_NETFILTER_XT_MATCH_CONNTRACK=|CONFIG_NETFILTER_XT_MATCH_MULTIPORT=|CONFIG_NETFILTER_XT_NAT=|CONFIG_NETFILTER_XT_TARGET_MASQUERADE=)' \
    "${workspace}/embedded.config"
file "${artifact}"
