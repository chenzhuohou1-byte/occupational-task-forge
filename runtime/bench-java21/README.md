# bench-java21：Java 21 + Maven 3.9 + 离线依赖（外部专家题 ale-001 的判分与考生环境）

ale-001「多组织专家管理系统越权修复」是 Java 21 / Spring Boot 3.5.16 后端工程题。判分（专家 `rubrics/grade.py`）
在临时副本里跑 `mvn --batch-mode --offline`，考生也要能离线编译、跑测试。本目录给出在别的机器上复现这套环境的方法。

## 组成（2026-11-25 本机实测）

| 组件 | 版本 | 来源与校验 |
|---|---|---|
| JDK | Eclipse Temurin 21.0.13.1+2，macOS aarch64 tar.gz | 清华 Adoptium 镜像；sha256 与 api.adoptium.net 官方 checksum 一致（写在 `setup_macos.sh`） |
| Maven | 3.9.16 bin.tar.gz | 阿里云 Apache 镜像；sha512 与 archive.apache.org 一致 |
| 依赖仓库 | `m2repo/`，约 240 MB | 在线跑一遍参考解 `mvn clean verify`（含两个隐藏探针测试类）+ `dependency:go-offline`，走阿里云 maven 镜像 |

体积：jdk21 335 MB、maven 11 MB、m2repo 约 240 MB，合计不到 600 MB。不用 sudo、不进系统目录、不碰 `~/.m2`
（`localRepository` 写在 `maven/conf/settings.xml`，原文件备份为 `settings.xml.orig`）。

## 复现（macOS arm64）

```bash
sh runtime/bench-java21/setup_macos.sh ~/ale-runtime <任务目录>/assets/reference/reference/project <任务目录>/assets/reference/rubrics/tests
```

第二个参数是任何一份能构建的工程副本（用题包参考解），第三个参数是判分侧要注入的额外测试类目录，
它们引入的测试依赖也要进离线仓库。脚本最后会用 `--offline` 再构建一次确认离线可用。

判分：卡上 `evaluation.bridge.tools` 默认找 `~/ale-runtime/{jdk21/Contents/Home,maven/bin/mvn,m2repo}`，
别处安装用环境变量 `ALE_JAVA21_HOME` / `ALE_MVN` / `ALE_M2REPO` 覆盖。

## 考生沙箱（macOS sandbox-exec）里用

`run_python` 的沙箱禁读家目录、禁网络、只许写 `output/` 与 `_scratch/`。把这几个目录加进只读放行、把包装脚本目录加进 PATH：

```bash
source ~/ale-runtime/env.sh
R=~/ale-runtime
ALE_RUN_SOFTWARE="$ALE_RUN_SOFTWARE:$R/jdk21:$R/maven:$R/m2repo:$R/java21-bin" \
ALE_RUN_PATH="$R/java21-bin:$R/jdk21/Contents/Home/bin:$ALE_RUN_PATH" \
ALE_RUN_RUNTIME="py3.12-sci-v3+java21-mvn3.9.16" ... python3 n7_run.py ...
```

`java21-bin/mvn`（本目录 `examinee-mvn`）解决沙箱里的两个坑，实测缺一个 `mvn test` 都是 92/96 报错：

1. macOS 的 JDK 不认 `TMPDIR`，`java.io.tmpdir` 默认 `/var/folders/.../T`，沙箱里不可写 → 钉到 `TMPDIR`（run_python 给的是 `_scratch/`）。
2. Mockito 5 的 inline mock maker 默认让 JVM「自附加」Byte Buddy agent，attach 走 unix socket，被沙箱的 `deny network*` 挡掉
   （`Could not self-attach to current VM`）→ 启动时直接 `-javaagent:` 挂 `byte-buddy-agent-1.17.8.jar`（Mockito 官方推荐的 JDK 21 用法）。

两项都经 `JAVA_TOOL_OPTIONS` 传给 surefire 派生的测试 JVM。只读的 `m2repo` 加 `-o` 可用：离线构建不往本地仓库写解析状态文件
（冒烟后 `find m2repo -newer` 为空）。判分侧不经过包装（grade.py 在宿主上跑、不在沙箱里），桥接包装会清掉 `JAVA_TOOL_OPTIONS`。

冒烟（同一份原始 repo，沙箱内）：`mvn -v` 正常；`mvn -o -q package -DskipTests` 5 秒；`mvn -o test` 96 个测试全过、11–19 秒。

## Linux / Docker

题包 `task.json` 写的评测环境是 Linux x86_64 + Temurin 21 + Maven 3.9+。`Dockerfile` 是对应的镜像定义（`eclipse-temurin:21-jdk` +
Maven 3.9.16 + 构建时在线预取依赖），**本机 docker 守护进程没开，尚未构建过**；第一次用的人构建后跑一次参考解判分，确认 100 分再登记。
