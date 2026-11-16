FROM python:3.12-slim

# yt-dlpがYouTubeの署名解決に使うJavaScriptランタイム
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railwayが渡すPORTで待ち受ける（未設定時は8000）
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
