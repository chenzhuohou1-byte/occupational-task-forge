#!/bin/sh
# Java 21 + Maven 3.9 判分/考生环境（macOS arm64，不用 sudo、不进系统目录、不碰 ~/.m2）。
# 用法：sh setup_macos.sh <ROOT，默认 ~/ale-runtime> <要预取依赖的 Maven 工程目录（含 backend/pom.xml）> [额外测试类目录]
# 产出：ROOT/jdk21、ROOT/maven（conf/settings.xml 写 localRepository=ROOT/m2repo + 阿里云镜像）、ROOT/m2repo、
#       ROOT/java21-bin/{mvn,java}（考生沙箱用包装，见 README）、ROOT/java21.sh（判分侧 source 用）。
set -eu
ROOT="${1:-$HOME/ale-runtime}"; PROJ="${2:?要给一份能离线构建的工程目录}"; EXTRA_TESTS="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
JDK_TGZ=OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.13.1_2.tar.gz
JDK_SHA=48067f2b7c958ed104a9d8a5749be7d25220b0a356b4d2d1bcfaab3e16ad0b14   # api.adoptium.net 官方 checksum
MVN_TGZ=apache-maven-3.9.16-bin.tar.gz
MVN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6  # archive.apache.org
mkdir -p "$ROOT/dl/java" "$ROOT/jdk21" "$ROOT/maven" "$ROOT/m2repo" "$ROOT/java21-bin"
cd "$ROOT/dl/java"
[ -f "$JDK_TGZ" ] || curl -fsS -o "$JDK_TGZ" "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jdk/aarch64/mac/$JDK_TGZ"
[ -f "$MVN_TGZ" ] || curl -fsS -o "$MVN_TGZ" "https://mirrors.aliyun.com/apache/maven/maven-3/3.9.16/binaries/$MVN_TGZ"
echo "$JDK_SHA  $JDK_TGZ" | shasum -a 256 -c -
echo "$MVN_SHA512  $MVN_TGZ" | shasum -a 512 -c -
[ -x "$ROOT/jdk21/Contents/Home/bin/java" ] || tar -xzf "$JDK_TGZ" -C "$ROOT/jdk21" --strip-components=1
[ -x "$ROOT/maven/bin/mvn" ] || tar -xzf "$MVN_TGZ" -C "$ROOT/maven" --strip-components=1
xattr -dr com.apple.quarantine "$ROOT/jdk21" "$ROOT/maven" 2>/dev/null || true
S="$ROOT/maven/conf/settings.xml"
if ! grep -q "<localRepository>$ROOT/m2repo" "$S"; then
  [ -f "$S.orig" ] || cp "$S" "$S.orig"
  python3 - "$S.orig" "$S" "$ROOT/m2repo" <<'PY'
import sys
src, dst, repo = sys.argv[1:]
s = open(src).read()
s = s.replace("<pluginGroups>", "<localRepository>%s</localRepository>\n\n  <pluginGroups>" % repo, 1)
s = s.replace("<mirrors>", "<mirrors>\n    <mirror><id>aliyun-public</id><mirrorOf>*</mirrorOf>"
              "<url>https://maven.aliyun.com/repository/public</url></mirror>", 1)
open(dst, "w").write(s)
PY
fi
cp "$HERE/examinee-mvn" "$ROOT/java21-bin/mvn"; cp "$HERE/examinee-java" "$ROOT/java21-bin/java"
chmod +x "$ROOT/java21-bin/mvn" "$ROOT/java21-bin/java"
cat > "$ROOT/java21.sh" <<SH
export JAVA_HOME="$ROOT/jdk21/Contents/Home"
export PATH="\$JAVA_HOME/bin:$ROOT/maven/bin:\$PATH"
SH
# 在线预取：在工程副本上跑一遍完整构建 + 测试（含判分侧额外测试类），再 go-offline 补插件
W="$(mktemp -d)"; cp -R "$PROJ" "$W/p"
[ -n "$EXTRA_TESTS" ] && cp "$EXTRA_TESTS"/*.java "$W/p/backend/src/test/java/org/example/platform/"
cd "$W/p/backend"
JAVA_HOME="$ROOT/jdk21/Contents/Home" "$ROOT/maven/bin/mvn" -B -ntp clean verify
JAVA_HOME="$ROOT/jdk21/Contents/Home" "$ROOT/maven/bin/mvn" -B -ntp dependency:go-offline
# 离线复核：换一份干净副本 --offline 再跑一遍
rm -rf "$W/p/backend/target"
JAVA_HOME="$ROOT/jdk21/Contents/Home" "$ROOT/maven/bin/mvn" -B -ntp --offline -q package -DskipTests && echo "离线构建 OK"
rm -rf "$W"
du -sh "$ROOT/jdk21" "$ROOT/maven" "$ROOT/m2repo"
