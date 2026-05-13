"""
Roblox DevForum Monitor Bot.

Periodically polls the Roblox DevForum (Discourse JSON endpoints), detects
new topics, runs them through an AI model for analysis, and posts a
Brazilian-Portuguese summary to a Discord webhook.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterable

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("devforum-bot")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

URGENCY_COLORS = {
    "Low": 0x3498DB,       # blue
    "Medium": 0xF1C40F,    # yellow
    "High": 0xE67E22,      # orange
    "Critical": 0xE74C3C,  # red
}

URGENCY_PT = {
    "Low": "🟦 Baixa",
    "Medium": "🟨 Média",
    "High": "🟧 Alta",
    "Critical": "🟥 Crítica",
}

HTTP_TIMEOUT = 20
USER_AGENT = "RobloxDevForumMonitor/1.0 (+https://github.com/)"


VALID_PROVIDERS = ("openai", "gemini", "groq")


@dataclass
class Config:
    discord_webhook_url: str
    ai_provider: str
    openai_api_key: str
    openai_model: str
    gemini_api_key: str
    gemini_model: str
    groq_api_key: str
    groq_model: str
    check_interval_minutes: int
    devforum_base_url: str
    monitored_endpoints: list[str]
    keywords: list[str]
    ignore_keywords: list[str]
    max_topics_per_check: int
    database_path: str
    min_likes_default: int = 0
    min_likes_by_category: dict[int, int] = field(default_factory=dict)
    port: int = 8080
    healthcheck_enabled: bool = True
    _ai_client: Any = field(default=None, repr=False)

    @property
    def active_model(self) -> str:
        return {
            "openai": self.openai_model,
            "gemini": self.gemini_model,
            "groq": self.groq_model,
        }[self.ai_provider]


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def load_config() -> Config:
    load_dotenv()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("DISCORD_WEBHOOK_URL is required")

    provider = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise RuntimeError(
            f"AI_PROVIDER must be one of {VALID_PROVIDERS}, got {provider!r}"
        )

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    required_key = {"openai": openai_key, "gemini": gemini_key, "groq": groq_key}[provider]
    if not required_key:
        raise RuntimeError(
            f"AI_PROVIDER={provider} requires "
            f"{provider.upper()}_API_KEY to be set"
        )

    endpoints = _csv(os.environ.get("MONITORED_ENDPOINTS")) or ["/latest.json"]

    min_likes_map: dict[int, int] = {}
    for pair in _csv(os.environ.get("MIN_LIKES_BY_CATEGORY")):
        if "=" not in pair:
            continue
        cat_str, val_str = pair.split("=", 1)
        try:
            min_likes_map[int(cat_str.strip())] = int(val_str.strip())
        except ValueError:
            log.warning("Ignoring bad MIN_LIKES_BY_CATEGORY entry: %r", pair)

    cfg = Config(
        discord_webhook_url=webhook,
        ai_provider=provider,
        openai_api_key=openai_key,
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip(),
        gemini_api_key=gemini_key,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip(),
        groq_api_key=groq_key,
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant").strip(),
        check_interval_minutes=max(1, int(os.environ.get("CHECK_INTERVAL_MINUTES", "10"))),
        devforum_base_url=os.environ.get("DEVFORUM_BASE_URL", "https://devforum.roblox.com").rstrip("/"),
        monitored_endpoints=endpoints,
        keywords=[k.lower() for k in _csv(os.environ.get("KEYWORDS"))],
        ignore_keywords=[k.lower() for k in _csv(os.environ.get("IGNORE_KEYWORDS"))],
        max_topics_per_check=max(1, int(os.environ.get("MAX_TOPICS_PER_CHECK", "20"))),
        database_path=os.environ.get("DATABASE_PATH", "./data/devforum_bot.sqlite3"),
        min_likes_default=max(0, int(os.environ.get("MIN_LIKES", "0"))),
        min_likes_by_category=min_likes_map,
        port=int(os.environ.get("PORT", "8080")),
        healthcheck_enabled=os.environ.get("HEALTHCHECK", "1") != "0",
    )

    log.info(
        "Loaded config: provider=%s, model=%s, interval=%dmin, endpoints=%s, "
        "keywords=%d, ignore=%d, max=%d, min_likes=%d, min_likes_by_cat=%s",
        cfg.ai_provider,
        cfg.active_model,
        cfg.check_interval_minutes,
        cfg.monitored_endpoints,
        len(cfg.keywords),
        len(cfg.ignore_keywords),
        cfg.max_topics_per_check,
        cfg.min_likes_default,
        cfg.min_likes_by_category,
    )
    return cfg


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            item_id TEXT NOT NULL UNIQUE,
            topic_id TEXT,
            title TEXT,
            url TEXT,
            processed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_items_topic_id ON processed_items(topic_id)"
    )
    conn.commit()
    return conn


def is_already_processed(conn: sqlite3.Connection, item_id: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM processed_items WHERE item_id = ? LIMIT 1",
        (item_id,),
    )
    return cur.fetchone() is not None


def mark_processed(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_id: str,
    topic_id: str | None,
    title: str | None,
    url: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_items
            (item_type, item_id, topic_id, title, url, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item_type,
            item_id,
            topic_id,
            title,
            url,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
})


def fetch_json(url: str) -> dict[str, Any] | None:
    try:
        resp = _session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            log.warning("Rate-limited on %s, sleeping %ds", url, retry_after)
            time.sleep(min(retry_after, 60))
            resp = _session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# DevForum parsing
# ---------------------------------------------------------------------------

@dataclass
class Topic:
    topic_id: int
    title: str
    slug: str
    url: str
    category_id: int | None
    tags: list[str]
    created_at: str | None
    bumped_at: str | None
    excerpt: str | None
    like_count: int = 0


def get_latest_topics(cfg: Config, endpoint: str) -> list[Topic]:
    url = cfg.devforum_base_url + (endpoint if endpoint.startswith("/") else "/" + endpoint)
    data = fetch_json(url)
    if not data:
        return []

    topic_list = (data.get("topic_list") or {}).get("topics") or []
    topics: list[Topic] = []
    for raw in topic_list[: cfg.max_topics_per_check]:
        try:
            topic_id = int(raw["id"])
            slug = raw.get("slug") or str(topic_id)
            topic_url = f"{cfg.devforum_base_url}/t/{slug}/{topic_id}"
            like_count = int(
                raw.get("like_count")
                or raw.get("op_like_count")
                or raw.get("thumbs_up_count")
                or 0
            )
            topics.append(Topic(
                topic_id=topic_id,
                title=raw.get("title") or "",
                slug=slug,
                url=topic_url,
                category_id=raw.get("category_id"),
                tags=list(raw.get("tags") or []),
                created_at=raw.get("created_at"),
                bumped_at=raw.get("bumped_at"),
                excerpt=raw.get("excerpt"),
                like_count=like_count,
            ))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed topic in %s: %s", endpoint, exc)
    return topics


def fetch_topic_details(cfg: Config, topic: Topic) -> dict[str, Any] | None:
    url = f"{cfg.devforum_base_url}/t/{topic.topic_id}.json"
    return fetch_json(url)


def clean_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<script.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _haystack(topic: Topic) -> str:
    parts = [topic.title or "", topic.excerpt or "", " ".join(topic.tags)]
    return " ".join(parts).lower()


def min_likes_for(cfg: Config, category_id: int | None) -> int:
    if category_id is not None and category_id in cfg.min_likes_by_category:
        return cfg.min_likes_by_category[category_id]
    return cfg.min_likes_default


def should_process_topic(topic: Topic, cfg: Config) -> tuple[bool, str]:
    """Return (accept, reason). reason='likes' means try again later."""
    text = _haystack(topic)
    if cfg.ignore_keywords and any(k in text for k in cfg.ignore_keywords):
        return False, "ignored"

    if cfg.keywords and not any(k in text for k in cfg.keywords):
        return False, "no_keyword"

    required_likes = min_likes_for(cfg, topic.category_id)
    if required_likes > 0 and topic.like_count < required_likes:
        return False, "likes"

    return True, "ok"


# ---------------------------------------------------------------------------
# AI analysis
# ---------------------------------------------------------------------------

AI_SYSTEM_PROMPT = (
    "You are an expert Roblox developer assistant. Analyze Roblox DevForum "
    "posts for a Roblox game developer. Write in Brazilian Portuguese. Be "
    "practical, concise, and highlight concrete impact."
)

AI_JSON_SCHEMA_TEXT = """Responda APENAS com JSON válido neste schema:
{
  "summary": "string (resumo curto em pt-BR)",
  "context": "string (contexto da mudança/notícia)",
  "impact": "string (por que importa para devs Roblox e impacto em jogos)",
  "recommended_action": "string (ação recomendada)",
  "urgency": "Low | Medium | High | Critical",
  "developer_notes": "string (notas técnicas relevantes)"
}"""


def _extract_first_post(details: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    posts = ((details.get("post_stream") or {}).get("posts")) or []
    if not posts:
        return "", None
    first = posts[0]
    cooked = first.get("cooked") or ""
    return clean_html(cooked), first


def _build_user_prompt(topic: Topic, details: dict[str, Any] | None) -> str:
    body = ""
    created = topic.created_at
    if details:
        body, first = _extract_first_post(details)
        if first and first.get("created_at"):
            created = first.get("created_at")
        category = details.get("category_id") or topic.category_id
        tags = details.get("tags") or topic.tags
    else:
        category = topic.category_id
        tags = topic.tags

    body = body or topic.excerpt or ""
    if len(body) > 6000:
        body = body[:6000] + "…"

    return (
        f"Título: {topic.title}\n"
        f"URL: {topic.url}\n"
        f"Categoria ID: {category}\n"
        f"Tags: {', '.join(tags) if tags else '(nenhuma)'}\n"
        f"Criado em: {created}\n"
        f"Bumped em: {topic.bumped_at}\n"
        f"Excerpt: {topic.excerpt or '(sem excerpt)'}\n"
        f"\n--- Conteúdo do post ---\n{body}\n"
        f"\n{AI_JSON_SCHEMA_TEXT}"
    )


def _parse_ai_json(content: str) -> dict[str, Any] | None:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        if content.endswith("```"):
            content = content[:-3].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _fallback_analysis(topic: Topic, body_preview: str, reason: str) -> dict[str, Any]:
    excerpt = (body_preview or topic.excerpt or "").strip()
    if len(excerpt) > 500:
        excerpt = excerpt[:500] + "…"
    return {
        "summary": excerpt or topic.title,
        "context": "Análise automática indisponível — exibindo trecho original.",
        "impact": "Avaliar manualmente abrindo o link.",
        "recommended_action": "Abrir o tópico e revisar com atenção.",
        "urgency": "Low",
        "developer_notes": f"Falha na IA: {reason}",
    }


def _get_openai_client(cfg: Config, *, base_url: str | None = None, api_key: str):
    """Lazy-import the OpenAI SDK (used by both 'openai' and 'groq' providers)."""
    if cfg._ai_client is None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required for OpenAI/Groq providers. "
                "Install with `pip install openai`."
            ) from exc
        cfg._ai_client = OpenAI(api_key=api_key, base_url=base_url)
    return cfg._ai_client


def _call_openai_compat(cfg: Config, user_prompt: str, *, base_url: str | None,
                        api_key: str, model: str, use_json_mode: bool) -> str:
    client = _get_openai_client(cfg, base_url=base_url, api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
    }
    if use_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _call_openai(cfg: Config, user_prompt: str) -> str:
    return _call_openai_compat(
        cfg, user_prompt,
        base_url=None,
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        use_json_mode=True,
    )


def _call_groq(cfg: Config, user_prompt: str) -> str:
    # Groq exposes an OpenAI-compatible endpoint and supports JSON mode.
    return _call_openai_compat(
        cfg, user_prompt,
        base_url="https://api.groq.com/openai/v1",
        api_key=cfg.groq_api_key,
        model=cfg.groq_model,
        use_json_mode=True,
    )


def _call_gemini(cfg: Config, user_prompt: str) -> str:
    if cfg._ai_client is None:
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "google-generativeai package is required for Gemini. "
                "Install with `pip install google-generativeai`."
            ) from exc
        genai.configure(api_key=cfg.gemini_api_key)
        cfg._ai_client = genai.GenerativeModel(
            model_name=cfg.gemini_model,
            system_instruction=AI_SYSTEM_PROMPT,
        )

    resp = cfg._ai_client.generate_content(
        user_prompt,
        generation_config={
            "temperature": 0.3,
            "response_mime_type": "application/json",
        },
    )
    # Gemini SDK exposes .text; fall back to candidates if blocked/empty.
    text = getattr(resp, "text", None)
    if text:
        return text
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        for p in parts:
            t = getattr(p, "text", None)
            if t:
                return t
    return ""


_PROVIDER_DISPATCH = {
    "openai": _call_openai,
    "gemini": _call_gemini,
    "groq": _call_groq,
}


def analyze_with_ai(cfg: Config, topic: Topic, details: dict[str, Any] | None) -> dict[str, Any]:
    user_prompt = _build_user_prompt(topic, details)
    body_preview = _extract_first_post(details)[0] if details else (topic.excerpt or "")
    call = _PROVIDER_DISPATCH[cfg.ai_provider]
    last_error = "unknown"

    for attempt in (1, 2):
        try:
            content = call(cfg, user_prompt)
            parsed = _parse_ai_json(content)
            if parsed and parsed.get("summary"):
                urgency = parsed.get("urgency", "Low")
                if urgency not in URGENCY_COLORS:
                    parsed["urgency"] = "Low"
                return parsed
            last_error = "JSON inválido retornado pelo modelo"
            log.warning("[%s] AI returned unparseable JSON on attempt %d: %s",
                        cfg.ai_provider, attempt, (content or "")[:300])
        except Exception as exc:  # broad: every SDK raises its own classes
            last_error = f"{type(exc).__name__}: {exc}"
            log.error("[%s] AI call failed (attempt %d): %s",
                      cfg.ai_provider, attempt, last_error)
            time.sleep(2)

    return _fallback_analysis(topic, body_preview, last_error)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "—"
    return text[: limit - 1] + "…"


def send_discord_embed(cfg: Config, topic: Topic, analysis: dict[str, Any]) -> bool:
    urgency = analysis.get("urgency", "Low")
    color = URGENCY_COLORS.get(urgency, URGENCY_COLORS["Low"])
    urgency_label = URGENCY_PT.get(urgency, urgency)

    embed = {
        "title": "📰 Novo post no Roblox DevForum",
        "url": topic.url,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Título", "value": _truncate(topic.title, 1024), "inline": False},
            {"name": "Urgência", "value": urgency_label, "inline": True},
            {"name": "Categoria/Tags",
             "value": _truncate(", ".join(topic.tags) or "—", 1024), "inline": True},
            {"name": "Curtidas", "value": f"❤ {topic.like_count}", "inline": True},
            {"name": "Resumo", "value": _truncate(analysis.get("summary"), 1024), "inline": False},
            {"name": "Contexto", "value": _truncate(analysis.get("context"), 1024), "inline": False},
            {"name": "Impacto para jogos Roblox",
             "value": _truncate(analysis.get("impact"), 1024), "inline": False},
            {"name": "Ação recomendada",
             "value": _truncate(analysis.get("recommended_action"), 1024), "inline": False},
            {"name": "Notas técnicas",
             "value": _truncate(analysis.get("developer_notes"), 1024), "inline": False},
            {"name": "Referência", "value": topic.url, "inline": False},
        ],
        "footer": {"text": "Roblox DevForum Monitor • AI summary"},
    }

    payload = {"embeds": [embed]}

    for attempt in range(1, 4):
        try:
            resp = _session.post(
                cfg.discord_webhook_url,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 429:
                data = {}
                try:
                    data = resp.json()
                except ValueError:
                    pass
                retry_after = float(data.get("retry_after", resp.headers.get("Retry-After", 2)))
                log.warning("Discord rate-limited, retrying in %.1fs", retry_after)
                time.sleep(min(retry_after + 0.5, 30))
                continue
            if 200 <= resp.status_code < 300:
                return True
            log.error("Discord error %d: %s", resp.status_code, resp.text[:300])
            time.sleep(2 * attempt)
        except requests.RequestException as exc:
            log.error("Discord request failed (attempt %d): %s", attempt, exc)
            time.sleep(2 * attempt)

    return False


# ---------------------------------------------------------------------------
# Healthcheck (Railway-friendly)
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # silence default logging
        return


def start_healthcheck(port: int) -> None:
    def _run():
        try:
            server = HTTPServer(("0.0.0.0", port), _HealthHandler)
            log.info("Healthcheck listening on :%d", port)
            server.serve_forever()
        except OSError as exc:
            log.warning("Healthcheck disabled (%s)", exc)

    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _iter_unique_topics(topics: Iterable[Topic]) -> Iterable[Topic]:
    seen: set[int] = set()
    for t in topics:
        if t.topic_id in seen:
            continue
        seen.add(t.topic_id)
        yield t


def process_once(cfg: Config, conn: sqlite3.Connection) -> None:
    log.info("Polling DevForum endpoints…")
    all_topics: list[Topic] = []
    for endpoint in cfg.monitored_endpoints:
        all_topics.extend(get_latest_topics(cfg, endpoint))

    new_count = 0
    skip_count = 0
    pending_count = 0
    for topic in _iter_unique_topics(all_topics):
        item_id = f"topic:{topic.topic_id}"
        if is_already_processed(conn, item_id):
            continue
        accept, reason = should_process_topic(topic, cfg)
        if not accept:
            if reason == "likes":
                # Don't persist — likes may rise; re-evaluate next cycle.
                pending_count += 1
                continue
            mark_processed(
                conn,
                item_type=f"topic_skipped:{reason}",
                item_id=item_id,
                topic_id=str(topic.topic_id),
                title=topic.title,
                url=topic.url,
            )
            skip_count += 1
            continue

        log.info("Analyzing topic %d (cat=%s, likes=%d): %s",
                 topic.topic_id, topic.category_id, topic.like_count, topic.title)
        details = fetch_topic_details(cfg, topic)
        analysis = analyze_with_ai(cfg, topic, details)

        if send_discord_embed(cfg, topic, analysis):
            mark_processed(
                conn,
                item_type="topic",
                item_id=item_id,
                topic_id=str(topic.topic_id),
                title=topic.title,
                url=topic.url,
            )
            new_count += 1
            time.sleep(1.2)  # be polite to Discord
        else:
            log.error("Discord send failed for topic %d, will retry next cycle", topic.topic_id)

    log.info("Cycle done: %d sent, %d filtered, %d under likes threshold, %d total fetched",
             new_count, skip_count, pending_count, len(all_topics))


def main_loop() -> None:
    cfg = load_config()
    conn = init_db(cfg.database_path)

    if cfg.healthcheck_enabled:
        start_healthcheck(cfg.port)

    interval_sec = cfg.check_interval_minutes * 60
    log.info("Starting polling loop every %d minute(s)", cfg.check_interval_minutes)

    while True:
        started = time.time()
        try:
            process_once(cfg, conn)
        except Exception as exc:  # never crash the loop
            log.exception("Unexpected error in cycle: %s", exc)

        elapsed = time.time() - started
        sleep_for = max(5, interval_sec - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down")
