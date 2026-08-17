FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 app:app"]
