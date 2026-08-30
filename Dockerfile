FROM python:3.11-slim

# opencv-python-headless 仍需要 libgl/libglib
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY service/ ./service/
COPY tools/ ./tools/
# 补录工具要读 prompts 里的提示词，保证和工作流用同一份
COPY dify/ ./dify/
EXPOSE 8000
# 默认按 CPU 核数开进程，这样演示前把机器升配到 4 核，重启一下就自动吃满，
# 不用再来改这个文件。想手工定就在 .env 里写 WEB_CONCURRENCY=N。
# 用 exec 是为了让 uvicorn 接管 PID 1，docker stop 能立刻收到信号而不是等超时。
CMD ["sh", "-c", "exec uvicorn service.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-$(nproc)}"]
