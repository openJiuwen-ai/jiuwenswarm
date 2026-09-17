FROM openeuler/openeuler:24.03

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN echo "sslverify=false" >> /etc/yum.conf
RUN echo "insecure" >> ~/.curlrc
RUN sed -i 's/gpgcheck=1/gpgcheck=0/g' /etc/yum.conf /etc/yum.repos.d/openEuler.repo

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

EXPOSE 8321
EXPOSE 8322

# 默认走 HTTP; UDS 模式由调用方覆盖 JIUWENBOX_LISTEN, 例如
# `docker run -e JIUWENBOX_LISTEN=unix:///run/jiuwenbox/jiuwenbox.sock \
#             -v /tmp/jiuwenbox-sock:/run/jiuwenbox jiuwenbox`。
ENV JIUWENBOX_LISTEN=http://0.0.0.0:8321
CMD ["python3", "-m", "jiuwenbox.server.launcher"]
