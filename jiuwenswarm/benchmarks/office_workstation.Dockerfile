FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash coreutils curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests==2.32.3 pandas==2.2.3 openpyxl==3.1.5 \
    python-docx==1.1.2 pypdf==5.3.0 playwright==1.51.0 \
    && playwright install --with-deps chromium
WORKDIR /workspace
