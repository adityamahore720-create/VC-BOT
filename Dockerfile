FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN ffmpeg -f lavfi -i anullsrc=r=48000:cl=mono -t 30 -q:a 9 silence.mp3
CMD ["python", "bot.py"]
