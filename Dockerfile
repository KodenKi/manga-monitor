FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala o navegador Chromium usado pelo Playwright/patchright pra
# renderizar sites com JavaScript (ex: MangaPlus). Isso deixa a imagem
# maior (~300-400MB por navegador) e o build mais demorado na primeira vez.
# O --with-deps do playwright já cobre as bibliotecas de sistema
# necessárias; o patchright usa o comando próprio dele, sem esse flag.
RUN playwright install --with-deps chromium
RUN python -m patchright install chromium

COPY . .

RUN mkdir -p /data
VOLUME /data

EXPOSE 5050

CMD ["python", "app.py"]
