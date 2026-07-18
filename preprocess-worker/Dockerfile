FROM python:3.12-slim

WORKDIR /app

# docling(TableFormer)はopencv経由でX11共有ライブラリをロードする。
# ヘッドレスコンテナのため最小限のランタイムライブラリのみ追加する。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# doclingはPyTorch依存。コンテナはCPU版torchで数GB級になるため、
# docker composeのprofile "preprocess" でゲートし、手動実行時のみビルドする。
RUN pip install --no-cache-dir -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

COPY app ./app

CMD ["python", "-m", "app.cli", "--help"]
