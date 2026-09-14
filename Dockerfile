# 底座：精简版 Python 3.13（与本地 conda 环境大版本一致）
FROM python:3.13-slim

# 容器内工作目录，后续相对路径都基于它
WORKDIR /app

# ① 先只拷依赖清单单独安装，利用 Docker 分层缓存：
#    只改业务代码时这一层命中缓存，不会重新下载安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# ② 最后才拷全部业务代码（改动最频繁，放最下面）
#    注意 data/ 知识库文档会一起拷入；.env 密钥被 .dockerignore 排除
COPY . .

# 声明容器监听端口（真正对外暴露靠 run 的 -p 或 compose 的 ports）
EXPOSE 8000

# 启动 API 服务。容器内必须绑 0.0.0.0（绑 127.0.0.1 宿主机访问不到）
# 一次性入库请用：docker compose run --rm app python run.py --ingest
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
