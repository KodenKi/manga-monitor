FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala o navegador Chromium usado pelo Playwright pra renderizar sites
# com JavaScript (ex: MangaPlus). Isso deixa a imagem maior (~300-400MB) e
# o build mais demorado na primeira vez.
RUN playwright install --with-deps chromium

COPY . .

RUN mkdir -p /data
VOLUME /data

EXPOSE 5050

CMD ["python", "app.py"]
