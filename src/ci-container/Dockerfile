# SPDX-License-Identifier: GPL-2.0

FROM ubuntu:24.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential bc bison flex libelf-dev \
    libssl-dev qemu-system-x86 qemu-utils \
    minicom socat vde2 git ssh rsync brotli \
    lsb-release wget software-properties-common \
    gnupg clang bear pahole kmod swig quilt \
    libtool pkg-config libpython3-dev libmount-dev \
    libaio-dev libssl-dev libnl-genl-3-dev \
    libkeyutils-dev libyaml-dev libreadline-dev \
    module-assistant debhelper libsnmp-dev \
    mpi-default-dev curl libguestfs-tools \
    qemu-utils linux-image-generic ccache \
    minicom iproute2 net-tools nginx \
    fcgiwrap debootstrap rsync openssh-client \
    openssh-server ca-certificates

# Purge this stuff
RUN rm -rf /var/lib/apt/lists/*

# Expose HTTP and SSH ports
EXPOSE 80
EXPOSE 22

# Create ktest user and workspace
RUN useradd -m -s /bin/bash ktest

# Set up directories for ktest operations
RUN mkdir -p /home/ktest/linux/ \
    && chown -R ktest:ktest /home/ktest/

# Switch to ktest user for builds
USER ktest

# Grab Linux
RUN git clone https://github.com/tim-day-387/linux.git /home/ktest/linux

# Git config
RUN git config --global safe.directory '*'

# Set up directories for ktest operations
RUN mkdir -p /home/ktest/.ssh \
    && ssh-keygen -t rsa -b 4096 -f /home/ktest/.ssh/id_rsa -N "" \
    && cp /home/ktest/.ssh/id_rsa.pub /home/ktest/.ssh/authorized_keys \
    && chmod 600 /home/ktest/.ssh/authorized_keys \
    && chmod 700 /home/ktest/.ssh \
    && chown -R ktest:ktest /home/ktest/.ssh

# Set up working directory
WORKDIR /workspace

# Copy project files
COPY . /workspace/

# Switch back to root for service setup
USER root

# Copy startup, worker scripts, config
RUN mkdir -p /workspace/ktest-out \
    && mkdir -p /workspace/users
COPY src/ci-container/start-services.sh /usr/local/bin/
COPY src/ci-container/worker.sh /usr/local/bin/
COPY src/ci-container/ktest-ci.toml /etc/ktest-ci.toml
COPY src/ci-container/sample-user.toml /workspace/users/origin.toml

RUN chown -R ktest:ktest /workspace
RUN chmod -R 777 /workspace
RUN chown -R ktest:ktest /home/ktest/linux
RUN chmod -R 777 /home/ktest/linux

# Configure nginx for CGI
COPY src/ci-container/nginx.conf /etc/nginx/sites-available/ktest-ci
RUN rm /etc/nginx/sites-enabled/default \
    && ln -s /etc/nginx/sites-available/ktest-ci /etc/nginx/sites-enabled/ktest-ci

# Configure stuff with root
RUN mkdir -p /var/run/sshd \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && echo 'AllowUsers ktest' >> /etc/ssh/sshd_config \
    && mkdir -p /var/lib/ktest \
    && chown ktest:ktest /var/lib/ktest \
    && cp /workspace/target/release/* /bin/ 2>/dev/null || true \
    && mkdir -p /var/www/cgi-bin \
    && cp /workspace/target/release/cgi /var/www/cgi-bin/ \
    && chown www-data:www-data /var/www/cgi-bin/cgi \
    && chmod +x /var/www/cgi-bin/cgi \
    && chmod +x /usr/local/bin/start-services.sh \
    && chmod +x /usr/local/bin/worker.sh
