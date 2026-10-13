FROM jenkins/jenkins:2.568.3-lts-jdk21

COPY jenkins/controller/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt --latest=false --list \
    > /usr/share/jenkins/ref/plugins-resolved.txt \
    && cat /usr/share/jenkins/ref/plugins-resolved.txt

COPY jenkins/controller/init.groovy.d/05-signing-portal.groovy /usr/share/jenkins/ref/init.groovy.d/05-signing-portal.groovy
COPY jenkins/controller/signing-portal/ /usr/share/jenkins/ref/signing-portal/
COPY jenkins/controller/init.groovy.d/04-ci-auto-promotion.groovy /usr/share/jenkins/ref/init.groovy.d/04-ci-auto-promotion.groovy
COPY jenkins/controller/CiAutoPromotion.groovy /usr/share/jenkins/ref/ci-auto-promotion/CiAutoPromotion.groovy
COPY jenkins/template-bindings.json /usr/share/jenkins/ref/ci-auto-promotion/template-bindings.json

# 实例配置由管理员安装到持久 Home；通用镜像不携带公司 profile 或示例账号。
