ARG JENKINS_AGENT_IMAGE=jenkins/inbound-agent:3385.vf1123fb_515da_-1-jdk21@sha256:6dbeabd484bb3926293de0b337117f7ad8bb2c0ba1a425b3e34e3d3983b5e1f9
FROM ${JENKINS_AGENT_IMAGE}

ARG ANDROID_PLATFORM_VERSION=36
ARG ANDROID_BUILD_TOOLS_VERSION=35.0.0
ARG ANDROID_NDK_VERSION=25.1.8937393
ARG ANDROID_COMMAND_LINE_TOOLS_VERSION=15859902
ARG ANDROID_COMMAND_LINE_TOOLS_SHA256=4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583
ARG CI_GROOVY_VERSION=2.4.21
ARG CI_GROOVY_SHA256=de65260cf2070442e99882f2f3d72e7531725c1e6a257446cc0cea525c607bd0
ARG DWS_VERSION=1.0.61
ARG DWS_LINUX_AMD64_SHA256=a507356bc19edc4b868398bd99c803c81480c2e094b9efbb6ad74e2905089a4d
ARG DEBIAN_MIRROR=https://mirror.nju.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirror.nju.edu.cn/debian-security

ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    DWS_CONFIG_DIR=/home/jenkins/.dws \
    DWS_LANG=zh \
    GROOVY_TEST_CLASSPATH=/usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar \
    PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:${PATH}

USER root

RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i \
            -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${DEBIAN_MIRROR}|g" {} + \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        docker-cli \
        docker-compose \
        file \
        git \
        nodejs \
        openssh-client \
        procps \
        python3 \
        ruby \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# 门禁与 Controller 使用同一 Groovy；候选工作区不能替换测试解释器。
RUN install -d -o root -g root -m 0755 /usr/local/lib/controller-groovy \
    && curl --fail --silent --show-error --location \
        "https://repo.maven.apache.org/maven2/org/codehaus/groovy/groovy-all/${CI_GROOVY_VERSION}/groovy-all-${CI_GROOVY_VERSION}.jar" \
        --output "/usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar" \
    && echo "${CI_GROOVY_SHA256}  /usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar" | sha256sum --check - \
    && chown root:root "/usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar" \
    && chmod 0644 "/usr/local/lib/controller-groovy/groovy-all-${CI_GROOVY_VERSION}.jar"

RUN curl --fail --silent --show-error --location \
        "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_COMMAND_LINE_TOOLS_VERSION}_latest.zip" \
        --output /tmp/android-command-line-tools.zip \
    && echo "${ANDROID_COMMAND_LINE_TOOLS_SHA256}  /tmp/android-command-line-tools.zip" | sha256sum --check - \
    && mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools/latest" \
    && unzip -q /tmp/android-command-line-tools.zip -d /tmp/android-command-line-tools \
    && mv /tmp/android-command-line-tools/cmdline-tools/* "${ANDROID_SDK_ROOT}/cmdline-tools/latest/" \
    && rm -rf /tmp/android-command-line-tools /tmp/android-command-line-tools.zip

RUN curl --fail --silent --show-error --location \
        "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/releases/download/v${DWS_VERSION}/dws-linux-amd64.tar.gz" \
        --output /tmp/dws-linux-amd64.tar.gz \
    && echo "${DWS_LINUX_AMD64_SHA256}  /tmp/dws-linux-amd64.tar.gz" | sha256sum --check - \
    && mkdir -p /tmp/dws-release \
    && tar -xzf /tmp/dws-linux-amd64.tar.gz -C /tmp/dws-release \
    && find /tmp/dws-release -type f -name dws \
        -exec install -m 0755 {} /usr/local/bin/dws \; \
    && test -x /usr/local/bin/dws \
    && /usr/local/bin/dws version | grep --fixed-strings "v${DWS_VERSION}" \
    && rm -rf /tmp/dws-release /tmp/dws-linux-amd64.tar.gz

RUN yes | sdkmanager --licenses >/dev/null \
    && sdkmanager \
        "platform-tools" \
        "platforms;android-${ANDROID_PLATFORM_VERSION}" \
        "build-tools;${ANDROID_BUILD_TOOLS_VERSION}" \
        "ndk;${ANDROID_NDK_VERSION}" \
    && chown -R jenkins:jenkins "${ANDROID_SDK_ROOT}"

# DWS 的官方迁移包包含 config/ 与 keychain/dws-cli/。二者收拢到同一数据卷，
# 再从 DWS 默认路径建立软链接，确保认证状态可随 Jenkins 迁移包恢复。
RUN install -d -o jenkins -g jenkins \
        /home/jenkins/.dws-state/config \
        /home/jenkins/.dws-state/keychain/dws-cli \
        /home/jenkins/.local/share \
    && ln -s .dws-state/config /home/jenkins/.dws \
    && ln -s ../../.dws-state/keychain/dws-cli /home/jenkins/.local/share/dws-cli \
    && chown -h jenkins:jenkins \
        /home/jenkins/.dws \
        /home/jenkins/.local/share/dws-cli

COPY --chmod=0755 start-android-agent.sh /usr/local/bin/start-android-agent
COPY --chmod=0644 gitee-known-hosts /etc/ssh/ssh_known_hosts

RUN ssh-keygen -F gitee.com -f /etc/ssh/ssh_known_hosts \
        | ssh-keygen -lf - \
        | grep --fixed-strings 'SHA256:+ULzij2u99B9eWYFTw1Q4ErYG/aepHLbu96PAUCoV88'

USER jenkins
ENTRYPOINT ["/usr/local/bin/start-android-agent"]
