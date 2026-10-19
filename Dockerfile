FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data LOG_DIR=/data/logs
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY main.py .
# Режим (DRY_RUN) и ключи берутся из переменных окружения / env_file; состояние и логи — в томе /data
CMD ["python", "main.py"]
