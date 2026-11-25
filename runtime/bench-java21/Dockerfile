# bench-java21:mvn3.9.16-v1 —— Java 21 + Maven 3.9.16 + 预取的离线依赖（外部专家题 ale-001）。
# 尚未构建过（2026-11-25 本机 docker 守护进程未开），见同目录 README.md。
# 构建（上下文目录里放一份能构建的工程 project/ 和判分侧额外测试类 extra_tests/）：
#   docker build -t bench-java21:mvn3.9.16-v1 -f runtime/bench-java21/Dockerfile <上下文目录>
FROM eclipse-temurin:21-jdk-noble
ARG MVN_VERSION=3.9.16
ARG MVN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl && rm -rf /var/lib/apt/lists/* \
 && curl -fsS -o /tmp/mvn.tgz https://archive.apache.org/dist/maven/maven-3/${MVN_VERSION}/binaries/apache-maven-${MVN_VERSION}-bin.tar.gz \
 && echo "${MVN_SHA512}  /tmp/mvn.tgz" | sha512sum -c - \
 && mkdir -p /opt/maven && tar -xzf /tmp/mvn.tgz -C /opt/maven --strip-components=1 && rm /tmp/mvn.tgz \
 && ln -s /opt/maven/bin/mvn /usr/local/bin/mvn
ENV MAVEN_ARGS="-Dmaven.repo.local=/opt/m2repo"
COPY project /tmp/prefetch/project
COPY extra_tests /tmp/prefetch/extra_tests
RUN cp /tmp/prefetch/extra_tests/*.java /tmp/prefetch/project/backend/src/test/java/org/example/platform/ \
 && cd /tmp/prefetch/project/backend && mvn -B -ntp clean verify && mvn -B -ntp dependency:go-offline \
 && rm -rf /tmp/prefetch/project/backend/target && mvn -B -ntp --offline -q package -DskipTests \
 && rm -rf /tmp/prefetch && chmod -R a+rX /opt/m2repo
