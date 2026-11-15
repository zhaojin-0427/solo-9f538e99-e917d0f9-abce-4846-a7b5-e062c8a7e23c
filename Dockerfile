FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    DB_PATH=/instance/fermenter.db \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./

RUN mkdir -p /data /instance

EXPOSE 8000

# 容器内用 Flask 内置服务器即可满足演示/质控场景；生产可换 gunicorn
CMD ["python", "-I", "run.py"]
