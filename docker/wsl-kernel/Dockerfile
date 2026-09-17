FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bc \
        binutils \
        bison \
        build-essential \
        ca-certificates \
        cpio \
        dwarves \
        flex \
        kmod \
        libelf-dev \
        libssl-dev \
        python3 \
        rsync \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

WORKDIR /src
