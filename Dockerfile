FROM denoland/deno:bin AS deno
FROM python:3.11-slim
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home bot
COPY bot.py .
USER bot
CMD ["python", "-u", "bot.py"]
