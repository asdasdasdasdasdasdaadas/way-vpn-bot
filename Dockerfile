FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bot
COPY vpn_bot ./vpn_bot
RUN mkdir /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "-m", "vpn_bot.app"]
