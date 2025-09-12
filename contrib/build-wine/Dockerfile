FROM debian:bullseye

# need ca-certificates before using snapshot packages
RUN apt update -qq > /dev/null && apt install -qq --yes --no-install-recommends \
    ca-certificates

ENV LC_ALL=C.UTF-8 LANG=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive

RUN dpkg --add-architecture i386 && \
    apt-get update -q && \
    apt-get install -qy --allow-downgrades \
        wget \
        gnupg2 \
        dirmngr \
        python3-software-properties \
        software-properties-common \
        git \
        p7zip-full \
        make \
        mingw-w64 \
        mingw-w64-tools \
        autotools-dev \
        autoconf \
        autopoint \
        libtool \
        gettext \
        sudo \
        nsis \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

RUN wget -nc https://dl.winehq.org/wine-builds/Release.key && \
        apt-key add Release.key && \
        rm Release.key && \
    wget -nc https://dl.winehq.org/wine-builds/winehq.key && \
        apt-key add winehq.key && \
        rm winehq.key && \
    apt-add-repository https://dl.winehq.org/wine-builds/debian/ && \
    apt-get update -q && \
    apt-get install -qy --allow-downgrades \
        wine-stable-amd64:amd64 \
        wine-stable-i386:i386 \
        wine-stable:amd64 \
        winehq-stable:amd64 \
        libvkd3d1:amd64 \
        libvkd3d1:i386 \
        && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get autoremove -y && \
    apt-get clean

# create new user to avoid using root; but with sudo access and no password for convenience.
ARG UID=1000
ENV USER="user"
ENV HOME_DIR="/home/${USER}"
ENV WORK_DIR="${HOME_DIR}/wspace" \
    PATH="${HOME_DIR}/.local/bin:${PATH}"
RUN useradd --uid $UID --create-home --shell /bin/bash ${USER}
RUN usermod -append --groups sudo ${USER}
RUN echo "%sudo ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
WORKDIR ${WORK_DIR}
RUN chown --recursive ${USER} ${WORK_DIR}
RUN chown ${USER} /opt
USER ${USER}

RUN mkdir --parents "/opt/wine64/drive_c/electrum"
