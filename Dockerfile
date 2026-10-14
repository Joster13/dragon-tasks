FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
COPY core.py main.py speech.py ./
USER bot
CMD ["python", "main.py"]
