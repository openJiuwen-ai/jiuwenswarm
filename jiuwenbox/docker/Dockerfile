FROM openeuler/openeuler:24.03

ARG ARCH
# get by: ping mirrors.huaweicloud.com
ARG ARM_MIRRORS_IP

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PROGRESS_BAR=off

RUN echo "sslverify=false" >> /etc/yum.conf
RUN echo "insecure" >> ~/.curlrc
RUN sed -i 's/gpgcheck=1/gpgcheck=0/g' /etc/yum.conf /etc/yum.repos.d/openEuler.repo

# 只在 arm64 架构下执行这些步骤
RUN if [ "$ARCH" = "arm64" ]; then \
        sed -i '/^mirrorlist=/d' /etc/yum.repos.d/openEuler.repo && \
        sed -i '/^metalink=/d' /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[OS\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/OS/\\\$basearch|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[everything\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/everything/\\\$basearch|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[EPOL\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/EPOL/main/\\\$basearch|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[debuginfo\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/debuginfo/\\\$basearch|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[source\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/source|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[update\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/update/\\\$basearch|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i "/^\[update-source\]/,/^\[/ s|^baseurl=.*|baseurl=http://${ARM_MIRRORS_IP}/openeuler/openEuler-24.03-LTS-SP3/update/source|g" /etc/yum.repos.d/openEuler.repo && \
        sed -i 's/^gpgcheck=1/gpgcheck=0/' /etc/yum.repos.d/openEuler.repo && \
        sed -i 's/^sslverify=1/sslverify=0/' /etc/yum.repos.d/openEuler.repo && \
        cat /etc/yum.repos.d/openEuler.repo && \
        yum clean all && yum makecache; \
    else \
        echo "Skipping ARM-specific yum configuration for ARCH=$ARCH"; \
    fi

RUN yum makecache
# JDK 17 uses the headless + devel pair (BiSheng OpenJDK 17 on openEuler 24.03):
# headless gives `java` without X11 deps, devel gives `javac`/`jar`.
# Verified on 205 (aarch64).
RUN yum install -y \
        bubblewrap \
        iproute \
        iptables \
        iptables-nft \
        nftables \
        ca-certificates \
        nodejs \
        python3-pip \
        git \
        vim \
        unzip \
        java-17-openjdk-headless \
        java-17-openjdk-devel \
    && yum clean all \
    && rm -rf /var/cache/yum

# JAVA_HOME points at the stable, version-independent symlink
# (/usr/lib/jvm/java-17-openjdk -> /etc/alternatives/java_sdk_17_openjdk -> the
# versioned dir), so minor JDK upgrades do not break the path. java/javac/jar
# are also on PATH via /usr/bin alternatives, but prepending $JAVA_HOME/bin
# makes the JDK the authoritative one.
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY docker/requirements.txt ./docker/requirements.txt
COPY src ./src
COPY src/jiuwenbox/configs /app/configs

RUN pip install --break-system-packages --upgrade -i https://pypi.tuna.tsinghua.edu.cn/simple build setuptools wheel \
    && pip install --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple -r ./docker/requirements.txt \
    && python3 -m build --wheel --no-isolation \
    && pip install --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple dist/jiuwenbox-*.whl

RUN ln -s /usr/bin/python3 /usr/bin/python

RUN mkdir -p /opt/jiuwenbox-pip && \
    chown app:app /opt/jiuwenbox-pip

EXPOSE 8321
EXPOSE 8322

# 默认走 HTTP; UDS 模式由调用方覆盖 JIUWENBOX_LISTEN, 例如
# `docker run -e JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
#             -v /tmp/jiuwenbox-sock:/run/jiuwenbox jiuwenbox`。
ENV JIUWENBOX_LISTEN=http://0.0.0.0:8321
CMD ["python3", "-m", "jiuwenbox.server.launcher"]
