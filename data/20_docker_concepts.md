# Docker 容器化核心概念与实操

## 1. 核心概念（先建立心智模型）

### 1.1 镜像（Image）vs 容器（Container）

- **镜像** = 只读的"安装包/模板"，里面是 Python 环境 + 你的代码 + 依赖，分层叠起来的。类比：**类（class）**。
- **容器** = 镜像跑起来的一个**运行实例**，有自己独立的文件系统、进程、网络。类比：**对象（instance）**。同一个镜像可以同时跑多个容器。
- 一句话：`docker build` 把 Dockerfile 做成镜像，`docker run` 用镜像起容器。

### 1.2 Dockerfile 是什么

一份"怎么构建镜像"的说明书，从上到下每条指令叠一层。你只需要掌握这几条：

| 指令 | 作用 |
|---|---|
| `FROM python:3.13-slim` | 以精简版 Python 3.13 为底座 |
| `WORKDIR /app` | 设容器内工作目录（相当于 cd） |
| `COPY 宿主机路径 容器路径` | 把文件拷进镜像 |
| `RUN 命令` | 构建时执行（装系统库、pip 装依赖） |
| `ENV K=V` | 设环境变量 |
| `EXPOSE 8000` | 声明容器监听端口（仅文档作用） |
| `CMD [...]` | 容器启动时默认执行的命令 |

### 1.3 镜像分层与构建缓存（高频，也是优化关键）

Dockerfile 每条指令生成**一层**，构建时某层没变就**复用缓存**。所以正确顺序是：

```
先 COPY requirements.txt → 再 pip install → 最后才 COPY 全部代码
```

原因：依赖不常变，前两层能一直命中缓存；你天天改业务代码，只有最后一层重建，**几秒搞定而不是每次重装几分钟依赖**。如果一上来就 `COPY . .` 再装依赖，改一行代码也要重装全部包，非常慢。

### 1.4 端口映射 -p、卷 -v、环境变量 -e

- `-p 8000:8000`：左边**宿主机**端口，右边**容器内**端口。容器是隔离的，不映射的话外面访问不到。
- `-v 宿主机目录:容器目录`：把宿主机目录挂进容器，容器删了数据还在（PostgreSQL/Milvus 的数据必须挂卷，否则删容器数据全没）。
- `-e KEY=VALUE` / `--env-file .env`：把密钥、连接串传进容器。**密钥绝不写进镜像**，运行时注入。

### 1.5 .dockerignore（和 .gitignore 同理）

构建时忽略不需要的文件，让镜像更小、构建更快，并**防止 .env 密钥和垃圾文件被打进镜像**。

### 1.6 docker-compose：一条命令拉起一组容器

你的系统不是一个进程，而是 **app + PostgreSQL + Milvus(etcd/minio/standalone)** 好几个容器，手动一个个 `docker run` 太痛苦。compose 用一个 yaml 声明所有服务，`docker compose up -d` 一键全起，还自动建一个内部网络：**服务名就是主机名**，app 里连数据库直接写 `postgres:5432`、连 Milvus 写 `standalone:19530`。

---

## 2. 你项目的容器化架构（先看懂依赖关系）

```
┌─────────────────────────── docker 内部网络 ───────────────────────────┐
│                                                                       │
│   app 容器 (你的 FastAPI, 8000)                                       │
│      │ 用"服务名"互联（不是 localhost！）                              │
│      ├──► postgres 容器:5432        （Checkpoint 持久化）             │
│      └──► standalone 容器:19530     （Milvus，依赖 etcd+minio）       │
│                                                                       │
│   宿主机浏览器 ── -p 8000:8000 ──► app                                 │
└───────────────────────────────────────────────────────────────────────┘
        外部 API（DeepSeek/Tavily/DashScope）走公网，密钥由 .env 注入
```

**最容易踩的坑（务必记住）：容器里的 `127.0.0.1 / localhost` 指的是容器自己，不是你的宿主机，也不是别的容器。**
- 跨容器通信 → 用**服务名**（compose 内）
- 容器连**宿主机上**装的服务 → 用 `host.docker.internal`（Docker Desktop 内置域名）

---

## 3. 三个文件（可直接复制，确认后我帮你建到项目里）

### 3.1 requirements.txt（锁定你当前环境的真实版本）

> 你项目之前没有依赖清单，这本身就是个该补的工程短板。下面版本就是从你 conda `langgraph` 环境里读出来的。

```text
langgraph==1.2.11
langgraph-checkpoint-postgres==3.0.5
langchain==1.2.12
langchain-core==1.5.5
langchain-deepseek==1.0.1
langchain-tavily==0.2.17
fastapi==0.135.1
uvicorn==0.46.0
pydantic==2.12.5
psycopg[binary,pool]==3.3.3
pymilvus==2.6.12
dashscope==1.25.6
python-dotenv==1.2.1
loguru==0.7.3
requests==2.32.5
pypdf==6.10.2
pikepdf==10.5.1
```

### 3.2 Dockerfile（逐行注释版）

```dockerfile
# 底座：精简版 Python 3.13（比完整 python 镜像小很多）
FROM python:3.13-slim

# 容器内工作目录，后续相对路径都基于它
WORKDIR /app

# psycopg 运行时需要 libpq；先装系统依赖（这层基本不变，吃缓存）
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

# ① 先只拷依赖清单
COPY requirements.txt .
# ② 单独装依赖（用清华源加速）。代码再怎么改，这层都命中缓存
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# ③ 最后才拷全部业务代码（改动最频繁，放最下面）
COPY . .

# 声明容器监听 8000（真正对外暴露靠 run 的 -p 或 compose 的 ports）
EXPOSE 8000

# 启动命令：容器内必须绑 0.0.0.0（绑 127.0.0.1 宿主机访问不到！）
# Linux 容器里 uvicorn 默认事件循环就兼容 psycopg 异步，不用你 Windows 那套 SelectorEventLoop
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 3.3 .dockerignore

```text
.git
__pycache__/
*.pyc
.env                # 密钥绝不进镜像
volumes/            # 数据库/Milvus 数据不进镜像
outputs/
images/
*.png
.langgraph_api/
.venv/
venv/
```

---

## 4. 两种落地方式（二选一，推荐先 A 后 B）

### 方式 A：只容器化 app，连你现有的服务（改动最小，先跑通用这个）

你现在 PostgreSQL 是本地装的、Milvus 已经用 compose 跑着并映射到了宿主机端口。那 app 容器通过 `host.docker.internal` 走宿主机端口去连它们，**不用动现有任何东西**：

```powershell
# 1. 构建镜像（末尾的点别漏，代表用当前目录的 Dockerfile）
docker build -t research-agent .

# 2. 起容器：注入 .env，并把两个连接地址从 localhost 改成 host.docker.internal
docker run -d --name ra-api -p 8000:8000 --env-file .env `
  -e MILVUS_HOST=host.docker.internal `
  -e POSTGRES_DSN=postgresql://langgraph_user:******@host.docker.internal:5432/langgraph_db `
  research-agent

# 3. 浏览器打开 http://127.0.0.1:8000/docs
```

> `-e` 会**覆盖** `.env` 里的同名变量，所以 .env 里写 localhost 也没关系，运行时被替换。

### 方式 B：全栈一体化 compose（更标准，推荐）

在你**现有 docker-compose.yml（etcd/minio/standalone）基础上追加 postgres 和 app 两个服务**：

```yaml
  # —— 追加：PostgreSQL ——
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: langgraph_user
      POSTGRES_PASSWORD: "your_password"
      POSTGRES_DB: langgraph_db
    volumes:
      - ./volumes/pgdata:/var/lib/postgresql/data   # 数据挂卷，容器删了不丢
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langgraph_user"]
      interval: 10s
      retries: 5

  # —— 追加：你的 FastAPI ——
  app:
    build: .                    # 用当前目录 Dockerfile 构建
    depends_on:
      postgres:
        condition: service_healthy     # 等数据库真正就绪再启动 app
      standalone:
        condition: service_started
    env_file: .env
    environment:
      # 关键：compose 内部用服务名互联，覆盖 .env 里的 localhost
      MILVUS_HOST: standalone
      POSTGRES_DSN: postgresql://langgraph_user:******@postgres:5432/langgraph_db
    ports:
      - "8000:8000"
```

一键起停：

```powershell
docker compose up -d --build      # 构建并后台启动全部服务
docker compose logs -f app        # 实时看 app 日志
docker compose ps                 # 看各服务状态
docker compose down               # 停止并删除容器（数据在 volumes 里，不丢）
```

> 注意：方式 B 的 postgres 是**全新空库**（和你本地已有会话数据的那个库不是一份）。学习用空库没问题；想沿用旧数据就用方式 A，或做一次数据迁移。

---

## 5. 必会命令速查表

| 目的 | 命令 |
|---|---|
| 构建镜像 | `docker build -t research-agent .` |
| 看本地镜像 | `docker images` |
| 看运行中的容器 | `docker ps`（加 `-a` 看已停止的） |
| 后台运行 | `docker run -d --name ra-api -p 8000:8000 research-agent` |
| 实时日志 | `docker logs -f ra-api` |
| 进容器排查 | `docker exec -it ra-api bash` |
| 停/删容器 | `docker stop ra-api`；`docker rm ra-api` |
| compose 起 | `docker compose up -d --build` |
| compose 看日志 | `docker compose logs -f app` |
| compose 停 | `docker compose down` |
| 清理悬空镜像省空间 | `docker image prune -f` |

---

## 6. 自测题（先自己答，答案在后）

1. 镜像和容器的区别？
2. Dockerfile 为什么要"先 COPY requirements、pip install，最后才 COPY 代码"？
3. 容器里为什么必须绑 `0.0.0.0` 而不是 `127.0.0.1`？
4. `-p 8000:8000` 左右两个端口分别是谁的？
5. 容器里的 localhost 指谁？app 容器怎么连另一个 postgres 容器？怎么连宿主机上的数据库？
6. 为什么 .env 和密钥不能打进镜像？
7. compose 解决什么问题？服务名有什么用？
8. `depends_on` + `healthcheck` 解决什么问题？
9. 改了代码后怎么让容器里的服务更新？数据为什么不会丢？
10. Docker 和 K8s 什么关系？Pod/Deployment/Service 分别大概是什么？

### 参考答案

1. **镜像是只读模板（类），容器是镜像运行起来的实例（对象）**；一个镜像可起多个容器。
2. **分层构建缓存**：依赖不常变，单独成层能长期命中缓存；业务代码常变，放最后，改代码只重建最后一层，省掉重装依赖的时间。
3. `127.0.0.1` 只接受容器**内部**回环访问，宿主机的端口映射进不来；绑 `0.0.0.0` 才监听所有网卡，外部才能通过 `-p` 访问到。
4. 左边是**宿主机**端口，右边是**容器内**端口；访问宿主机左端口会被转发到容器右端口。
5. 容器里 localhost 指**容器自己**；连另一个容器用 compose **服务名**（如 `postgres:5432`）；连宿主机上的服务用 `host.docker.internal`。
6. 镜像会被推到仓库、可能多人拉取，密钥打进去等于泄露；且镜像应做到"环境无关"，密钥在运行时用 `--env-file/-e` 注入，换环境不用重新构建。
7. compose 用一个 yaml **编排/一键管理多个容器**并自动建内部网络；**服务名即主机名**，容器间靠它做服务发现，不用记 IP。
8. `depends_on` 保证启动顺序，但容器"启动了"不代表数据库"就绪了"；配合 **healthcheck** 等 postgres 真正能接受连接再启动 app，避免 app 先起连库失败崩溃。
9. 重新 `build` 再 `up`（compose 用 `up -d --build`）；数据库数据通过 **volume 挂在宿主机**，容器重建删除都不影响数据卷。
10. **Docker 负责"打包单个容器"，K8s 负责在集群里"编排/调度/伸缩一大堆容器"**，是上下层关系。Pod 是最小调度单位（装一个或几个紧密容器）、Deployment 管副本数量和滚动更新、Service 给一组 Pod 提供稳定访问入口。实习阶段会答到这个程度即可。

---
