FROM python:3.12-slim

# curl is used by docker-compose.yml's healthcheck, not by the app itself.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

ENV FLASK_APP=wsgi.py

EXPOSE 5000

CMD ["./entrypoint.sh"]
