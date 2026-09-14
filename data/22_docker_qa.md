# Docker 容器化常见问题答疑

## A. 基础概念

### 1. 镜像和容器的区别？同一镜像能起多个容器吗？
镜像是**只读模板**（构建产物，含代码+依赖+系统库，相当于"类/安装包"）；容器是镜像跑起来的**运行实例**（相当于"对象/进程"）。同一个镜像可同时起任意多个互不影响的容器（名字、宿主端口不冲突即可）。`build` 造镜像，`run/up` 起容器，`rm/down` 删容器但镜像还在。

### 2. 容器和虚拟机的本质区别？为什么容器秒起？
VM 用 Hypervisor 虚拟硬件、每台装完整 Guest OS，重、分钟级启动、GB 级；容器**共享宿主机 Linux 内核**，只打包应用和依赖库，靠 namespace 做视图隔离、cgroup 做资源限制，本质是宿主机上被隔离的普通进程，所以秒起、MB 级、密度高。代价是隔离强度弱于 VM（共享内核）。

### 3. 只读层、可写层、写时复制（COW）
镜像由多层只读层叠加，`run` 时顶部加一层**容器可写层**。读文件从上往下联合查找；要改只读层文件时，先把它**复制到可写层再改副本，底层原件不动**，即写时复制。运行期改动都在可写层，`docker rm` 删容器即丢失，只读镜像层永不变；想永久固化要改 Dockerfile/源码后**重新 build** 出新镜像。

## B. Dockerfile 与构建

### 4. 为什么先 COPY requirements 再装依赖、最后才 COPY 代码？
每条指令产生一层，某层输入没变且下层没变就命中缓存（CACHED），某层失效会让其后所有层重建。依赖不常变、代码常变，所以先单独拷 requirements 并 pip install（长期命中缓存），最后才拷全部代码（只让最上层随代码变）。反过来写，每改一行代码都重装全部依赖，构建极慢。原则：**变化越少的层越靠前**。

### 5. 为什么 Dockerfile 拆两轮 pip install？（真实踩坑）
全新构建时 pip 一次性解析发现 `langchain 1.2.12` 要求 `langgraph>=1.1.1,<1.2.0`，与目标 `langgraph==1.2.11` 冲突（ResolutionImpossible）。本地 conda 能跑是分步安装、后装的 1.2.11 覆盖了约束。Dockerfile 复刻：第一轮装 requirements（经 langchain 拉入 1.1.x），第二轮单独装 langgraph==1.2.11 + checkpoint-postgres 覆盖锁定，仅剩可忽略 warning。教训：**本地能跑 ≠ 全新构建能过，全新环境是一次性解析依赖**。

### 6. .dockerignore 和 .gitignore 的区别？为什么 .env 两个都写？
`.gitignore` 决定文件**不进 Git**；`.dockerignore` 决定 build 时文件**不发给 Docker、不进镜像**（缩小构建上下文、防泄密）。`.env` 两个都写：前者防推 GitHub 泄露，后者防打进镜像随分发泄露。密钥改在运行时用环境变量注入；同时提交不含真值的 `.env.example` 当模板。

### 7. -p 端口、0.0.0.0、EXPOSE
`-p 左:右`，左=**宿主机**端口，右=**容器内**端口，做转发。服务只绑 127.0.0.1 时只接受容器内部访问，外部映射进不来，必须绑 `0.0.0.0`（所有网卡），所以 uvicorn 写 `--host 0.0.0.0`。`EXPOSE 8000` 只是文档声明、**不会真正放行**，真正对外靠 `run -p` 或 compose 的 `ports`。

## C. 运行时与网络

### 8. localhost / host.docker.internal / 服务名
每个容器有独立网络栈，容器内 `localhost` 指**容器自己**。
- 连**宿主机**上的服务（方式 A）：`host.docker.internal`（Docker Desktop 内置域名）；
- 连同 compose 的**兄弟容器**（方式 B）：用**服务名**（如 `postgres`），compose 自建内网 + 内置 DNS 维护"服务名→容器 IP"。
容器重建 IP 会变、服务名不变，所以永远写名字不写死 IP。

### 9. 密钥为什么运行时注入？--env-file 和 -e 谁覆盖谁？
镜像是会被分发的死物，烤进密钥会永久泄露、难轮换；运行时注入让同一份镜像在不同环境/不同人手里用不同 Key，换 Key 不用重新 build。同时用 `--env-file .env` 和 `-e` 时，**`-e` 优先级更高，覆盖 env-file 同名变量**（项目里 .env 是 localhost，再用 -e 覆盖成 host.docker.internal / standalone）。

### 10. 方式 A vs 方式 B
方式 A：只容器化 app，依赖宿主机已有 PG/Milvus（host.docker.internal），适合本地快速验证。方式 B：compose 把 app+PG+Milvus(+etcd/minio) 全容器化、服务名互联、healthcheck 控序、卷持久化，一条命令从零拉起、不依赖宿主机预装，适合交付/部署，推荐用于交付部署。

## D. 存储

### 11. 数据为什么会丢？volume / bind mount / down -v
容器写文件默认进可写层、删容器即丢。volume 把容器内目录映射到外部，写入绕开可写层落到宿主机，容器删数据还在。
- **bind mount**：挂指定宿主机具体路径（项目用 `./volumes/pgdata`，资源管理器可见，开发常用）；
- **named volume**：Docker 统一管理位置，可移植性好、偏生产。
`down` 删容器和网络、保留卷数据；`down -v` 额外删 named volume，但删不掉 bind mount 的宿主机目录（要重置手动删 `volumes/xxx`）。代码烤进镜像可重建、**数据是唯一无法重建的东西**，所以只给数据库挂卷。

## E. compose 编排

### 12. docker run vs docker compose
run 是命令式、一次管一个容器，多容器要自己建网、逐条 run、自己等依赖。compose 是声明式：一份 yaml 描述"期望的整套状态"，自动建内网、DNS 服务发现、按 depends_on 排序、healthcheck 等待、统一生命周期（up/down/ps/logs），yaml 可进 git、可复现。对应关系：`--name→container_name`、`-p→ports`、`--env-file→env_file`、`-e→environment`、`-v→volumes`。compose 起的仍是普通容器，docker ps/logs/exec 照样能用。

### 13. depends_on 两种条件 / healthcheck 三态 / 边界
只写服务名或 `service_started` 只保证对方**进程已启动**（Up 瞬间），但数据库还要几秒初始化才能连；`service_healthy` 要求健康检查连续通过才放行。三态：`starting → healthy / unhealthy`（连续成功/失败达 retries 次才翻转）。healthcheck 是**启动门禁+状态标签，不管运行中途崩溃**，运行期依赖挂了得靠应用层重试。Milvus 只用 started，是因为 app 启动期不连它、且它健康稳定要 90 秒，不值得干等。

### 14. 为什么 postgres 映射 5433？etcd/minio 为什么不映射？
宿主机本地 PG 已占 5432，compose pg 再映射宿主机 5432 会冲突，故对外用 5433（app 内网仍走 `postgres:5432`，不经过该映射）。etcd/minio 只给内网 Milvus 用、无需宿主机访问，按最小暴露原则不映射，ps 里只显示内部端口。

## F. 交付与排错

### 15. 别人怎么跑你的项目？要推 Docker Hub 吗？
开源走"给代码"：clone → 复制 `.env.example` 为 `.env` 填**自己的 Key** → `docker compose up -d --build` 本地现构建、整套拉起，**不需要推镜像仓库**。推仓库（build 换成 image:仓库地址）是闭源产品化/CI-CD 路径。对方自备：Docker 环境 + 自己的 API Key；PG/Milvus 由 compose 自动起，无需安装。

### 16. 排错三步（从粗到细的定位漏斗）
① `docker compose ps`（或 `docker ps -a`）：定位**谁**出问题（没起/重启/不健康）；
② `docker logs 容器名`（或 `docker compose logs 服务名`）：看**报什么错**，多数问题到此解决；
③ `docker exec -it 容器 bash`：日志只给表象时（如 connection refused），进去亲手查环境变量、配置、到依赖的网络连通性，定位**根因**。
轻症第二步确诊，重症才进第三步。

### 17. 为什么 C 盘目录下 compose ps 报 no configuration file，docker ps 却正常？
`docker compose` 是项目级命令，必须在**含 docker-compose.yml 的目录**执行（或 `-f` 指定路径），站在没有该文件的目录它不知道管哪套；`docker ps` 是全局命令、直接问 Docker 引擎，任何目录都能列所有容器。

## G. 综合与拓展

### 18. clone → /docs 全链路（四阶段见第二部分第 4 节）
clone、填 .env；`up --build` 先构建 app 镜像（拉基础镜像→装库→拷代码），compose 同时建内网、起依赖、等 pg healthy 再起 app；app 启动用服务名连内网 pg 容器并建表、准备连 Milvus，uvicorn 绑 0.0.0.0:8000；经 `-p 8000:8000` 转到宿主机，浏览器开 `127.0.0.1:8000/docs`。密钥运行时注入、数据落 volumes 卷。

### 19. Docker / compose / K8s 关系
Docker 管"构建镜像、运行单容器"；compose 管**单机**一组容器；K8s 管**跨多机集群**的大量容器，提供调度、自愈、弹性伸缩、滚动更新/回滚、服务发现/负载均衡。概念对应：容器→**Pod**、compose service→**Deployment**、内网服务名→**Service**、对外域名路由→Ingress。三者都是声明式 yaml。实习阶段 Docker+compose 会动手，K8s 到"懂概念、知道 Pod/Deployment/Service"即可。

---

# 第二部分 · 易混点深挖（答疑）

## 1. 端口冲突的本质：两个层面的端口别叠在一起
- 端口是**操作系统层面**的资源，同一台机器同一端口号同一时间只能被一个进程监听。
- 存在两个层面的端口：**宿主机层面**（本机 PG 已占宿主机 5432）和**容器层面**（pg 容器在自己网络命名空间内监听 5432），二者互不冲突。
- 冲突只发生在 `-p 左边:右边` 的**左边（宿主机侧）**：写 `5432:5432` 要在宿主机占 5432，与本机 PG 抢端口 → `port already allocated`；写 `5433:5432` 用宿主机没人占的 5433 转发进容器 5432，不冲突。

```
宿主机窗口 5432  →  本机装的 PostgreSQL（早就占着）
宿主机窗口 5433  →  转发进 →  pg 容器内部的 5432（新开，不冲突）
```
- app 走内网 `postgres:5432`，**不经过宿主机 5433**；5433 只是给"宿主机想用 Navicat/psql 连容器库"留的门。
- 类比：一栋楼（宿主机）5432 门牌已被本地 PG 挂了，容器 PG 改挂 5433；但进店后它内部窗口仍叫 5432。

## 2. 挂载数据 ≠ 自己装 PostgreSQL（服务进程 vs 数据目录）
- **数据库服务进程**跑在 **postgres 容器里**，compose 自动起，对方电脑**无需安装 PostgreSQL**；
- **数据目录 `./volumes/pgdata`** 只是宿主机磁盘上的**普通文件夹**（Docker 自动建），和"装没装数据库软件"无关。
- 挂载 = 让容器里的 PG 进程把数据写进宿主机这个普通文件夹。首次为空文件夹时，PG 容器自动 initdb 初始化（日志可见 `CREATE DATABASE`）。
- 类比：不用自己先买冰箱（装 PG），compose 送来一台冰箱（PG 容器），你只在墙上腾个格子（普通文件夹）给它放东西，格子本身不需要是冰箱。

## 3. 排错三步是"漏斗"，不是二选一
- `ps` 回答"**谁**病了"（把范围从整套缩到某个容器）；
- `logs` 回答"**什么错**"（化验单，80% 问题到此解决）；
- `exec` 回答"**为什么**"（日志只给表象时，进容器亲手验证网络/认证/配置）。
- 日志已把根因说死（语法错、缺包）→ 不用 exec；日志只给现象（refused/timeout）→ exec 继续挖依赖。

## 4. 全链路四阶段 + uvicorn 是什么 + 连的是哪个库

**(1) 连的是哪个库？** 方式 B 连的是 **compose 内网的 postgres 兄弟容器**（服务名 `postgres`），不是宿主机、不是外部库；只有方式 A 才用 host.docker.internal 连宿主机的库。

**(2) 服务名怎么找到容器？** compose 内置 DNS 把 `postgres`/`standalone` 解析成对应容器的内网 IP。

**(3) uvicorn 和 FastAPI 的关系：**
- FastAPI 是 Web **框架**（写路由/接口逻辑，即 api.py 里的 app），自己不会监听端口；
- uvicorn 是 **ASGI Web 服务器进程**，把 FastAPI 加载起来、监听端口、收 HTTP 请求转给框架；
- `CMD ["uvicorn","api:app","--host","0.0.0.0","--port","8000"]` = 容器启动用 uvicorn 加载 api.py 的 app 并绑 8000。
- 类比：FastAPI 是"菜"，uvicorn 是"端菜上桌、对外营业的店面+服务员"。

**(4) 四阶段归属表：**

| 阶段 | 触发 | 干的事 |
|---|---|---|
| ① 构建期 | `up --build` | 拉 python:3.13-slim、apt 装库、两轮 pip、拷代码 → 造出 app 镜像（无业务在跑） |
| ② 编排拉起期 | compose | 建内网 → 起 etcd/minio/standalone/pg → 等 pg 变 healthy |
| ③ app 启动期 | pg healthy 后放 app | 执行 CMD 起 uvicorn → FastAPI lifespan 用服务名连 pg 容器并建 4 张表、准备连 standalone |
| ④ 对外访问期 | 浏览器 | uvicorn 绑 0.0.0.0:8000，经 -p 转到宿主机，访问 127.0.0.1:8000/docs |

一句话：**构建期造镜像 → 编排期按序起依赖 → app 启动期用服务名连内网库/向量库、由 uvicorn 把 FastAPI 跑起来 → 端口映射让宿主机浏览器能访问。**

---

# 第三部分 · 一页速记卡

- 镜像=只读模板/类，容器=运行实例/对象；容器共享内核、秒起，VM 虚拟完整 OS、重。
- 分层缓存：越不常变的层越靠前；改代码只让最后的 COPY 层失效。
- 只读层永不改，改动进容器可写层（COW），删容器即丢；永久改要重新 build。
- 网络：连自己 localhost、连宿主机 host.docker.internal、连兄弟容器用服务名（compose DNS）。
- 密钥：.gitignore + .dockerignore 双挡，运行时 env 注入；-e 覆盖 --env-file。
- 数据：只给数据库挂卷；bind mount=指定宿主目录，named volume=Docker 托管；down 留数据。
- compose：声明式管一整套，自动建网/DNS/排序/健康检查；run 是命令式管一个。
- healthcheck：started=进程起了，healthy=真能用；三态 starting/healthy/unhealthy；只管启动顺序。
- 端口：-p 宿主:容器；冲突只在宿主侧；服务绑 0.0.0.0 外部才可达；EXPOSE 不放行。
- 交付：开源给代码+compose，别人填自己 Key 后 up --build，不必推镜像仓库。
- 排错：ps 找谁 → logs 看什么错 → exec 挖根因。
- K8s：跨机集群编排（调度/自愈/伸缩/滚动更新）；Pod≈容器、Deployment≈service、Service≈内网服务名。
