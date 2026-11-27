import os
import re
import hashlib
import requests
import threading
import time
import secrets
import json
from urllib.parse import urlparse, urljoin
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

# Garante que só um navegador headless roda por vez em todo o app — sem
# isso, o relógio automático (que roda num thread separado) e um clique
# manual em "Checar agora" podiam abrir navegador ao mesmo tempo,
# multiplicando o consumo de RAM e arriscando derrubar o servidor
# inteiro (não só o app) se a máquina tiver pouca memória sobrando.
_BROWSER_LOCK = threading.Lock()

# Guarda, por thread, o que o navegador viu na última página que ele abriu
# (abas, links, requisições JSON). Só é preenchido quando a lista de
# capítulos não foi encontrada, pra servir de diagnóstico.
_debug_local = threading.local()

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

_PLACEHOLDER_SECRET = "troque-esta-chave-em-producao"


def _load_secret_key():
    """Usa SECRET_KEY do ambiente se ela for de verdade. Se estiver vazia ou
    com o valor de exemplo (que é público, qualquer um conseguiria forjar
    um login com ele), gera uma chave aleatória e guarda no volume /data
    pra ela sobreviver a reinícios do container."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key and env_key != _PLACEHOLDER_SECRET:
        return env_key
    path = os.path.join(os.path.dirname(SQLITE_PATH) or ".", "secret_key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        # sem como gravar: usa uma chave aleatória só dessa execução
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _load_secret_key()

# Versão mostrada discretamente no rodapé, pra saber se uma atualização
# já entrou no ar. APP_VERSION é a data da entrega (mudada a cada
# atualização); BUILD_ID é calculado sozinho a partir dos arquivos do app e
# muda sempre que qualquer arquivo mudar, mesmo que a data não tenha sido
# alterada.
APP_VERSION = "2026.11.26"


def _compute_build_id():
    h = hashlib.sha1()
    base = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(base, "app.py"), os.path.join(base, "static", "style.css")]
    templates_dir = os.path.join(base, "templates")
    if os.path.isdir(templates_dir):
        paths += sorted(os.path.join(templates_dir, f) for f in os.listdir(templates_dir))
    for path in paths:
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except OSError:
            continue
    return h.hexdigest()[:7]


BUILD_ID = _compute_build_id()


@app.context_processor
def inject_version():
    """Disponibiliza a versão pro rodapé e, na primeira página que cada
    usuário abrir depois de uma atualização, um aviso de "app atualizado"
    (mostrado uma vez só: a última versão vista fica guardada por usuário)."""
    current = f"v{APP_VERSION} · {BUILD_ID}"
    notice = None
    user_id = session.get("user_id")
    if user_id and session.get("seen_version") != current:
        try:
            last_seen = get_setting(user_id, "last_seen_version", "")
            if last_seen != current:
                notice = {"current": current, "previous": last_seen or None}
                set_setting(user_id, "last_seen_version", current)
            session["seen_version"] = current
        except Exception as e:
            print(f"[versão] não consegui checar a versão vista: {e}")
    return {"app_version": current, "update_notice": notice}

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


# Rótulo de capítulo no INÍCIO de um texto curto: "Capítulo 222", "Cap. 222",
# "Chapter 222 - Título". Comentários e sinopses costumam citar capítulos no
# meio de frases, então exigir o início ajuda a não confundir com eles.
_LABEL_RE = re.compile(
    r"^\s*(?:cap[ií]tulo|chapter|cap\.?|epis[oó]dio|ep\.?)\s*[:\-#]?\s*(\d+(?:[.,]\d+)?)\b",
    re.IGNORECASE,
)


def _label_candidates(soup):
    """Procura rótulos curtos de capítulo em QUALQUER elemento, não só em
    links. Cobre linhas de lista que não são links (ex: capítulo bloqueado
    ou de acesso antecipado) e listas montadas por JavaScript."""
    out = []
    for node in soup.find_all(string=_LABEL_RE):
        text = re.sub(r"\s+", " ", str(node)).strip()
        if not text or len(text) > 60:
            continue
        parent = node.parent
        if parent is not None and parent.name in ("script", "style", "title", "noscript"):
            continue
        in_comments = False
        for anc in list(node.parents)[:8]:
            attrs = f"{anc.get('id') or ''} {' '.join(anc.get('class') or [])}".lower() if hasattr(anc, "get") else ""
            if "comment" in attrs or "coment" in attrs:
                in_comments = True
                break
        if in_comments:
            continue
        m = _LABEL_RE.match(text)
        n = _to_number(m.group(1)) if m else None
        if n is not None:
            out.append((n, text, "rótulo"))
    return out


def _collect_chapter_candidates(soup, pattern):
    """Junta os candidatos a 'número de capítulo' da página: texto dos links
    e (só com o regex padrão) rótulos curtos fora de links. Devolve uma
    lista de (numero, texto, tipo) na ordem em que aparecem."""
    candidates = []
    for a in soup.find_all("a"):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        # Links gigantes (que embrulham a página inteira) não valem. Mas um
        # link de linha de lista pode ter bastante texto (título, data,
        # curtidas), então só olha o começo dele em vez de descartar.
        if not text or len(text) > 400:
            continue
        head = text[:160]
        num = extract_chapter_number(head, pattern)
        if num is not None:
            candidates.append((num, head, "link"))
    if _is_default_pattern(pattern):
        labels = _label_candidates(soup)
        # um rótulo isolado pode ser comentário ou sinopse; uma lista de
        # vários capítulos diferentes é bem mais confiável
        if len({n for n, _, _ in labels}) >= 3:
            candidates.extend(labels)
    return candidates


def _main_cluster(nums, max_gap=5):
    """Os capítulos de UM mangá aparecem numa sequência 'colada' (222, 221,
    220...). Números soltos longe dessa sequência costumam ser de outros
    títulos na mesma página (recomendados, atualizações recentes) ou
    comentários. Devolve os números da maior sequência; se não houver uma
    sequência clara (menos de 3 números juntos), não filtra nada."""
    vals = sorted(set(nums))
    if len(vals) < 3:
        return vals
    clusters, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= max_gap:
            cur.append(v)
        else:
            clusters.append(cur)
            cur = [v]
    clusters.append(cur)
    best = max(clusters, key=lambda c: (len(c), c[-1]))
    if len(best) < 3:
        return vals
    return best


def _describe_candidates(candidates, chosen=None):
    if not candidates:
        return None

    def short(t, n=40):
        return re.sub(r"\s+", " ", t or "").strip()[:n]

    top = sorted(candidates, key=lambda c: c[0], reverse=True)[:6]
    top_txt = " | ".join(f"{n:g} <- '{short(t)}' ({kind})" for n, t, kind in top)
    order = ", ".join(f"{n:g}" for n, _, _ in candidates[:12])
    lines = [
        f"Maiores números vistos: {top_txt}",
        f"Ordem em que aparecem na página: {order}",
    ]
    if chosen:
        ignored = sorted({n for n, _, _ in candidates} - set(chosen), reverse=True)
        lines.append(
            f"Sequência usada: {min(chosen):g} a {max(chosen):g} ({len(chosen)} números)"
            + (f"; ignorados por ficarem soltos: {', '.join(f'{n:g}' for n in ignored[:8])}" if ignored else "")
        )
    return "\n".join(lines)


def auto_detect_chapter_text(soup, pattern, record=False):
    """Sem seletor manual, tenta achar sozinho os números de capítulo da
    página (texto dos links e rótulos curtos). Retorna só o texto desses
    candidatos — não a página inteira — pra evitar ruído de comentários,
    curtidas, contador de visitas etc. Retorna None se não achar nenhum.
    Com o regex padrão, só vale a maior sequência de capítulos 'colados'
    (números soltos de outros títulos ficam de fora). Com record=True,
    guarda um resumo do que viu pro diagnóstico."""
    candidates = _collect_chapter_candidates(soup, pattern)
    default = _is_default_pattern(pattern)
    chosen = _main_cluster([n for n, _, _ in candidates]) if (candidates and default) else None
    if record:
        _debug_local.candidates_info = _describe_candidates(candidates, chosen)
    if not candidates:
        return None
    if default:
        keep = set(chosen)
        ranked = sorted((c for c in candidates if c[0] in keep), key=lambda c: c[0], reverse=True)
        # texto padronizado: o número extraído depois é exatamente o certo,
        # sem se confundir com outros números que estejam no texto bruto
        seen, parts = set(), []
        for n, _, _ in ranked:
            if n not in seen:
                seen.add(n)
                parts.append(f"Capítulo {n:g}")
            if len(parts) >= 5:
                break
        return " | ".join(parts)
    ranked = sorted(candidates, key=lambda c: c[0], reverse=True)
    return " | ".join(t for _, t, _ in ranked[:5])


# ---------------------------------------------------------------------------
# Descoberta automática do número do capítulo (várias estratégias)
# ---------------------------------------------------------------------------
# Quando o texto dos links não tem algo como "Cap. 45", o app tenta outras
# pistas, da mais confiável pra menos confiável:
#   1. o texto dos links (auto_detect_chapter_text, acima);
#   2. a URL dos links ("/capitulo-45", "/chapter/45");
#   3. grupos de links numerados dentro da própria página do mangá
#      (".../one-piece/1191", ".../one-piece/1190", ...);
#   4. dados JSON — embutidos no HTML (__NEXT_DATA__ e similares) ou
#      carregados pela página em segundo plano (XHR/fetch).
# Só são usadas com o regex padrão: quem definiu um regex próprio controla
# a extração por conta própria.

_HREF_CHAPTER_RE = re.compile(
    r"(?:^|[/\-_])(?:cap[ií]tulo|capitulo|chapter|chap|epis[oó]dio|episodio|episode|cap|ch|ep|c)"
    r"[-_/.]?(\d+(?:[.,]\d+)?)/?$",
    re.IGNORECASE,
)

# Pedaços de URL que indicam paginação/listagens, não capítulos.
_BANNED_PREFIX_PARTS = {
    "page", "pagina", "p", "pg", "paged", "tag", "tags", "category", "categoria",
    "author", "autor", "year", "ano", "genre", "genero", "genres", "search",
    "busca", "user", "users",
}

_JSON_STRONG_KEYS = {
    "chapter", "chapternumber", "chapter_number", "chapternum", "chapter_num",
    "chapterno", "chapter_no", "chap", "numerocapitulo", "numero_capitulo",
    "capitulo", "cap", "episode", "episodenumber", "episode_number", "episodio",
}
_JSON_WEAK_KEYS = {"number", "num", "numero"}
_JSON_SIBLING_HINTS = {
    "title", "name", "slug", "created", "createdat", "created_at", "date",
    "published", "publishedat", "published_at", "updated", "updatedat",
    "updated_at", "release", "releasedat", "released_at", "url", "uuid", "id",
}


def _is_default_pattern(pattern):
    return pattern is None or pattern == DEFAULT_CHAPTER_REGEX


def _to_number(raw):
    if isinstance(raw, bool):
        return None
    try:
        n = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 20000 else None


def _number_from_value(v):
    if isinstance(v, (dict, list, bool)) or v is None:
        return None
    if isinstance(v, str):
        m = re.search(r"\d+(?:[.,]\d+)?", v)
        return _to_number(m.group()) if m else None
    return _to_number(v)


def _belongs_to_page(path, page_url):
    """Evita pegar links de OUTROS mangás (barra lateral de 'últimas
    atualizações', 'populares'...): o link precisa estar debaixo do caminho
    da página do mangá ou conter o nome (slug) dele."""
    base = urlparse(page_url or "").path.rstrip("/")
    if not base:
        return True
    low = path.lower()
    if low.startswith(base.lower() + "/"):
        return True
    slug = base.split("/")[-1].lower()
    return len(slug) >= 4 and slug in low


def _chapter_from_hrefs(soup, page_url):
    keyword_nums = []
    groups = {}
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        path = urlparse(urljoin(page_url or "", href)).path.rstrip("/")
        if not path or not _belongs_to_page(path, page_url):
            continue
        m = _HREF_CHAPTER_RE.search(path)
        if m:
            n = _to_number(m.group(1))
            if n is not None:
                keyword_nums.append(n)
            continue
        prefix, _, last = path.rpartition("/")
        if last.isdigit():
            n = _to_number(last)
            parent = prefix.rsplit("/", 1)[-1].lower()
            if n is not None and parent not in _BANNED_PREFIX_PARTS:
                groups.setdefault(prefix, []).append(n)
    if keyword_nums:
        return max(keyword_nums), "href com palavra"
    best = max(groups.values(), key=len, default=None)
    if best and len(best) >= 3:
        return max(best), "href numerado"
    return None


def _norm_json_key(k):
    return re.sub(r"[^a-z_]", "", str(k).lower())


def _chapter_list_candidate(items, parent_key):
    """Se 'items' parece uma lista de capítulos (vários objetos com um campo
    de número de capítulo), devolve (maior_número, quantos, é_forte)."""
    dicts = [i for i in items if isinstance(i, dict)]
    if len(dicts) < 3:
        return None
    parent_hint = any(w in _norm_json_key(parent_key) for w in ("chap", "cap", "episod"))
    for key_set, strong in ((_JSON_STRONG_KEYS, True), (_JSON_WEAK_KEYS, False)):
        nums = []
        for d in dicts:
            for k, v in d.items():
                if _norm_json_key(k) in key_set:
                    n = _number_from_value(v)
                    if n is not None:
                        nums.append(n)
                        break
        if len(nums) < 3 or len(nums) < 0.6 * len(dicts):
            continue
        if len(set(nums)) < 0.8 * len(nums):
            continue  # capítulos não se repetem; isso deve ser outra coisa
        if not strong and not parent_hint:
            hints = sum(
                1 for d in dicts if any(_norm_json_key(k) in _JSON_SIBLING_HINTS for k in d)
            )
            if hints < 0.6 * len(dicts):
                continue
        return max(nums), len(nums), strong or parent_hint
    return None


def _walk_json(obj, parent_key="", out=None, depth=0):
    if out is None:
        out = []
    if depth > 8 or len(out) > 50:
        return out
    if isinstance(obj, list):
        cand = _chapter_list_candidate(obj, parent_key)
        if cand:
            out.append(cand)
        for item in obj[:200]:
            if isinstance(item, (dict, list)):
                _walk_json(item, parent_key, out, depth + 1)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _walk_json(v, k, out, depth + 1)
    return out


def _chapter_from_json(objects):
    candidates = []
    for obj in objects:
        try:
            candidates.extend(_walk_json(obj))
        except RecursionError:
            continue
    if not candidates:
        return None
    # prefere listas com campo "forte" e, entre elas, a mais longa
    candidates.sort(key=lambda c: (c[2], c[1]), reverse=True)
    return candidates[0][0], "dados JSON"


def _json_from_scripts(soup):
    out = []
    for sc in soup.find_all("script"):
        typ = (sc.get("type") or "").lower()
        if "json" not in typ and sc.get("id") != "__NEXT_DATA__":
            continue
        text = (sc.string or sc.get_text() or "").strip()
        if not text or len(text) > 2_000_000:
            continue
        try:
            out.append(json.loads(text))
        except ValueError:
            continue
    return out


def discover_chapter_number(soup, json_sources=None, page_url=None):
    """Tenta achar o número do capítulo mais recente por pistas além do texto
    dos links. Devolve (numero, como_achou) ou None."""
    found = _chapter_from_hrefs(soup, page_url)
    if found:
        return found
    objects = list(json_sources or []) + _json_from_scripts(soup)
    return _chapter_from_json(objects)


def _extract_from_soup(soup, selector, pattern, json_sources=None, page_url=None):
    _debug_local.candidates_info = None
    if selector:
        el = soup.select_one(selector)
        if el is not None:
            txt = el.get_text(" ", strip=True)
            _debug_local.candidates_info = f"Via seletor CSS manual. Texto lido: '{txt[:80]}'"
            return txt
        # o seletor não achou nada (site mudou?): segue pra detecção automática
    if pattern:
        auto_text = auto_detect_chapter_text(soup, pattern, record=True)
        if auto_text:
            return auto_text
    if _is_default_pattern(pattern):
        found = discover_chapter_number(soup, json_sources, page_url)
        if found:
            num, how = found
            print(f"[detect] capítulo {num:g} encontrado via {how}")
            _debug_local.candidates_info = f"Via {how} (os links/rótulos da página não tinham o número)"
            return f"Capítulo {num:g}"
    return soup.get_text(" ", strip=True)


def fetch_text_simple(url, selector, pattern=None):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_from_soup(soup, selector, pattern, None, url)


# Textos comuns de abas/botões que escondem a lista de capítulos, tipo
# "Capítulos", "Capítulos (120)", "Lista de capítulos", "Chapters", "Episódios".
_CHAPTER_TAB_REGEX = re.compile(
    r"^\s*(lista de )?(cap[ií]tulos?|chapters?|epis[oó]dios?)(\s*[\(\[]?\s*\d+\s*[\)\]]?)?\s*$",
    re.IGNORECASE,
)


def _chapters_already_visible(html, selector, pattern, page_url=None, json_sources=None):
    """Diz se o app já consegue achar a lista/número de capítulos na página
    (sem precisar clicar em nada)."""
    soup = BeautifulSoup(html, "html.parser")
    if selector and soup.select_one(selector) is not None:
        return True
    if auto_detect_chapter_text(soup, pattern or DEFAULT_CHAPTER_REGEX) is not None:
        return True
    if _is_default_pattern(pattern):
        return discover_chapter_number(soup, json_sources, page_url) is not None
    return False


def _try_open_chapter_tab(page, is_visible, max_seconds=8):
    """Alguns sites só mostram a lista de capítulos depois de clicar numa
    aba ou botão (ex: "Capítulos"). Procura esse tipo de aba/botão e
    clica, parando assim que a lista aparecer. Tenta no máximo 2 itens de
    cada tipo e respeita um limite total de tempo, pra não demorar nem
    mexer demais na página. 'is_visible' é uma função que diz se a lista
    já apareceu. Devolve True se apareceu."""
    deadline = time.monotonic() + max_seconds
    locators = (
        page.get_by_role("tab", name=_CHAPTER_TAB_REGEX),
        page.get_by_role("button", name=_CHAPTER_TAB_REGEX),
        page.get_by_text(_CHAPTER_TAB_REGEX),
    )
    for locator in locators:
        try:
            total = locator.count()
        except Exception:
            continue
        for i in range(min(total, 2)):
            if time.monotonic() > deadline:
                return False
            try:
                locator.nth(i).click(timeout=2000)
            except Exception:
                continue
            # dá até ~3s pra lista aparecer depois do clique
            waited = 0
            while waited <= 3000:
                if is_visible():
                    return True
                page.wait_for_timeout(500)
                waited += 500
    return False


def _build_browser_diagnostic(html, json_responses, tab_clicked):
    """Resumo curto do que o navegador viu, pra entender por que a lista de
    capítulos não apareceu sem precisar abrir o Inspecionar na mão."""
    soup = BeautifulSoup(html, "html.parser")

    def short(t, n=40):
        t = re.sub(r"\s+", " ", t or "").strip()
        return t[:n]

    buttons = []
    for el in soup.select("button, [role=tab], [role=button]"):
        t = short(el.get_text(" ", strip=True))
        if t and t not in buttons:
            buttons.append(t)
        if len(buttons) >= 15:
            break

    links = []
    for a in soup.find_all("a"):
        t = short(a.get_text(" ", strip=True), 50)
        if t and re.search(r"\d", t) and t not in links:
            links.append(t)
        if len(links) >= 10:
            break

    json_lines = [f"{status} {u} -> {body}" for u, status, body in json_responses[:8]]

    if tab_clicked is None:
        click_txt = "não"
    else:
        click_txt = f"sim (lista apareceu: {'sim' if tab_clicked else 'não'})"
    parts = [
        f"Clique em aba tentado: {click_txt}",
        f"Abas/botões vistos: {buttons}",
        f"Links com número: {links}",
        "Requisições JSON vistas: " + (" || ".join(json_lines) if json_lines else "nenhuma"),
    ]
    return "\n".join(parts)


def _render_with_browser(sync_playwright_factory, url, selector, pattern):
    """Abre o navegador, navega, espera telas de verificação liberarem,
    tenta abrir a aba de capítulos / rolar a página se a lista estiver
    escondida, e devolve (html_final, lista_de_dados_json_vistos)."""
    json_sources = []
    with sync_playwright_factory() as p:
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

            # Anota as requisições internas (XHR/fetch) que a página faz: a
            # lista de capítulos muitas vezes chega por aí, em JSON.
            seen_responses = []
            json_cache = {}  # id(resposta) -> (dados, trecho) ou (None, None)

            def _on_response(resp):
                try:
                    if resp.request.resource_type in ("xhr", "fetch") and len(seen_responses) < 40:
                        seen_responses.append(resp)
                except Exception:
                    pass

            page.on("response", _on_response)

            def _json_sources():
                out = []
                for resp in list(seen_responses):
                    key = id(resp)
                    if key not in json_cache:
                        data, snippet = None, None
                        try:
                            ctype = (resp.headers.get("content-type") or "").lower()
                            if "json" in ctype:
                                body = resp.text()
                                if body and len(body) <= 2_000_000:
                                    data = json.loads(body)
                                    snippet = re.sub(r"\s+", " ", body)[:150]
                        except Exception:
                            data, snippet = None, None
                        json_cache[key] = (data, snippet)
                    if json_cache[key][0] is not None:
                        out.append(json_cache[key][0])
                return out

            def _visible_now():
                return _chapters_already_visible(page.content(), selector, pattern, url, _json_sources())

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
                    page.wait_for_selector(selector, timeout=5000)
                except Exception:
                    pass  # segue mesmo se o seletor não aparecer a tempo
            html = page.content()

            # Alguns serviços de proteção (tipo Cloudflare) mostram uma tela
            # de "verificação de segurança" por alguns segundos antes de
            # liberar o conteúdo real. Se detectar essa tela, espera um
            # pouco (até 10s no total, em 2 tentativas) e tenta pegar o
            # conteúdo de novo, em vez de desistir na hora. Esperar mais que
            # isso só segura o navegador (e a fila de checagens) à toa.
            attempts = 0
            while _looks_like_challenge_page(html) and attempts < 2:
                page.wait_for_timeout(5000)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                html = page.content()
                attempts += 1

            # Se a lista de capítulos ainda não apareceu, tenta (1) abrir a
            # aba "Capítulos" e (2) rolar a página, pra listas preguiçosas.
            tab_result = None  # None = não tentou clicar; True/False = resultado
            if not _looks_like_challenge_page(html) and not _visible_now():
                try:
                    tab_result = _try_open_chapter_tab(page, _visible_now)
                except Exception as e:
                    tab_result = False
                    print(f"[browser] não consegui abrir a aba de capítulos: {e}")
                if not tab_result:
                    try:
                        for _ in range(2):
                            page.mouse.wheel(0, 2500)
                            page.wait_for_timeout(800)
                            if _visible_now():
                                break
                    except Exception as e:
                        print(f"[browser] não consegui rolar a página: {e}")

            html = page.content()
            json_sources = _json_sources()

            # Se mesmo assim a lista não apareceu, junta um diagnóstico do que
            # o navegador viu (só nesse caso, pra não gastar tempo à toa).
            _debug_local.info = None
            if not _looks_like_challenge_page(html) and not _chapters_already_visible(
                html, selector, pattern, url, json_sources
            ):
                json_responses = []
                for resp in seen_responses:
                    data, snippet = json_cache.get(id(resp), (None, None))
                    if data is not None:
                        json_responses.append((resp.url, resp.status, snippet))
                try:
                    _debug_local.info = _build_browser_diagnostic(html, json_responses, tab_result)
                except Exception as e:
                    _debug_local.info = f"(não consegui montar o diagnóstico: {e})"
        finally:
            browser.close()
    return html, json_sources


def fetch_text_js(url, selector, pattern=None):
    """Renderiza a página com um navegador antes de extrair o texto.
    Necessário pra sites que montam a lista de capítulos via JavaScript
    (ex: MangaPlus) ou que bloqueiam requisições simples (bot detection).

    Tenta usar o patchright (versão do Playwright com correções
    anti-detecção) primeiro; se ele não estiver instalado OU falhar por
    qualquer motivo (ex: navegador dele não foi baixado no build), cai de
    volta pro Playwright normal automaticamente, sem quebrar a checagem.

    Só um navegador roda por vez em todo o app (veja _BROWSER_LOCK) —
    outras checagens que precisarem de navegador esperam a vez, em vez de
    abrir tudo em paralelo e estourar a memória do servidor."""
    with _BROWSER_LOCK:
        result = None
        try:
            from patchright.sync_api import sync_playwright as patchright_sync
            result = _render_with_browser(patchright_sync, url, selector, pattern)
        except Exception as e:
            print(f"[fetch_text_js] patchright indisponível ou falhou ({e}); usando playwright normal.")

        if result is None:
            from playwright.sync_api import sync_playwright
            result = _render_with_browser(sync_playwright, url, selector, pattern)

    html, json_sources = result
    soup = BeautifulSoup(html, "html.parser")
    return _extract_from_soup(soup, selector, pattern, json_sources, url)


# Frases típicas de telas de "verificação de segurança" (Cloudflare e afins)
# que aparecem temporariamente antes do conteúdo real da página.
_CHALLENGE_PAGE_MARKERS = (
    "verificação de segurança",
    "verificando seu navegador",
    "checking your browser",
    "just a moment",
    "um momento...",
    "ddos protection by",
    "attention required",
)


def _looks_like_challenge_page(html):
    lower = html.lower()
    return any(marker in lower for marker in _CHALLENGE_PAGE_MARKERS)


# Sinais de que a página só mostra o conteúdo real depois de rodar JavaScript
# (ou está bloqueando a requisição, tipo verificação anti-bot).
_JS_WARNING_WORDS = ("javascript", "habilite", "enable", "ativar", "habilitar")


def looks_insufficient(text, has_selector, chapter_found):
    stripped = (text or "").strip()
    lower = stripped.lower()
    if "javascript" in lower and any(w in lower for w in _JS_WARNING_WORDS if w != "javascript"):
        return True
    # Se o número do capítulo foi achado, texto curto é normal (ex: só
    # "Cap. 1191" quando há seletor) e não precisa do navegador.
    if chapter_found:
        return False
    # Sem número de capítulo em lugar nenhum: sinal de página vazia, ainda
    # não renderizada, ou bloqueio/verificação anti-bot.
    return True


def fetch_text_auto(manga):
    """Busca o conteúdo da fonte, detectando automaticamente se precisa de
    navegador headless E, quando não há seletor manual, tentando achar
    sozinho o link de capítulo certo. Retorna (texto, usou_js)."""
    _debug_local.info = None
    _debug_local.candidates_info = None
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
    if chapter_num is not None:
        how = getattr(_debug_local, "candidates_info", None)
        conn.execute(
            "UPDATE mangas SET last_error = ? WHERE id = ?",
            (f"Capítulo detectado: {chapter_num:g}." + (f"\n{how}" if how else ""), manga_id),
        )

    if chapter_num is None:
        preview = (text or "").strip()[:200]
        msg = f"Nenhum número de capítulo encontrado. Prévia do conteúdo recebido: {preview!r}"
        extra = getattr(_debug_local, "info", None)
        if extra:
            msg += "\n" + extra
        conn.execute(
            "UPDATE mangas SET last_error = ? WHERE id = ?",
            (msg[:4000], manga_id),
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


@app.route("/group_chapter", methods=["POST"])
@login_required
def group_chapter():
    """Corrige o 'último capítulo notificado' de um grupo (ex: o app leu um
    número errado e passou a ignorar os capítulos de verdade)."""
    user_id = session["user_id"]
    group_name = request.form.get("group_name", "").strip()
    raw = request.form.get("chapter", "").strip().replace(",", ".")
    try:
        value = float(raw)
        if value < 0 or value >= 100000:
            raise ValueError
    except ValueError:
        flash("Informe um número de capítulo válido (ex: 222).")
        return redirect(url_for("index"))
    conn = get_db()
    owns_group = conn.execute(
        "SELECT 1 FROM mangas WHERE user_id = ? AND group_name = ?", (user_id, group_name)
    ).fetchone()
    if owns_group:
        get_group(conn, user_id, group_name)
        conn.execute(
            "UPDATE groups SET last_notified_chapter = ? WHERE user_id = ? AND group_name = ?",
            (value, user_id, group_name),
        )
        conn.commit()
        flash(f"Último capítulo notificado de '{group_name}' ajustado para {value:g}.")
    conn.close()
    return redirect(url_for("index"))


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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), threaded=True)
