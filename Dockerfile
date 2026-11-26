FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/screenshots

ENV DB_PATH=/app/data/users.db
ENV PYTHONUNBUFFERED=1

CMD ["python", "telegram_bot.py"]

