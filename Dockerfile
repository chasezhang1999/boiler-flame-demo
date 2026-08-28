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
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
