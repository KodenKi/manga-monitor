FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instala o Chromium do Playwright como base/fallback (mais simples, some
# sites já funcionam bem só com ele).
RUN playwright install --with-deps chromium

# Instala o Google Chrome de verdade + Xvfb (tela virtual, já que o
# servidor não tem monitor). A documentação do patchright recomenda usar
# o Chrome real (não o Chromium genérico) rodando SEM modo headless pra
# ter a maior chance de passar por proteções anti-bot mais chatas —
# o Xvfb permite isso mesmo sem tela física.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg xvfb \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Garante que o patchright também está instalado e sabe onde achar o
# Chrome de verdade que acabamos de instalar.
RUN (python -m patchright install chrome || echo "Aviso: instalação do patchright/chrome falhou, seguindo com fallback.")

COPY . .

RUN mkdir -p /data
VOLUME /data

EXPOSE 5050

# Roda o app dentro de uma tela virtual (Xvfb), necessária pro Chrome
# rodar em modo "com interface" (não-headless) dentro do container.
CMD ["xvfb-run", "-a", "python", "app.py"]
