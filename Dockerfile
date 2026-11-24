FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV DB_PATH=/data/data.db KEYS_PATH=/data/keys.json
VOLUME /data
CMD ["python", "bot.py"]
