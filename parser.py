#!/usr/bin/env python3
"""Pharos Global SMM — monitoring parser.

Collects headline, URL, date, source and a short description from the
sources in sources.json and builds an editorial queue. Full texts and images
are never stored. Nothing is published automatically.

Outputs (output/):
  news.json      all collected items (rolling, max 1000)
  digest.md      new items of this run + last 7 days, grouped by rubric
  queue.csv      items of the last 14 days in the column order of the
                 "Очередь" sheet — ready to paste/import
  errors.md      sources that failed in this run (switch to manual check)
  state.json     seen URLs and last fetch time per source
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
UA = "PharosGlobalSMMMonitor/1.0 (+editorial monitoring; headlines and links only)"
TIMEOUT = 25
NOW = datetime.now(timezone.utc)

QUEUE_COLUMNS = [
    "Заголовок", "Статус", "Рубрика", "Класс активов", "Юрисдикция",
    "Дата публикации", "Дата события / период", "Суть события",
    "Почему важно для рынка", "Комментарий Pharos Global", "Проверка (да/нет)",
    "Ответственный", "Источник", "URL источника", "URL первоисточника",
    "Запись на сайте", "Пост LinkedIn",
]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[ ./-]([A-Za-z]{3,9})[ ./-](\d{4})\b"), "dMy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})\b"), "Mdy"),
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), "dmy"),
]


# ---------- helpers ----------

def canonical(url: str) -> str:
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(q), ""))


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def parse_date(text: str) -> str | None:
    if not text:
        return None
    for rx, kind in DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
            elif kind == "dMy":
                d, mo, y = int(m[1]), MONTHS.get(m[2][:3].lower()), int(m[3])
            elif kind == "Mdy":
                mo, d, y = MONTHS.get(m[1][:3].lower()), int(m[2]), int(m[3])
            else:
                d, mo, y = int(m[1]), int(m[2]), int(m[3])
            if not mo:
                continue
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def matches_keywords(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed(url: str) -> tuple[bool, str]:
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code >= 500:
                _robots[base] = None
            else:
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                _robots[base] = rp
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    if rp is None:
        return False, "robots.txt не читается — ручной мониторинг"
    return (rp.can_fetch(UA, url), "запрещено robots.txt")


def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# ---------- collectors ----------

def collect_rss(src: dict) -> list[dict]:
    r = fetch(src["url"])
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries[:40]:
        date = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                date = time.strftime("%Y-%m-%d", e[key])
                break
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        items.append({"title": e.get("title", "").strip(), "url": e.get("link", "").strip(),
                      "date": date, "summary": summary[:300]})
    return items


def collect_html(src: dict) -> list[dict]:
    r = fetch(src["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    needles = [n.lower() for n in src.get("link_contains", [])]
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(src["url"], a["href"])
        if not href.startswith("http") or canonical(href) == canonical(src["url"]):
            continue
        if needles and not any(n in href.lower() for n in needles):
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 15:
            title = a.get("title") or title
        if len(title) < 15 or canonical(href) in seen:
            continue
        seen.add(canonical(href))
        # look for a date in the link's block (time tag or nearby text)
        block = a.find_parent(["li", "article", "tr", "div"]) or a
        t = block.find("time")
        date = parse_date(t.get("datetime", "") or t.get_text(" ")) if t else None
        if not date:
            date = parse_date(block.get_text(" ", strip=True)[:300])
        items.append({"title": title[:250], "url": href, "date": date, "summary": ""})
        if len(items) >= 40:
            break
    return items


# ---------- main ----------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main(force: bool = False) -> int:
    OUT.mkdir(exist_ok=True)
    cfg = load_json(ROOT / "sources.json", {})
    keywords = cfg.get("keywords", [])
    sources = cfg.get("sources", []) + [g for g in cfg.get("google_alerts", []) if g.get("url")]
    state = load_json(OUT / "state.json", {"seen": {}, "last_fetch": {}})
    news = load_json(OUT / "news.json", [])
    seen_titles = {norm_title(n["title"]) for n in news}
    new_items, errors = [], []

    for src in sources:
        last = state["last_fetch"].get(src["id"])
        if not force and last:
            if NOW - datetime.fromisoformat(last) < timedelta(hours=src.get("min_hours", 4)):
                continue
        ok, why = allowed(src["url"])
        if not ok:
            errors.append((src, why))
            continue
        try:
            raw = collect_rss(src) if src["type"] == "rss" else collect_html(src)
        except Exception as exc:  # network, HTTP or parse error -> manual check
            errors.append((src, f"{type(exc).__name__}: {str(exc)[:120]}"))
            continue
        state["last_fetch"][src["id"]] = NOW.isoformat()
        for it in raw:
            if not it["title"] or not it["url"]:
                continue
            cu = canonical(it["url"])
            nt = norm_title(it["title"])
            if cu in state["seen"] or nt in seen_titles:
                continue
            if src.get("filter") and not matches_keywords(it["title"] + " " + it["summary"], keywords):
                continue
            state["seen"][cu] = NOW.strftime("%Y-%m-%d")
            seen_titles.add(nt)
            item = {
                "title": it["title"], "url": cu, "date": it["date"],
                "first_seen": NOW.strftime("%Y-%m-%d"), "summary": it["summary"],
                "source": src["name"], "source_url": src["url"], "rubric": src["rubric"],
                "asset_class": src.get("asset_class", ""), "priority": src.get("priority", ""),
            }
            new_items.append(item)
        time.sleep(1)  # polite pacing between sources

    news = (new_items + news)[:1000]
    (OUT / "news.json").write_text(json.dumps(news, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    write_digest(news, new_items)
    write_queue(news)
    write_errors(errors)
    print(f"new: {len(new_items)} · total: {len(news)} · errors: {len(errors)}")
    return 0


def recent(news: list[dict], days: int) -> list[dict]:
    cut = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
    return [n for n in news if (n.get("date") or n["first_seen"]) >= cut or n["first_seen"] >= cut]


def write_digest(news: list[dict], new_items: list[dict]) -> None:
    lines = [f"# Pharos Global — мониторинг · {NOW:%Y-%m-%d %H:%M} UTC", "",
             f"Новых в этом запуске: **{len(new_items)}**", ""]
    week = recent(news, 7)
    for rubric in ["Markets & Macro", "Regulation", "Digital Assets", "Private Markets", "Alternatives"]:
        group = [n for n in week if n["rubric"] == rubric]
        if not group:
            continue
        lines += [f"## {rubric} ({len(group)})", ""]
        for n in sorted(group, key=lambda x: x.get("date") or x["first_seen"], reverse=True):
            mark = " 🆕" if n in new_items else ""
            d = n.get("date") or f"найдено {n['first_seen']}"
            lines.append(f"- **{n['source']}** · {d} · [{n['title']}]({n['url']}){mark}")
        lines.append("")
    lines += ["---", "Только заголовки и ссылки. Перед использованием — открыть первоисточник, "
              "сверить дату, период, юрисдикцию и статус изменения."]
    (OUT / "digest.md").write_text("\n".join(lines), encoding="utf-8")


def write_queue(news: list[dict]) -> None:
    with open(OUT / "queue.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(QUEUE_COLUMNS)
        for n in recent(news, 14):
            w.writerow([n["title"], "Кандидат", n["rubric"], n["asset_class"], "",
                        n.get("date") or "", "", n.get("summary", ""), "", "", "", "",
                        n["source"], n["source_url"], n["url"], "", ""])


def write_errors(errors: list) -> None:
    lines = [f"# Ошибки доступа · {NOW:%Y-%m-%d %H:%M} UTC", ""]
    if not errors:
        lines.append("Все источники ответили.")
    for src, why in errors:
        lines.append(f"- **{src['name']}** — {why} → проверить вручную: {src['url']}")
    (OUT / "errors.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
