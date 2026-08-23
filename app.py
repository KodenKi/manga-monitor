import os
import re
import hashlib
import requests
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres")
SQLITE_PATH = os.environ.get("DB_PATH", "/data/mangas.db")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

DEFAULT_CHAPTER_REGEX = r"(?:cap[íi]tulo|chapter|cap\.?|epis[oó]dio|ep\.?)\s*[:\-]?\s*(\d+(?:\.\d+)?)"


# ---------------------------------------------------------------------------
# Camada de banco de dados (funciona com SQLite ou Postgres)
# ---------------------------------------------------------------------------
class DB:
    def __init__(self):
        if USE_POSTGRES:
            self.conn = psycopg2.connect(DATABASE_URL)
            self.cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
            self.conn = sqlite3.connect(SQLITE_PATH)
            self.conn.row_factory = sqlite3.Row
            self.cur = self.conn.cursor()

    def execute(self, query, params=()):
        if USE_POSTGRES:
            query = query.replace("?", "%s")
        self.cur.execute(query, params)
        return self.cur

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    return DB()


def init_db():
    conn = get_db()
    id_col = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS mangas (
            id {id_col},
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            selector TEXT,
            chapter_regex TEXT,
            group_name TEXT NOT NULL,
            interval_hours REAL,
            check_time TEXT,
            last_hash TEXT,
            js_render INTEGER NOT NULL DEFAULT 0,
            js_detected INTEGER NOT NULL DEFAULT 0,
            last_checked TEXT,
            last_changed TEXT,
            last_chapter_seen REAL,
            active INTEGER NOT NULL DEFAULT 1,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            failure_notified INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            user_id INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            last_notified_chapter REAL,
            last_notified_at TEXT,
            webhook_url TEXT,
            PRIMARY KEY (user_id, group_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS chapter_history (
            id {id_col},
            user_id INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            manga_name TEXT NOT NULL,
            url TEXT NOT NULL,
            chapter_num REAL,
            detected_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if not username or not password:
            flash("Preencha usuário e senha.")
            return redirect(url_for("register"))

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            flash("Esse usuário já existe.")
            return redirect(url_for("register"))

        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

        session["user_id"] = user["id"]
        session["username"] = username
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Usuário ou senha incorretos.")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Settings / groups (agora por usuário)
# ---------------------------------------------------------------------------
def get_setting(user_id, key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(user_id, key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (user_id, key, value),
    )
    conn.commit()
    conn.close()


def get_group(conn, user_id, group_name):
    row = conn.execute(
        "SELECT * FROM groups WHERE user_id = ? AND group_name = ?", (user_id, group_name)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO groups (user_id, group_name, last_notified_chapter, last_notified_at, webhook_url) "
            "VALUES (?, ?, NULL, NULL, NULL)",
            (user_id, group_name),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM groups WHERE user_id = ? AND group_name = ?", (user_id, group_name)
        ).fetchone()
    return row


def set_group_webhook(conn, user_id, group_name, webhook_url):
    get_group(conn, user_id, group_name)
    conn.execute(
        "UPDATE groups SET webhook_url = ? WHERE user_id = ? AND group_name = ?",
        (webhook_url or None, user_id, group_name),
    )
    conn.commit()


def bump_group_baseline(conn, user_id, group_name, chapter_num):
    row = get_group(conn, user_id, group_name)
    current = row["last_notified_chapter"]
    if current is None or chapter_num > current:
        conn.execute(
            "UPDATE groups SET last_notified_chapter = ? WHERE user_id = ? AND group_name = ?",
            (chapter_num, user_id, group_name),
        )
        conn.commit()


def mark_group_notified(conn, user_id, group_name, chapter_num):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE groups SET last_notified_chapter = ?, last_notified_at = ? WHERE user_id = ? AND group_name = ?",
        (chapter_num, now, user_id, group_name),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Lógica de checagem
# ---------------------------------------------------------------------------
def extract_chapter_number(text, pattern):
    try:
        matches = re.findall(pattern, text, re.IGNORECASE)
    except re.error:
        matches = []
    nums = []
    for m in matches:
        try:
            nums.append(float(m))
        except (TypeError, ValueError):
            continue
    return max(nums) if nums else None


def auto_detect_chapter_text(soup, pattern):
    """Sem seletor manual, tenta achar sozinho quais links da página parecem
    ser de capítulo (o texto do link bate com o padrão de número de
    capítulo). Retorna só o texto desses links — não a página inteira —
    pra evitar ruído de comentários, curtidas, contador de visitas etc.
    Retorna None se não achar nenhum candidato confiável."""
    candidates = []
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        if not text or len(text) > 120:
            continue
        num = extract_chapter_number(text, pattern)
        if num is not None:
            candidates.append((num, text))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    top = candidates[:5]
    return " | ".join(t for _, t in top)


def _extract_from_soup(soup, selector, pattern):
    if selector:
        el = soup.select_one(selector)
        return el.get_text(" ", strip=True) if el else soup.get_text(" ", strip=True)
    if pattern:
        auto_text = auto_detect_chapter_text(soup, pattern)
        if auto_text:
            return auto_text
    return soup.get_text(" ", strip=True)


def fetch_text_simple(url, selector, pattern=None):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_from_soup(soup, selector, pattern)


def fetch_text_js(url, selector, pattern=None):
    """Renderiza a página com um navegador headless antes de extrair o texto.
    Necessário pra sites que montam a lista de capítulos via JavaScript
    (ex: MangaPlus) ou que bloqueiam requisições simples (bot detection).

    Inclui alguns disfarces básicos contra detecção de automação, já que
    sistemas anti-bot mais chatos (Cloudflare e afins) costumam checar
    sinais óbvios de headless Chromium (navigator.webdriver, plugins
    vazios, etc.) antes mesmo de olhar o conteúdo."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        try:
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 900},
                locale="pt-BR",
            )
            # Esconde os sinais mais óbvios de que é um navegador automatizado.
            page.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en'] });
                window.chrome = { runtime: {} };
                """
            )
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                # Alguns sites nunca ficam "ociosos" de rede (comentários,
                # analytics, polling) — tenta de novo só esperando o HTML
                # carregar, sem depender da rede ficar quieta.
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=10000)
                except Exception:
                    pass  # segue mesmo se o seletor não aparecer a tempo
            html = page.content()
        finally:
            browser.close()

    soup = BeautifulSoup(html, "html.parser")
    return _extract_from_soup(soup, selector, pattern)


# Sinais de que a página só mostra o conteúdo real depois de rodar JavaScript
# (ou está bloqueando a requisição, tipo verificação anti-bot).
_JS_WARNING_WORDS = ("javascript", "habilite", "enable", "ativar", "habilitar")


def looks_insufficient(text, has_selector, chapter_found):
    stripped = (text or "").strip()
    if len(stripped) < 60:
        return True
    lower = stripped.lower()
    if "javascript" in lower and any(w in lower for w in _JS_WARNING_WORDS if w != "javascript"):
        return True
    # com seletor configurado, texto muito curto é sinal forte de que o
    # elemento não existia ainda no HTML bruto (não renderizado).
    if has_selector and len(stripped) < 150:
        return True
    # sem seletor manual, se nem o número do capítulo aparece em lugar
    # nenhum da página, é sinal forte de bloqueio/verificação anti-bot ou
    # de página que não renderizou o conteúdo real.
    if not has_selector and not chapter_found:
        return True
    return False


def fetch_text_auto(manga):
    """Busca o conteúdo da fonte, detectando automaticamente se precisa de
    navegador headless E, quando não há seletor manual, tentando achar
    sozinho o link de capítulo certo. Retorna (texto, usou_js)."""
    url = manga["url"]
    selector = manga["selector"]
    pattern = manga["chapter_regex"] or DEFAULT_CHAPTER_REGEX
    force_js = bool(manga["js_render"])
    already_detected_js = bool(manga["js_detected"])

    if force_js or already_detected_js:
        return fetch_text_js(url, selector, pattern), True

    try:
        text = fetch_text_simple(url, selector, pattern)
    except Exception:
        text = ""

    chapter_found = extract_chapter_number(text, pattern) is not None

    if looks_insufficient(text, bool(selector), chapter_found):
        text = fetch_text_js(url, selector, pattern)
        return text, True

    return text, False


def send_discord_notification(user_id, manga_name, group_name, group_webhook, url, chapter_num):
    webhook = group_webhook or get_setting(user_id, "discord_webhook")
    if not webhook:
        return
    if chapter_num is not None:
        chap_txt = f"Capítulo **{chapter_num:g}**"
    else:
        chap_txt = "novo capítulo (não foi possível confirmar o número)"
    payload = {
        "content": f"📚 **{group_name}** tem {chap_txt} disponível!\nDetectado via: {manga_name}\n{url}"
    }
    try:
        requests.post(webhook, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"[discord] falha ao notificar: {e}")


FAILURE_THRESHOLD = 5  # avisa depois de N falhas seguidas de checagem


def send_discord_failure_warning(user_id, manga_name, group_name, group_webhook, url, failures, error_msg):
    webhook = group_webhook or get_setting(user_id, "discord_webhook")
    if not webhook:
        return
    payload = {
        "content": (
            f"⚠️ **{manga_name}** (grupo: {group_name}) está falhando há {failures} checagens seguidas.\n"
            f"Erro mais recente: {error_msg[:200]}\n"
            f"{url}\n"
            f"Pode ser que o site mudou de estrutura, saiu do ar, ou bloqueou o acesso. "
            f"Dá uma olhada quando puder — as checagens continuam tentando sozinhas."
        )
    }
    try:
        requests.post(webhook, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"[discord] falha ao avisar sobre erro: {e}")


def check_manga(manga_id):
    conn = get_db()
    manga = conn.execute("SELECT * FROM mangas WHERE id = ?", (manga_id,)).fetchone()
    if not manga or not manga["active"]:
        conn.close()
        return

    user_id = manga["user_id"]
    now = datetime.now().isoformat(timespec="seconds")
    group_name = manga["group_name"]
    pattern = manga["chapter_regex"] or DEFAULT_CHAPTER_REGEX

    try:
        text, used_js = fetch_text_auto(manga)
    except Exception as e:
        failures = (manga["consecutive_failures"] or 0) + 1
        conn.execute(
            "UPDATE mangas SET last_checked = ?, consecutive_failures = ?, last_error = ? WHERE id = ?",
            (now, failures, str(e)[:300], manga_id),
        )
        if failures >= FAILURE_THRESHOLD and not manga["failure_notified"]:
            group = get_group(conn, user_id, group_name)
            send_discord_failure_warning(user_id, manga["name"], group_name, group["webhook_url"], manga["url"], failures, str(e))
            conn.execute("UPDATE mangas SET failure_notified = 1 WHERE id = ?", (manga_id,))
        print(f"[check] erro ao checar {manga['name']}: {e}")
        conn.commit()
        conn.close()
        return

    newly_detected_js = used_js and not manga["js_detected"] and not manga["js_render"]
    if newly_detected_js:
        conn.execute("UPDATE mangas SET js_detected = 1 WHERE id = ?", (manga_id,))

    # Checagem deu certo: zera contador de falhas se algum dia falhou antes.
    if manga["consecutive_failures"]:
        conn.execute(
            "UPDATE mangas SET consecutive_failures = 0, last_error = NULL, failure_notified = 0 WHERE id = ?",
            (manga_id,),
        )

    new_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    chapter_num = extract_chapter_number(text, pattern)
    is_first_check = manga["last_hash"] is None
    page_changed = (not is_first_check) and new_hash != manga["last_hash"]

    # Diagnóstico: se mesmo depois de tudo (seletor/JS) não achou nenhum
    # número de capítulo, guarda uma prévia do que foi recebido — ajuda a
    # entender se o problema é bloqueio anti-bot, seletor errado, etc.
    if chapter_num is None:
        preview = (text or "").strip()[:200]
        conn.execute(
            "UPDATE mangas SET last_error = ? WHERE id = ?",
            (f"Nenhum número de capítulo encontrado. Prévia do conteúdo recebido: {preview!r}", manga_id),
        )

    if is_first_check:
        if chapter_num is not None:
            bump_group_baseline(conn, user_id, group_name, chapter_num)
        conn.execute(
            "UPDATE mangas SET last_hash = ?, last_checked = ?, last_chapter_seen = ? WHERE id = ?",
            (new_hash, now, chapter_num, manga_id),
        )
        conn.commit()
        conn.close()
        return

    group = get_group(conn, user_id, group_name)
    group_last_chapter = group["last_notified_chapter"]

    notify = False
    if chapter_num is not None:
        if group_last_chapter is None or chapter_num > group_last_chapter:
            notify = True
    elif page_changed:
        cooldown_hours = float(get_setting(user_id, "group_cooldown_hours", "2") or 2)
        last_notified_at = group["last_notified_at"]
        recently_notified = False
        if last_notified_at:
            elapsed = (datetime.now() - datetime.fromisoformat(last_notified_at)).total_seconds() / 3600
            recently_notified = elapsed < cooldown_hours
        notify = not recently_notified

    if notify:
        send_discord_notification(user_id, manga["name"], group_name, group["webhook_url"], manga["url"], chapter_num)
        mark_group_notified(conn, user_id, group_name, chapter_num if chapter_num is not None else (group_last_chapter or 0))
        conn.execute(
            "INSERT INTO chapter_history (user_id, group_name, manga_name, url, chapter_num, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, group_name, manga["name"], manga["url"], chapter_num, now),
        )
        conn.execute(
            "UPDATE mangas SET last_hash = ?, last_checked = ?, last_changed = ?, last_chapter_seen = ? WHERE id = ?",
            (new_hash, now, now, chapter_num, manga_id),
        )
    else:
        conn.execute(
            "UPDATE mangas SET last_hash = ?, last_checked = ?, last_chapter_seen = ? WHERE id = ?",
            (new_hash, now, chapter_num, manga_id),
        )
    conn.commit()
    conn.close()


def check_all_due():
    conn = get_db()
    mangas = conn.execute("SELECT * FROM mangas WHERE active = 1").fetchall()
    conn.close()
    now = datetime.now()
    for m in mangas:
        last_checked = datetime.fromisoformat(m["last_checked"]) if m["last_checked"] else None

        if m["check_time"]:
            # Modo horário fixo: checa uma vez por dia, no horário configurado (ex: 17:00).
            try:
                hh, mm = (int(p) for p in m["check_time"].split(":"))
            except (ValueError, AttributeError):
                continue
            scheduled_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            due = now >= scheduled_today and (last_checked is None or last_checked < scheduled_today)
            if due:
                check_manga(m["id"])
        else:
            # Modo intervalo: checa a cada X horas desde a última checagem.
            interval_hours = m["interval_hours"] or 5
            if last_checked is None:
                check_manga(m["id"])
                continue
            hours_passed = (now - last_checked).total_seconds() / 3600
            if hours_passed >= interval_hours:
                check_manga(m["id"])


# ---------------------------------------------------------------------------
# Rotas do app (todas exigem login e são restritas ao dono do registro)
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    user_id = session["user_id"]
    conn = get_db()
    mangas = conn.execute(
        "SELECT * FROM mangas WHERE user_id = ? ORDER BY group_name, name", (user_id,)
    ).fetchall()
    groups = {
        g["group_name"]: g
        for g in conn.execute("SELECT * FROM groups WHERE user_id = ?", (user_id,)).fetchall()
    }
    week_ago = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    chapters_this_week = conn.execute(
        "SELECT COUNT(*) AS c FROM chapter_history WHERE user_id = ? AND detected_at >= ?",
        (user_id, week_ago),
    ).fetchone()["c"]
    conn.close()

    grouped = {}
    for m in mangas:
        grouped.setdefault(m["group_name"], []).append(m)

    stats = {
        "total_sources": len(mangas),
        "active_sources": sum(1 for m in mangas if m["active"]),
        "total_groups": len(grouped),
        "failing_sources": sum(1 for m in mangas if (m["consecutive_failures"] or 0) > 0),
        "chapters_this_week": chapters_this_week,
    }

    return render_template("index.html", grouped=grouped, groups=groups, stats=stats)


@app.route("/add", methods=["POST"])
@login_required
def add_manga():
    user_id = session["user_id"]
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    selector = request.form.get("selector", "").strip() or None
    chapter_regex = request.form.get("chapter_regex", "").strip() or None
    group_name = request.form.get("group_name", "").strip() or name
    group_webhook = request.form.get("group_webhook", "").strip() or None
    js_render = 1 if request.form.get("js_render") == "on" else 0
    check_time = request.form.get("check_time", "").strip() or None
    interval_hours = None
    if not check_time:
        interval_hours = float(request.form.get("interval_hours") or 5)

    conn = get_db()
    conn.execute(
        "INSERT INTO mangas (user_id, name, url, selector, chapter_regex, group_name, interval_hours, check_time, js_render) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, url, selector, chapter_regex, group_name, interval_hours, check_time, js_render),
    )
    get_group(conn, user_id, group_name)
    if group_webhook:
        set_group_webhook(conn, user_id, group_name, group_webhook)
    conn.commit()
    conn.close()
    flash(f"'{name}' adicionado ao grupo '{group_name}'.")
    return redirect(url_for("index"))


def _owned_manga_or_404(conn, manga_id, user_id):
    return conn.execute(
        "SELECT * FROM mangas WHERE id = ? AND user_id = ?", (manga_id, user_id)
    ).fetchone()


@app.route("/edit/<int:manga_id>", methods=["GET", "POST"])
@login_required
def edit_manga(manga_id):
    user_id = session["user_id"]
    conn = get_db()
    manga = _owned_manga_or_404(conn, manga_id, user_id)
    if not manga:
        conn.close()
        flash("Fonte não encontrada.")
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form["name"].strip()
        url = request.form["url"].strip()
        selector = request.form.get("selector", "").strip() or None
        chapter_regex = request.form.get("chapter_regex", "").strip() or None
        group_name = request.form.get("group_name", "").strip() or name
        js_render = 1 if request.form.get("js_render") == "on" else 0
        check_time = request.form.get("check_time", "").strip() or None
        interval_hours = None
        if not check_time:
            interval_hours = float(request.form.get("interval_hours") or 5)

        conn.execute(
            "UPDATE mangas SET name = ?, url = ?, selector = ?, chapter_regex = ?, group_name = ?, "
            "interval_hours = ?, check_time = ?, js_render = ? WHERE id = ?",
            (name, url, selector, chapter_regex, group_name, interval_hours, check_time, js_render, manga_id),
        )
        get_group(conn, user_id, group_name)
        conn.commit()
        conn.close()
        flash(f"'{name}' atualizado.")
        return redirect(url_for("index"))

    conn.close()
    return render_template("edit.html", manga=manga)


@app.route("/delete/<int:manga_id>", methods=["POST"])
@login_required
def delete_manga(manga_id):
    user_id = session["user_id"]
    conn = get_db()
    if _owned_manga_or_404(conn, manga_id, user_id):
        conn.execute("DELETE FROM mangas WHERE id = ?", (manga_id,))
        conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/toggle/<int:manga_id>", methods=["POST"])
@login_required
def toggle_manga(manga_id):
    user_id = session["user_id"]
    conn = get_db()
    if _owned_manga_or_404(conn, manga_id, user_id):
        conn.execute("UPDATE mangas SET active = 1 - active WHERE id = ?", (manga_id,))
        conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/check_now/<int:manga_id>", methods=["POST"])
@login_required
def check_now(manga_id):
    user_id = session["user_id"]
    conn = get_db()
    owned = _owned_manga_or_404(conn, manga_id, user_id)
    conn.close()
    if owned:
        check_manga(manga_id)
        flash("Checagem manual concluída.")
    return redirect(url_for("index"))


def try_send_test_webhook(webhook, label):
    if not webhook:
        return False, "Nenhum webhook informado pra testar."
    payload = {"content": f"🔔 Teste do Monitor de Mangás ({label}) — se você está vendo isso, o webhook está funcionando!"}
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            return True, "Teste enviado! Confira o canal do Discord."
        return False, f"Discord respondeu com status {resp.status_code} — confira se a URL do webhook está correta."
    except requests.RequestException as e:
        return False, f"Falha ao enviar teste: {e}"


@app.route("/group_webhook", methods=["POST"])
@login_required
def group_webhook():
    user_id = session["user_id"]
    group_name = request.form.get("group_name", "").strip()
    webhook_url = request.form.get("webhook_url", "").strip() or None
    if group_name:
        conn = get_db()
        # só permite editar grupos que já têm alguma fonte do próprio usuário
        owns_group = conn.execute(
            "SELECT 1 FROM mangas WHERE user_id = ? AND group_name = ?", (user_id, group_name)
        ).fetchone()
        if owns_group:
            set_group_webhook(conn, user_id, group_name, webhook_url)
            flash(f"Webhook do grupo '{group_name}' atualizado.")
        conn.close()
    return redirect(url_for("index"))


@app.route("/group_webhook/test", methods=["POST"])
@login_required
def test_group_webhook():
    user_id = session["user_id"]
    group_name = request.form.get("group_name", "").strip()
    webhook_url = request.form.get("webhook_url", "").strip() or get_setting(user_id, "discord_webhook")
    ok, msg = try_send_test_webhook(webhook_url, group_name or "grupo")
    flash(msg)
    return redirect(url_for("index"))


@app.route("/history")
@login_required
def history():
    user_id = session["user_id"]
    conn = get_db()
    entries = conn.execute(
        "SELECT * FROM chapter_history WHERE user_id = ? ORDER BY detected_at DESC LIMIT 200",
        (user_id,),
    ).fetchall()
    conn.close()
    return render_template("history.html", entries=entries)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = session["user_id"]
    if request.method == "POST":
        set_setting(user_id, "discord_webhook", request.form.get("discord_webhook", "").strip())
        set_setting(user_id, "group_cooldown_hours", request.form.get("group_cooldown_hours", "2").strip() or "2")
        flash("Configurações salvas.")
        return redirect(url_for("settings"))
    webhook = get_setting(user_id, "discord_webhook")
    cooldown = get_setting(user_id, "group_cooldown_hours", "2")
    return render_template("settings.html", webhook=webhook, cooldown=cooldown, using_postgres=USE_POSTGRES)


@app.route("/settings/test_webhook", methods=["POST"])
@login_required
def test_webhook():
    user_id = session["user_id"]
    webhook = request.form.get("discord_webhook", "").strip() or get_setting(user_id, "discord_webhook")
    ok, msg = try_send_test_webhook(webhook, "geral")
    flash(msg)
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------
init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(check_all_due, IntervalTrigger(minutes=15), id="check_all_due")
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
