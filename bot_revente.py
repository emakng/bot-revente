"""
Bot Telegram Bons Plans Revente Gaming - V1 Dealabs
---------------------------------------------------
Objectif : surveiller Dealabs pour trouver des bons plans revendables
sur Vinted/Leboncoin : jeux PS5 physiques, jeux Switch physiques,
manettes, Joy-Con, casques/accessoires gaming petits formats.

Commandes Telegram :
/start_revente ou /start : active les alertes
/check : lance un scan manuel
/status : affiche l'état du bot
/stop_revente : désactive les alertes
/help : aide

Variables d'environnement Render :
TELEGRAM_TOKEN=xxxx
CHECK_INTERVAL_SECONDS=300
MIN_SCORE_ALERT=8
DEALABS_FEEDS=https://www.dealabs.com/rss/hot,https://www.dealabs.com/rss/new
DEALABS_SEARCH_QUERIES=jeu PS5,jeux PS5,jeu Switch,DualSense,Joy-Con,manette PS5,casque gaming
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

APP_NAME = "Bot Revente Gaming V1"
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
STATE_FILE = DATA_DIR / "revente_state.json"
KEYWORDS_FILE = Path(os.environ.get("KEYWORDS_FILE", "config_keywords.json"))
RULES_FILE = Path(os.environ.get("RULES_FILE", "config_resale_rules.json"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
MIN_SCORE_ALERT = int(os.environ.get("MIN_SCORE_ALERT", "8"))
MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "5"))
MANUAL_INCLUDE_LOW_SCORES = os.environ.get("MANUAL_INCLUDE_LOW_SCORES", "false").lower() == "true"
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))

DEFAULT_DEALABS_FEEDS = [
    "https://www.dealabs.com/rss/hot",
    "https://www.dealabs.com/rss/new",
]
DEFAULT_SEARCH_QUERIES = [
    "jeu PS5",
    "jeux PS5",
    "PS5 boite",
    "version physique PS5",
    "jeu Nintendo Switch",
    "jeux Switch",
    "cartouche Switch",
    "DualSense",
    "manette PS5",
    "Joy-Con",
    "manette Switch Pro",
    "casque gaming",
    "Pulse 3D",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(APP_NAME)


@dataclass
class Deal:
    id: str
    title: str
    url: str
    price: Optional[float]
    price_text: str
    merchant: str
    source: str = "Dealabs"
    temperature: Optional[float] = None
    image: Optional[str] = None
    description: str = ""


@dataclass
class Analysis:
    category: str
    resale_min: Optional[float]
    resale_max: Optional[float]
    margin_min: Optional[float]
    margin_max: Optional[float]
    score: int
    reasons: List[str]
    alert_type: str
    format_label: str
    action_label: str


DEFAULT_KEYWORDS = {
    "urgent": [
        "erreur de prix",
        "bug de prix",
        "prix jamais vu",
        "prix fou",
        "optimisation",
        "-70%",
        "-80%",
        "-90%",
    ],
    "physical_games": [
        "jeu ps5",
        "jeux ps5",
        "ps5 boîte",
        "ps5 boite",
        "version physique ps5",
        "disque ps5",
        "jeu playstation 5",
        "jeux playstation 5",
        "jeu nintendo switch",
        "jeux nintendo switch",
        "jeu switch",
        "jeux switch",
        "cartouche switch",
        "version physique switch",
    ],
    "accessories": [
        "dualsense",
        "manette ps5",
        "manette playstation",
        "joy-con",
        "joy con",
        "manette switch pro",
        "casque gaming",
        "pulse 3d",
        "inzone",
        "razer",
        "logitech g",
        "steelseries",
        "hyperx",
        "turtle beach",
        "station de recharge",
        "microsd switch",
        "micro sd switch",
    ],
    "trusted_merchants": [
        "amazon",
        "fnac",
        "carrefour",
        "leclerc",
        "cultura",
        "micromania",
        "auchan",
        "cdiscount",
        "boulanger",
        "rakuten",
    ],
    "exclusions": [
        "dématérialisé",
        "dematerialise",
        "digital",
        "code psn",
        "clé steam",
        "cle steam",
        "steam key",
        "xbox",
        "game pass",
        "abonnement",
        "occasion",
        "reconditionné",
        "reconditionne",
        "vendeur tiers",
        "marketplace",
        "pc uniquement",
        "téléchargement",
        "telechargement",
        "console nintendo switch",
        "console switch",
        "console ps5",
        "playstation 5 slim",
        "pack console",
        "bundle console",
    ],
}

DEFAULT_RULES = {
    "categories": {
        "dualsense": {
            "patterns": ["dualsense", "manette ps5", "manette playstation 5"],
            "label": "Manette PS5 / DualSense",
            "format": "petit colis",
            "resale_min": 45,
            "resale_max": 58,
            "buy_max": 40,
            "base_score": 4,
        },
        "joycon": {
            "patterns": ["joy-con", "joy con"],
            "label": "Joy-Con Switch",
            "format": "petit colis",
            "resale_min": 45,
            "resale_max": 65,
            "buy_max": 35,
            "base_score": 4,
        },
        "switch_pro_controller": {
            "patterns": ["manette switch pro", "switch pro controller"],
            "label": "Manette Switch Pro",
            "format": "petit colis",
            "resale_min": 38,
            "resale_max": 55,
            "buy_max": 32,
            "base_score": 4,
        },
        "switch_game": {
            "patterns": ["jeu switch", "jeux switch", "jeu nintendo switch", "cartouche switch", "version physique switch"],
            "label": "Jeu Switch physique",
            "format": "jeu physique / petit colis",
            "resale_min": 28,
            "resale_max": 42,
            "buy_max": 22,
            "base_score": 4,
        },
        "ps5_game": {
            "patterns": ["jeu ps5", "jeux ps5", "jeu playstation 5", "ps5 boîte", "ps5 boite", "version physique ps5", "disque ps5"],
            "label": "Jeu PS5 physique",
            "format": "jeu physique / petit colis",
            "resale_min": 20,
            "resale_max": 32,
            "buy_max": 15,
            "base_score": 4,
        },
        "gaming_headset": {
            "patterns": ["casque gaming", "pulse 3d", "inzone", "razer", "steelseries", "hyperx", "turtle beach"],
            "label": "Casque gaming",
            "format": "colis moyen",
            "resale_min": 30,
            "resale_max": 55,
            "buy_max": 25,
            "base_score": 3,
        },
        "switch_microsd": {
            "patterns": ["microsd switch", "micro sd switch", "carte microsd", "carte micro sd"],
            "label": "Carte microSD / accessoire Switch",
            "format": "petit colis",
            "resale_min": 12,
            "resale_max": 25,
            "buy_max": 10,
            "base_score": 2,
        },
    },
    "score": {
        "urgent_bonus": 3,
        "trusted_merchant_bonus": 1,
        "hot_temperature_bonus": 1,
        "very_hot_temperature_bonus": 2,
        "good_margin_bonus": 2,
        "excellent_margin_bonus": 3,
        "below_buy_max_bonus": 2,
        "exclusion_penalty": 5,
        "unknown_price_penalty": 2,
        "bulky_penalty": 2,
    },
}


def split_env_list(value: str, default: List[str]) -> List[str]:
    if not value.strip():
        return default
    return [x.strip() for x in value.split(",") if x.strip()]


def load_json_file(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Impossible de lire %s: %s", path, exc)
    return default


def ensure_config_files() -> None:
    if not KEYWORDS_FILE.exists():
        KEYWORDS_FILE.write_text(json.dumps(DEFAULT_KEYWORDS, ensure_ascii=False, indent=2), encoding="utf-8")
    if not RULES_FILE.exists():
        RULES_FILE.write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
                state.setdefault("subscribers", [])
                state.setdefault("seen_deals", [])
                state.setdefault("last_scan", None)
                state.setdefault("last_error", None)
                return state
        except Exception as exc:
            log.warning("Impossible de lire l'état: %s", exc)
    return {"subscribers": [], "seen_deals": [], "last_scan": None, "last_error": None}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def any_keyword(text: str, keywords: Iterable[str]) -> Optional[str]:
    norm = normalize_text(text)
    for keyword in keywords:
        k = normalize_text(keyword)
        if k and k in norm:
            return keyword
    return None


def looks_like_free_product(text: str) -> bool:
    """Retourne True seulement si gratuit concerne probablement le produit.

    Ancienne erreur : le bot voyait "livraison gratuite" ou "retour gratuit"
    et transformait le deal en prix 0 €.
    """
    lower = normalize_text(text)
    bad_contexts = [
        "livraison gratuite", "frais de port gratuit", "frais de port gratuits",
        "fdp gratuit", "fdp gratuits", "retour gratuit", "retours gratuits",
        "expédition gratuite", "expedition gratuite", "retrait gratuit",
    ]
    cleaned = lower
    for bad in bad_contexts:
        cleaned = cleaned.replace(bad, " ")

    # Gratuit doit apparaître comme information produit, pas seulement logistique.
    return bool(re.search(r"\b(gratuit|offert|free)\b", cleaned))


def extract_price(text: str) -> Tuple[Optional[float], str]:
    if not text:
        return None, "Prix non détecté"

    lower = normalize_text(text)

    # Ignore les montants qui ne sont pas des prix produits.
    ignored_context_words = [
        "fidélité", "fidelite", "cashback", "bon d'achat", "bon achat",
        "odr", "rembourse", "remboursé", "remboursee", "coupon",
        "livraison", "frais de port", "fdp", "retrait", "retour",
        "économie", "economie", "remise", "réduction", "reduction",
    ]

    matches = list(re.finditer(r"(\d{1,4}(?:[\s\.]\d{3})*(?:[,.]\d{1,2})?)\s*€", text))
    candidates: List[float] = []
    for m in matches:
        start, end = m.span()
        context = normalize_text(text[max(0, start - 45): min(len(text), end + 45)])
        if any(word in context for word in ignored_context_words):
            continue
        raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
        try:
            candidates.append(float(raw))
        except ValueError:
            pass

    if candidates:
        # Sur Dealabs, le vrai prix est généralement le premier montant € utile.
        value = candidates[0]
        return value, f"{value:.2f} €".replace(".", ",")

    # Produit réellement gratuit uniquement si le mot gratuit ne concerne pas la livraison.
    if looks_like_free_product(text) or re.search(r"\b0\s*€\b", lower):
        return 0.0, "0 € / gratuit"

    return None, "Prix non détecté"


def is_expired_deal_text(text: str) -> bool:
    norm = normalize_text(text)
    expired_terms = [
        "expiré", "expire", "expirée", "expiree", "deal expiré", "deal expire",
        "terminé", "termine", "ce deal a expiré", "ce deal a expire",
        "plus disponible", "indisponible", "rupture de stock", "stock épuisé", "stock epuise",
    ]
    return any(term in norm for term in expired_terms)


def extract_temperature(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(-?\d+(?:[,.]\d+)?)\s*°", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def detect_merchant(text: str, keywords: Dict[str, Any]) -> str:
    found = any_keyword(text, keywords.get("trusted_merchants", []))
    return str(found).title() if found else "Marchand à vérifier"


def clean_dealabs_url(url: str) -> str:
    if not url:
        return "https://www.dealabs.com/"
    url = url.split("?")[0]
    if url.startswith("/"):
        url = urljoin("https://www.dealabs.com", url)
    return url


def deal_id_from_url(url: str, title: str) -> str:
    match = re.search(r"-(\d+)(?:$|/)", url)
    if match:
        return f"dealabs:{match.group(1)}"
    return "dealabs:" + re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:80]


def requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    return s


def fetch_dealabs_feeds() -> List[Deal]:
    feed_urls = split_env_list(os.environ.get("DEALABS_FEEDS", ""), DEFAULT_DEALABS_FEEDS)
    deals: List[Deal] = []
    for feed_url in feed_urls:
        try:
            parsed = feedparser.parse(feed_url, request_headers={"User-Agent": USER_AGENT})
            if parsed.bozo and not parsed.entries:
                log.warning("Flux RSS potentiellement indisponible: %s", feed_url)
            for entry in parsed.entries[:30]:
                title = html.unescape(getattr(entry, "title", "")).strip()
                url = clean_dealabs_url(getattr(entry, "link", ""))
                summary = html.unescape(getattr(entry, "summary", "") or getattr(entry, "description", ""))
                raw_text = f"{title} {summary}"
                if is_expired_deal_text(raw_text):
                    continue
                price, price_text = extract_price(raw_text)
                temperature = extract_temperature(raw_text)
                merchant = detect_merchant(f"{title} {summary}", load_json_file(KEYWORDS_FILE, DEFAULT_KEYWORDS))
                image = None
                if getattr(entry, "media_content", None):
                    image = entry.media_content[0].get("url")
                deals.append(Deal(
                    id=deal_id_from_url(url, title),
                    title=title,
                    url=url,
                    price=price,
                    price_text=price_text,
                    merchant=merchant,
                    temperature=temperature,
                    image=image,
                    description=BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:500],
                ))
        except Exception as exc:
            log.warning("Erreur RSS Dealabs %s: %s", feed_url, exc)
    return deals


def parse_deal_card(card: Any, base_url: str = "https://www.dealabs.com") -> Optional[Deal]:
    text = card.get_text(" ", strip=True)
    if len(text) < 10:
        return None
    if is_expired_deal_text(text):
        return None

    link_tag = None
    for a in card.find_all("a", href=True):
        href = a.get("href", "")
        if "/bons-plans/" in href or "/deals/" in href or re.search(r"-\d+$", href):
            link_tag = a
            break
    if not link_tag:
        # Fallback: premier lien un peu long vers Dealabs
        for a in card.find_all("a", href=True):
            href = a.get("href", "")
            if href.startswith("/") or "dealabs.com" in href:
                link_tag = a
                break
    if not link_tag:
        return None

    url = clean_dealabs_url(urljoin(base_url, link_tag["href"]))
    title = link_tag.get_text(" ", strip=True)
    if len(title) < 5:
        # Essaie h2/h3
        h = card.find(["h1", "h2", "h3"])
        title = h.get_text(" ", strip=True) if h else text[:120]
    price, price_text = extract_price(text)
    temperature = extract_temperature(text)
    keywords = load_json_file(KEYWORDS_FILE, DEFAULT_KEYWORDS)
    merchant = detect_merchant(text, keywords)
    img = card.find("img")
    image = img.get("src") or img.get("data-src") if img else None
    return Deal(
        id=deal_id_from_url(url, title),
        title=title,
        url=url,
        price=price,
        price_text=price_text,
        merchant=merchant,
        temperature=temperature,
        image=image,
        description=text[:500],
    )


def fetch_dealabs_search_pages() -> List[Deal]:
    """Fallback HTML.

    Dealabs peut changer son HTML ou bloquer certains hébergements.
    Cette méthode reste volontairement simple et non agressive.
    """
    queries = split_env_list(os.environ.get("DEALABS_SEARCH_QUERIES", ""), DEFAULT_SEARCH_QUERIES)
    session = requests_session()
    deals: List[Deal] = []
    urls = ["https://www.dealabs.com/groupe/jeux-video", "https://www.dealabs.com/hot"]
    urls += [f"https://www.dealabs.com/search?q={quote_plus(q)}" for q in queries]

    for url in urls[:20]:
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code >= 400:
                log.warning("Dealabs HTML %s -> HTTP %s", url, resp.status_code)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("article, .thread, [data-t='thread'], [class*='thread']")
            if not cards:
                cards = soup.find_all("li")[:80]
            for card in cards[:40]:
                deal = parse_deal_card(card)
                if deal:
                    deals.append(deal)
        except Exception as exc:
            log.warning("Erreur HTML Dealabs %s: %s", url, exc)
    return deals


def dedupe_deals(deals: List[Deal]) -> List[Deal]:
    seen = set()
    unique: List[Deal] = []
    for deal in deals:
        key = deal.id or deal.url
        if key in seen:
            continue
        seen.add(key)
        unique.append(deal)
    return unique


def category_for_deal(deal: Deal, rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = normalize_text(f"{deal.title} {deal.description}")
    best = None
    for _, rule in rules.get("categories", {}).items():
        if any_keyword(text, rule.get("patterns", [])):
            best = rule
            break
    return best


def analyze_deal(deal: Deal, keywords: Dict[str, Any], rules: Dict[str, Any]) -> Optional[Analysis]:
    full_text = f"{deal.title} {deal.description} {deal.merchant}"
    norm = normalize_text(full_text)

    if is_expired_deal_text(full_text):
        return None

    exclusion = any_keyword(norm, keywords.get("exclusions", []))
    urgent = any_keyword(norm, keywords.get("urgent", []))
    category_rule = category_for_deal(deal, rules)

    # Si aucun produit cible et pas d'urgence, on ignore.
    if not category_rule and not urgent:
        return None

    # Pour de la revente, un deal sans prix fiable est inutile et génère trop de faux positifs.
    if deal.price is None:
        return None

    # Sécurité anti-faux 0€ : si le prix est 0 mais que le titre ne dit pas clairement
    # que le produit est gratuit/offert, on ignore. Sinon c'est souvent "livraison gratuite".
    if deal.price == 0 and not looks_like_free_product(deal.title):
        return None

    score_cfg = rules.get("score", {})
    score = 0
    reasons: List[str] = []

    if category_rule:
        score += int(category_rule.get("base_score", 3))
        reasons.append(f"Produit ciblé : {category_rule.get('label')}")
    else:
        category_rule = {
            "label": "Produit gaming à vérifier",
            "format": "à vérifier",
            "resale_min": None,
            "resale_max": None,
            "buy_max": None,
            "base_score": 0,
        }
        reasons.append("Mot-clé urgent détecté, catégorie à vérifier")

    if urgent:
        score += int(score_cfg.get("urgent_bonus", 3))
        reasons.append(f"Signal urgent : {urgent}")

    trusted = any_keyword(norm, keywords.get("trusted_merchants", []))
    if trusted or deal.merchant != "Marchand à vérifier":
        score += int(score_cfg.get("trusted_merchant_bonus", 1))
        reasons.append("Marchand connu ou à vérifier sur Dealabs")

    if deal.temperature is not None:
        if deal.temperature >= 300:
            score += int(score_cfg.get("very_hot_temperature_bonus", 2))
            reasons.append(f"Deal très chaud : {deal.temperature:.0f}°")
        elif deal.temperature >= 100:
            score += int(score_cfg.get("hot_temperature_bonus", 1))
            reasons.append(f"Deal chaud : {deal.temperature:.0f}°")

    if exclusion:
        score -= int(score_cfg.get("exclusion_penalty", 5))
        reasons.append(f"Malus exclusion : {exclusion}")

    if deal.price is None:
        score -= int(score_cfg.get("unknown_price_penalty", 2))
        reasons.append("Prix non détecté")

    resale_min = category_rule.get("resale_min")
    resale_max = category_rule.get("resale_max")
    buy_max = category_rule.get("buy_max")

    margin_min = margin_max = None
    if deal.price is not None and resale_min is not None and resale_max is not None:
        margin_min = round(float(resale_min) - deal.price, 2)
        margin_max = round(float(resale_max) - deal.price, 2)
        if margin_min >= 10:
            score += int(score_cfg.get("excellent_margin_bonus", 3))
            reasons.append("Marge estimée excellente")
        elif margin_min >= 6:
            score += int(score_cfg.get("good_margin_bonus", 2))
            reasons.append("Marge estimée correcte")
        if buy_max is not None and deal.price <= float(buy_max):
            score += int(score_cfg.get("below_buy_max_bonus", 2))
            reasons.append("Prix sous le seuil d'achat cible")

    score = max(0, min(10, score))

    if urgent and score >= 7:
        alert_type = "🚨 URGENT : ERREUR DE PRIX / BUG DE PRIX"
        action_label = "À vérifier immédiatement"
    elif score >= 8:
        alert_type = "🔥 BON PLAN REVENDABLE"
        action_label = "Intéressant si stock et livraison OK"
    else:
        alert_type = "🟡 DEAL À SURVEILLER"
        action_label = "À vérifier manuellement"

    return Analysis(
        category=str(category_rule.get("label", "Produit gaming")),
        resale_min=float(resale_min) if resale_min is not None else None,
        resale_max=float(resale_max) if resale_max is not None else None,
        margin_min=margin_min,
        margin_max=margin_max,
        score=score,
        reasons=reasons[:6],
        alert_type=alert_type,
        format_label=str(category_rule.get("format", "à vérifier")),
        action_label=action_label,
    )


def format_euro(value: Optional[float]) -> str:
    if value is None:
        return "à vérifier"
    return f"{value:.2f} €".replace(".", ",")


def format_range(min_v: Optional[float], max_v: Optional[float]) -> str:
    if min_v is None or max_v is None:
        return "à vérifier"
    return f"{format_euro(min_v)} – {format_euro(max_v)}"


def build_message(deal: Deal, analysis: Analysis) -> str:
    temp = f"\n🌡️ Température Dealabs : <b>{deal.temperature:.0f}°</b>" if deal.temperature is not None else ""
    reasons = "\n".join(f"• {html.escape(r)}" for r in analysis.reasons)
    return (
        f"{analysis.alert_type}\n\n"
        f"🎮 Produit : <b>{html.escape(deal.title[:120])}</b>\n"
        f"🏪 Source : <b>{html.escape(deal.merchant)}</b> via Dealabs\n"
        f"💸 Prix détecté : <b>{html.escape(deal.price_text)}</b>\n"
        f"📦 Format : <b>{html.escape(analysis.format_label)}</b>\n"
        f"📈 Revente estimée Vinted : <b>{html.escape(format_range(analysis.resale_min, analysis.resale_max))}</b>\n"
        f"🧮 Marge brute estimée : <b>{html.escape(format_range(analysis.margin_min, analysis.margin_max))}</b>"
        f"{temp}\n\n"
        f"⚠️ Score : <b>{analysis.score}/10</b>\n"
        f"✅ Action : <b>{html.escape(analysis.action_label)}</b>\n\n"
        f"🔎 Raisons :\n{reasons}\n\n"
        f"⚠️ Vérifie toujours : état réel, frais de port, vendeur, stock, et prix Vinted avant achat."
    )


def scan_once(include_low_scores: bool = False) -> List[Tuple[Deal, Analysis]]:
    ensure_config_files()
    keywords = load_json_file(KEYWORDS_FILE, DEFAULT_KEYWORDS)
    rules = load_json_file(RULES_FILE, DEFAULT_RULES)

    deals = []
    deals.extend(fetch_dealabs_feeds())
    deals.extend(fetch_dealabs_search_pages())
    deals = dedupe_deals(deals)

    results: List[Tuple[Deal, Analysis]] = []
    for deal in deals:
        analysis = analyze_deal(deal, keywords, rules)
        if not analysis:
            continue
        if include_low_scores or analysis.score >= MIN_SCORE_ALERT:
            results.append((deal, analysis))

    results.sort(key=lambda x: (x[1].score, x[0].temperature or 0), reverse=True)
    return results


class HealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"{APP_NAME} en ligne".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckServer)
    log.info("Serveur web actif sur port %s", port)
    server.serve_forever()


async def send_deal(application: Application, chat_id: int, deal: Deal, analysis: Analysis) -> None:
    keyboard = [[InlineKeyboardButton("🛒 Voir le deal", url=deal.url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message = build_message(deal, analysis)
    await application.bot.send_message(
        chat_id=chat_id,
        text=message,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


async def run_scan_and_alert(application: Application, manual_chat_id: Optional[int] = None) -> Tuple[int, int]:
    state = load_state()
    subscribers = state.get("subscribers", [])
    if manual_chat_id and manual_chat_id not in subscribers:
        subscribers.append(manual_chat_id)
        state["subscribers"] = subscribers

    seen = set(state.get("seen_deals", []))
    try:
        results = scan_once(include_low_scores=(manual_chat_id is not None and MANUAL_INCLUDE_LOW_SCORES))
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None
    except Exception as exc:
        log.exception("Erreur scan")
        state["last_error"] = str(exc)
        save_state(state)
        if manual_chat_id:
            await application.bot.send_message(chat_id=manual_chat_id, text=f"❌ Erreur pendant le scan : {exc}")
        return 0, 0

    new_results: List[Tuple[Deal, Analysis]] = []
    for deal, analysis in results:
        if manual_chat_id is None and deal.id in seen:
            continue
        if analysis.score < MIN_SCORE_ALERT and not MANUAL_INCLUDE_LOW_SCORES:
            continue
        new_results.append((deal, analysis))

    sent = 0
    for deal, analysis in new_results[:MAX_ALERTS_PER_SCAN]:
        target_chats = [manual_chat_id] if manual_chat_id else subscribers
        for chat_id in target_chats:
            try:
                await send_deal(application, int(chat_id), deal, analysis)
                sent += 1
            except Exception as exc:
                log.warning("Erreur envoi Telegram chat %s: %s", chat_id, exc)
        seen.add(deal.id)

    # On garde seulement les 2000 derniers ids pour éviter un fichier énorme.
    state["seen_deals"] = list(seen)[-2000:]
    save_state(state)
    return len(results), sent


async def scanner_loop(application: Application) -> None:
    while True:
        state = load_state()
        if state.get("subscribers"):
            total, sent = await run_scan_and_alert(application)
            log.info("Scan automatique terminé: %s deals filtrés, %s alertes envoyées", total, sent)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def start_revente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = load_state()
    subscribers = set(state.get("subscribers", []))
    subscribers.add(chat_id)
    state["subscribers"] = list(subscribers)
    save_state(state)
    await update.message.reply_text(
        "✅ Bot Revente Gaming activé.\n\n"
        "Je surveille Dealabs pour : jeux PS5 physiques, jeux Switch physiques, DualSense, Joy-Con, manettes et accessoires gaming petits formats.\n\n"
        "Commandes :\n"
        "/check — scan manuel\n"
        "/status — état du bot\n"
        "/stop_revente — arrêter les alertes"
    )


async def stop_revente(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = load_state()
    subscribers = [x for x in state.get("subscribers", []) if int(x) != int(chat_id)]
    state["subscribers"] = subscribers
    save_state(state)
    await update.message.reply_text("🛑 Alertes revente désactivées pour ce chat.")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔎 Scan manuel lancé...")
    total, sent = await run_scan_and_alert(context.application, manual_chat_id=update.effective_chat.id)
    if sent == 0:
        await update.message.reply_text(
            f"Aucune opportunité vraiment intéressante trouvée pour l'instant.\n"
            f"Deals analysés/filtrés : {total}."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    await update.message.reply_text(
        f"🤖 {APP_NAME}\n"
        f"État : en ligne\n"
        f"Abonnés : {len(state.get('subscribers', []))}\n"
        f"Intervalle scan : {CHECK_INTERVAL_SECONDS} sec\n"
        f"Score minimum alerte : {MIN_SCORE_ALERT}/10\n"
        f"Scan manuel élargi : {MANUAL_INCLUDE_LOW_SCORES}\n"
        f"Dernier scan : {state.get('last_scan') or 'aucun'}\n"
        f"Dernière erreur : {state.get('last_error') or 'aucune'}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commandes disponibles :\n"
        "/start_revente — activer les alertes\n"
        "/check — scanner maintenant\n"
        "/status — voir l'état\n"
        "/stop_revente — arrêter les alertes\n\n"
        "V1 = Dealabs uniquement. Keepa/Amazon et Vinted pourront être ajoutés ensuite."
    )


async def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN manquant. Ajoute-le dans les variables d'environnement Render.")

    ensure_config_files()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_revente))
    application.add_handler(CommandHandler("start_revente", start_revente))
    application.add_handler(CommandHandler("stop_revente", stop_revente))
    application.add_handler(CommandHandler("check", check))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("help", help_cmd))

    asyncio.create_task(scanner_loop(application))
    log.info("%s démarré", APP_NAME)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    asyncio.run(main())
