FROM python:3.12-slim
# 스크립트로 사진을 그리는 사이트를 읽기 위한 헤드리스 브라우저
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py index.html ./
ENV HOST=0.0.0.0 PYTHONUNBUFFERED=1 CHROME_PATH=/usr/bin/chromium
CMD ["python", "server.py"]
