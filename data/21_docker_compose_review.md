# Docker Compose 多服务编排与排错复盘

## 一、阶段目标与最终达成

### 学到什么程度（三层边界）
- **会用（必须）**：写 Dockerfile / docker-compose.yml，build、run、ps、logs、exec、down，能把自己的 FastAPI 项目容器化并跑通。
- **需要理解的原理**：镜像 vs 容器、分层缓存、容器网络（localhost / host.docker.internal / 服务名）、运行时注入、数据卷持久化、healthcheck 启动顺序、依赖冲突解决。
- **了解即可**：镜像推仓库、K8s、多阶段构建优化、docker network 原理细节。

### 最终成果
1. 新增 3 个文件：`Dockerfile`、`requirements.txt`、`.dockerignore`；改造 `docker-compose.yml`（原本只有 Milvus，追加了 PostgreSQL + app）。
2. **方式 A 跑通**：单容器 app，连宿主机已有的 PostgreSQL / Milvus。
3. **方式 B 跑通**：compose 一键拉起 5 个容器（app + postgres + milvus/etcd/minio），服务名互联、healthcheck 控序、数据卷持久化，`down → up` 后数据不丢。
4. 解决了一个真实依赖冲突（langchain vs langgraph 版本约束）。
5. 提交 git：tag `v8.0`（阶段 8：Docker 容器化部署）。

---

## 二、动手前的项目现状探查（先搞清楚再写）

| 探查项 | 结论 |
|---|---|
| 已有 docker-compose.yml？ | 有，但只编排了 Milvus standalone（etcd + minio + standalone），**没有 Dockerfile、没有 requirements.txt** |
| 应用入口 | `api.py`（FastAPI + uvicorn），lifespan 启动时连 PostgreSQL 并 `setup()` 自动建 checkpoint 表 |
| 依赖怎么来的 | 用解释器 `pip list` 取真实版本，锁定进 requirements |
| 外部依赖 | PostgreSQL（checkpoint 持久化）、Milvus（向量库）、DeepSeek/Tavily/DashScope 三个云 API（靠 Key） |
| 环境变量 | `.env.example` 列了 DEEPSEEK/TAVILY/DASHSCOPE key、MILVUS_HOST/PORT、POSTGRES_DSN |

> 经验：不要凭记忆写 requirements，用当前能跑通的环境 `pip list`/`pip freeze` 取真实版本，否则"我本地能跑"到镜像里就崩。

---

## 三、核心概念（形象类比回顾）

- **镜像（Image）**：只读模板，像"自热火锅包装盒"或"类"。`docker build` 造出来。
- **容器（Container）**：镜像跑起来的实例，像"把自热火锅点燃吃上"或"对象"。同镜像可起多个容器。
- **Dockerfile**：造镜像的菜谱，一行一个步骤，每步产生一层（layer）。
- **分层缓存**：某层及其上游没变就直接复用（输出 CACHED）。所以把"不常变的依赖安装"放前面、"常变的代码 COPY"放最后。
- **端口映射 `-p 宿主:容器`**：像酒店总机转分机——外面拨总机号（宿主机 8000），转到你房间分机（容器 8000）。
- **运行时注入**：镜像里不烤死配置/密钥，`run` 时用 `--env-file/-e` 塞进去，像手机插 SIM 卡，同一台手机换卡就是换号。
- **数据卷 volume**：把容器内目录映射到宿主机，容器删了数据还在。
- **compose**：一份 yaml 声明一组容器 + 网络 + 启动顺序 + 卷，一条命令管整套系统。

---

## 四、四个产出文件逐个说明

### 1) requirements.txt —— 关键：故意拆成两轮安装
最终锁定版本（示例）：langchain==1.2.12、langchain-core、langchain-deepseek、langchain-tavily、fastapi、uvicorn、pydantic、psycopg[binary,pool]、pymilvus、dashscope、python-dotenv、loguru、requests、pypdf、pikepdf。

**注意：这个文件里故意不写 langgraph 和 langgraph-checkpoint-postgres**，原因见"踩坑 1"，由 Dockerfile 第二轮单独装。

### 2) Dockerfile（逐层解释）
```dockerfile
FROM python:3.13-slim                 # 精简版 Debian13 底座（实测容器内 Python 3.13.15）
WORKDIR /app                          # 后续工作目录，不存在会创建
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*    # 装 psycopg 运行需要的 libpq5，并清 apt 缓存给镜像瘦身
COPY requirements.txt .               # 先只拷依赖清单（利用缓存层）
RUN pip install --no-cache-dir -r requirements.txt -i 清华源   # 第一轮：装主依赖
RUN pip install --no-cache-dir langgraph==1.2.11 \
    langgraph-checkpoint-postgres==3.0.5 -i 清华源             # 第二轮：覆盖锁定 langgraph
COPY . .                              # 最后才拷全部业务代码（常变，放最后才不会让依赖层失效）
EXPOSE 8000                           # 声明容器端口（文档作用，真正放行靠 -p）
CMD ["uvicorn","api:app","--host","0.0.0.0","--port","8000"]   # 容器启动命令；绑 0.0.0.0 外部才可达
```

### 3) .dockerignore —— 控制"构建上下文"
构建时 `docker build` 会把当前目录整个发给守护进程，.dockerignore 决定哪些**不发、不进镜像**。忽略了：
- `.env`（**密钥绝不能进镜像**）、`.git/`、`__pycache__/`、虚拟环境；
- `volumes/`（数据卷，巨大且不该烤进镜像）、`outputs/`、图片、学习 md 等。

### 4) docker-compose.yml —— 5 个服务
| 服务 | 镜像 | 作用 | 对宿主机端口 | 关键点 |
|---|---|---|---|---|
| etcd | quay.io/coreos/etcd:v3.5.18 | Milvus 元数据 | 不暴露（仅内网） | healthcheck |
| minio | minio/minio | Milvus 对象存储 | 不暴露（仅内网） | healthcheck |
| standalone | milvusdb/milvus:v2.6.12 | 向量库本体 | 19530 / 9091 | 依赖 etcd/minio，start_period 90s |
| postgres | postgres:16 | LangGraph checkpoint | **5433→5432** | 数据卷 ./volumes/pgdata，pg_isready 健康检查 |
| app | build: .（本地构建） | 你的 FastAPI | 8000→8000 | 等 postgres healthy 才启动 |

app 的关键配置：
```yaml
depends_on:
  postgres: { condition: service_healthy }   # 等数据库真正可连
  standalone: { condition: service_started } # Milvus 启动即可（调用时才连）
env_file: .env                               # 先灌入 API Key
environment:                                 # 再用服务名覆盖 .env 里的 localhost
  MILVUS_HOST: standalone
  POSTGRES_DSN: postgresql://langgraph_user:******@postgres:5432/langgraph_db
```

---

## 五、实操全过程

### 方式 A：单容器连宿主机（7 步）
1. `docker ps` 确认环境；
2. 写好三个文件后 `docker build -t research-agent .` 造镜像（最终 503MB）；
3. `docker images` 看镜像；
4. `docker run -d --name ra-api -p 8000:8000 --env-file .env -e MILVUS_HOST=host.docker.internal -e "POSTGRES_DSN=...@host.docker.internal:5432/..." research-agent` 起容器；
5. `docker ps` 看 Up + 端口，`docker logs ra-api` 看到 `Application startup complete`（证明连上宿主机 PG）；
6. `docker exec -it ra-api bash` 进容器验证：Debian13 / Python3.13.15 / langgraph1.2.11 / `ls -a` 只有 .env.example 没有 .env / `printenv` 能看到注入的变量 → exit；
7. `docker stop ra-api; docker rm ra-api` 收尾（删容器，镜像保留）。

### 方式 B：compose 一体化
1. 在原 Milvus compose 基础上追加 postgres、app 两服务；
2. `docker compose config --quiet` 先校验 yaml 语法；
3. `docker compose up -d --build` 一键拉起（首次拉 postgres:16 镜像、用缓存构建 app）；
4. 观察到 `research-agent-pg Waiting → Healthy → research-agent-app Starting`，证明 healthcheck 控序生效；
5. `docker compose ps` 五服务全 Up；app 日志 startup complete；
6. 进全新 postgres 容器查表：app 启动时自动建了 checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations 4 张表；
7. `docker exec research-agent-app printenv` 看到 DSN 是 `@postgres:5432`、MILVUS_HOST=standalone → 服务名互联实锤；
8. `docker compose down` 再 `up -d`，表还在 → 数据卷持久化验证通过；
9. `down` → `up -d --build` 模拟"别人 clone 后从零构建"，接口返回 200，流程可靠。

---

## 六、容器网络（本阶段最容易混、高频）

| 场景 | 容器里该写什么地址 | 说明 |
|---|---|---|
| 容器访问它自己 | `localhost` / `127.0.0.1` | 每个容器有独立网络栈，localhost 指容器自身 |
| 容器访问**宿主机**上的服务（方式 A） | `host.docker.internal` | Docker Desktop 内置域名，解析到宿主机 |
| 容器访问**同 compose 的兄弟容器**（方式 B） | **服务名**（如 `postgres`、`standalone`） | compose 自建内网 + 内置 DNS |
| 宿主机/浏览器访问容器 | `127.0.0.1:宿主端口` | 靠 `-p` 映射；服务须绑 `0.0.0.0` |

- 为什么 postgres 映射成 **5433**：宿主机本地已装 PostgreSQL 占着 5432，避让冲突；app 在内部走 `postgres:5432`，不经过这个映射。
- 为什么 etcd/minio 不映射宿主机端口：它们只给内网的 Milvus 用，最小暴露原则。

---

## 七、数据持久化
- compose 里用的是 **bind mount（绑定挂载）**：`./volumes/pgdata:/var/lib/postgresql/data`，把容器数据库目录直接映射到项目下文件夹。
- `docker compose down`：删容器和内网，**volumes/ 里的数据保留**，重建后数据还在（已验证 4 张表不丢）。
- `down -v`：删 named volume；对 bind mount 的宿主机目录无效，要彻底重置需手动删 `volumes/xxx`。

---

## 八、命令速查

### 单容器（方式 A）
```powershell
docker build -t research-agent .      # 构建镜像
docker images                          # 列镜像
docker run -d --name ra-api -p 8000:8000 --env-file .env research-agent   # 起容器
docker ps / docker ps -a               # 看运行中 / 全部容器
docker logs -f ra-api                  # 跟踪日志
docker exec -it ra-api bash            # 进容器
docker stop ra-api; docker rm ra-api   # 停并删容器
docker rmi research-agent              # 删镜像（不删 buildkit 层缓存）
```

### compose（方式 B，必须先 cd 到 docker-compose.yml 所在目录！）
```powershell
docker compose config --quiet          # 校验语法
docker compose up -d                   # 后台整套启动
docker compose up -d --build           # 改了代码/Dockerfile，重新构建再启动
docker compose ps                      # 看整套服务状态
docker compose logs -f app             # 跟踪某个服务日志
docker compose restart app             # 只重启一个服务
docker compose down                    # 停删整套（保留数据卷）
docker compose down -v                 # 连 named volume 一起删
```
> 踩过的坑：在 `C:\Users\13237` 下执行 `docker compose ps` 报 `no configuration file provided`——compose 命令必须站在含 yaml 的目录；而 `docker ps` 是全局的，任何目录都能列所有容器。

---

## 九、别人怎么用上你的项目（交付路径）
**开源/学生项目走"给代码"，不需要推镜像仓库：**
1. 你把代码（含 Dockerfile、docker-compose.yml、requirements、.env.example）推 GitHub（.env 不推）；
2. 别人 `git clone` → 复制 `.env.example` 为 `.env` 并填**他自己的 Key** → `docker compose up -d --build`，本地现构建、整套跑起。

**产品化路径（了解）**：`docker push` 镜像到 Docker Hub/阿里云仓库，compose 里把 `build:.` 换成 `image:仓库/镜像:tag`，别人不拿源码直接拉镜像运行，用于闭源部署/CI-CD/K8s。

光有镜像还跑不起来的两样：① 外部依赖（compose 已自动起 PG/Milvus）；② 使用者自己的 API Key（正因密钥运行时注入、不进镜像，镜像才能安全分发）。

---

## 十、踩坑清单（真实记录）

1. **依赖冲突（最核心）**：全新 build 时 pip 报 ResolutionImpossible——`langchain 1.2.12` 声明 `langgraph>=1.1.1,<1.2.0`，与目标 `langgraph==1.2.11` 在一次性解析中冲突。本地 conda 能跑是因为分步安装、后升级的 langgraph 覆盖了约束。**解决：Dockerfile 拆两轮 pip**，第一轮装 requirements（经 langchain 拉入 1.1.x），第二轮强制装 1.2.11 + checkpoint-postgres 覆盖，仅剩一个可忽略的 incompatibility warning。
2. **PowerShell 把 docker 进度当红字**：build/pull 进度走 stderr，被标成 RemoteException 红字，不是错误。
3. **端口冲突**：本机 PG 占 5432 → compose PG 用 5433 避让；方式 A 遗留的 ra-api 占 8000 → 方式 B 前先 `docker rm -f ra-api`。
4. **换行符 warning**：`LF will be replaced by CRLF`，Git 在 Windows 上的自动换行转换提醒，无害，仓库存 LF 对 Linux 容器反而更合适。
5. **compose 命令目录依赖**：见上节。

---

## 十一、高频追问

**一段话讲清：**
> 我给项目做了 Docker 化：Dockerfile 多分层构建（依赖层前置利用缓存）、.dockerignore 保证密钥不进镜像、运行时环境变量注入；用 docker-compose 把 FastAPI、PostgreSQL、Milvus 及其 etcd/minio 依赖编排成一套，服务名通信、healthcheck 控制启动顺序、数据卷持久化。最终别人 clone 后填自己的 Key、一条 `docker compose up -d --build` 就能跑，不依赖本机预装数据库。期间还解决了全新构建时 langchain/langgraph 版本约束冲突，用拆分 pip 阶段锁定了兼容版本。

**高频追问：**
- 为什么先 COPY requirements 再 COPY 代码？→ 分层缓存，改代码不重装依赖。
- 容器里为什么不能用 localhost 连数据库？→ localhost 指容器自己；跨容器用服务名，连宿主机用 host.docker.internal。
- 密钥怎么管理？→ 不进镜像，.env 被 ignore，运行时 env-file/-e 注入，每人用自己的 Key。
- 数据怎么不丢？→ bind mount 到宿主机卷，容器可随意删。
- 为什么需要 healthcheck？→ depends_on 只保证容器进程启动，不保证服务 ready；数据库没就绪 app 就连，healthcheck 让 app 等它真正可连。
- Docker 和 VM 区别？→ 容器共享宿主机内核、只打包应用依赖，秒起、镜像小；VM 虚拟完整 OS，重。
- Docker 和 K8s？→ compose 管单机一组容器，K8s 管跨机器集群的编排/伸缩/自愈。

---

## 先自测：这几个问题你能不能脱口而出

1. 容器和虚拟机到底差在哪？为什么容器秒起、VM 要分钟级？
2. `docker build` 和 `docker run` 两个时刻分别发生了什么？哪些东西是 build 时定死的、哪些是 run 时才给的？
3. 为什么改代码后重装依赖那层能 CACHED？"层"到底是什么？
4. 你在**运行中的容器里** `apt install 个东西`或改个文件，`docker rm` 再 `run` 一个新容器，改动还在吗？为什么？那为什么数据库的表 down/up 后还在？
5. compose 里 app 写 `postgres:5432`，这串名字凭什么能被解析到那个容器？

如果 1-4 有任何一个含糊，根子都在下面这个模型里。

## 总纲：容器到底是什么 + build/run 分界 + 分层文件系统

### 1）容器的本质：不是"轻量虚拟机"，是"被隔离的普通进程"

- **虚拟机**：虚拟出一整套硬件，里面装一个完整的客户操作系统（Guest OS），所以重、启动慢（要把整个 OS 拉起来）。
- **容器**：**直接用你宿主机的 Linux 内核**，只是通过内核的两个机制（Namespace 做"视图隔离"、Cgroup 做"资源限制"）把一个普通进程圈起来，让它"以为"自己独占了一台机器——有自己的文件系统、进程号、网络。它本质还是宿主机上的一个进程，所以秒起、占用小。

一句话：**VM 虚拟的是硬件，容器隔离的是进程。** 你的 `research-agent-app` 里那个 uvicorn，在容器看是 PID 1，在宿主机看就是若干普通进程之一。

### 2）build 时 vs run 时（这是最关键的分界）

|              | build（造镜像，一次性）                                    | run（起容器，可重复无数次）                            |
| ------------ | ---------------------------------------------------------- | ------------------------------------------------------ |
| 干什么       | 按 Dockerfile 一层层叠文件系统，**烤死**代码、依赖、系统库 | 在镜像上套一层"可写层"，注入环境变量/端口/卷，启动进程 |
| 产物         | 只读镜像（模板）                                           | 容器（实例）                                           |
| 什么在这刻定 | Python 版本、pip 依赖、`COPY` 进去的代码                   | API Key、连哪个库、端口映射、挂哪个卷                  |
| 你项目对应   | langgraph 版本、src 代码被烤进镜像                         | `.env` 的 key、`POSTGRES_DSN=postgres:5432`、`-p 8000` |

**这就解释了"密钥为什么运行时注入"**：build 产物（镜像）是会被复制分发的死东西，烤进去就泄露；run 时才给的环境变量，每次起容器临时塞，同一份镜像在不同人手里用不同 key。

### 3）分层文件系统：理解 CACHED 和"改动为什么消失"

镜像是**一层层只读层叠起来**的，Dockerfile 每一行指令产生一层：

```
层4  COPY . .            ← 常变（你改代码）
层3  pip install langgraph← 不常变
层2  pip install -r req   ← 不常变
层1  apt install libpq5   ← 几乎不变
层0  python:3.13-slim     ← 不变
```

- **构建缓存**：某层的输入没变、且它下面的层没变，就直接复用（你看到的 CACHED）。你只改业务代码，变的只有最上层 COPY，下面三层全复用，所以构建只要 1 秒。把"装依赖"放前面、"拷代码"放最后，就是为了让贵的层尽量常命中缓存。
- **容器可写层**：`run` 时 Docker 在所有只读层**最上面加一层薄薄的可写层**。你在容器里 `apt install`、改文件，改的都是这层。`docker rm` 把容器一删，**这层可写层随之销毁**——所以新容器里你手动装的东西没了。
- **那数据库的表为什么 down/up 还在？** 因为 PostgreSQL 的数据目录被 volume **映射到了宿主机的 `./volumes/pgdata`**，写数据根本没写在容器可写层，而是直接落到宿主机磁盘。容器删了，宿主机那份文件还在，新容器一挂载又看到了。

**所以第 4 题的答案**：容器内手动改的东西默认随容器删除而消失（在可写层）；只有写进挂载卷（volume）的东西能活下来。这就是"容器无状态、数据要外挂"的由来。

------

网络那块（第 5 题：服务名怎么被解析、三种地址区别）是另一个独立小主题，我可以下一条专门用图解讲。

你先消化这个总纲，然后告诉我：**上面自检 1-5 里，哪个听完还卡、或者想让我展开哪一个**（比如"分层再细讲""网络专门讲""想搞懂 namespace 隔离到底怎么回事"），咱们一个一个抠透，不往后赶。

compose是声明式的，run式命令式的

明天7点半的闹钟，然后然后收拾准备出门
