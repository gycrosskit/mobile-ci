# SDK 由获授权管理员安装并只读挂载；公开镜像不包含或接受华为 SDK 许可。
ARG OHOS_JDK_IMAGE=eclipse-temurin:21.0.11_10-jdk-jammy@sha256:dbfd085220ae632a0830166e443747d1ee89e9038d92e3b48c3e5e9d8292b9a7
FROM ${OHOS_JDK_IMAGE}

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash ca-certificates curl file git openssh-client openssl perl procps python3 \
        unzip util-linux libatomic1 libxml2 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 jenkins \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash jenkins
COPY clt.py /usr/local/lib/ohos/clt.py
COPY start-ohos-agent.sh /usr/local/bin/start-ohos-agent
RUN chmod 0755 /usr/local/bin/start-ohos-agent
ENV HARMONY_CLT_HOME=/opt/harmony-clt
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/start-ohos-agent"]
