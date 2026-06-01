"""
Bot Telegram Bons Plans Revente Gaming - V1.8.1 Récap quotidien
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
SEND_UNCERTAIN_DEALS=true
MIN_SCORE_UNCERTAIN=7
DEALABS_FEEDS=https://www.dealabs.com/rss/hot,https://www.dealabs.com/rss/new
DEALABS_SEARCH_QUERIES=jeu PS5,jeux PS5,jeu Switch,DualSense,Joy-Con,manette PS5,casque gaming
SEND_BEST_CANDIDATE_ON_MANUAL_CHECK=true
CATEGORY_GATE=true
# V1.7
PROMO_VALUE_GUARD=true
# V1.8
DAILY_SUMMARY_ENABLED=true
DAILY_SUMMARY_HOUR=20
DAILY_SUMMARY_MINUTE=30
DAILY_SUMMARY_TIMEZONE=Europe/Paris
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
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

APP_NAME = "Bot Revente Gaming V1.8.1"
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
# V1.2: prix plus stricts pour éviter les faux positifs Dealabs
STRICT_PRICE_MODE = os.environ.get("STRICT_PRICE_MODE", "true").lower() == "true"
USE_HTML_FALLBACK = os.environ.get("USE_HTML_FALLBACK", "true").lower() == "true"
# V1.3: on envoie aussi certains deals ambigus en alerte jaune, sans inventer de marge.
SEND_UNCERTAIN_DEALS = os.environ.get("SEND_UNCERTAIN_DEALS", "true").lower() == "true"
MIN_SCORE_UNCERTAIN = int(os.environ.get("MIN_SCORE_UNCERTAIN", "7"))
SEND_BEST_CANDIDATE_ON_MANUAL_CHECK = os.environ.get("SEND_BEST_CANDIDATE_ON_MANUAL_CHECK", "true").lower() == "true"

# V1.5: filtre catégorie obligatoire. Même une "erreur de prix" est ignorée
# si le produit n’est pas dans les cibles gaming/revente définies.
CATEGORY_GATE = os.environ.get("CATEGORY_GATE", "true").lower() == "true"

# V1.7 : évite de confondre chèque cadeau / cashback / nouveaux clients
# avec le vrai prix payé. Ces deals restent visibles en jaune, sans marge inventée.
PROMO_VALUE_GUARD = os.environ.get("PROMO_VALUE_GUARD", "true").lower() == "true"
CONDITIONAL_PRICE_PENALTY = int(os.environ.get("CONDITIONAL_PRICE_PENALTY", "2"))

# V1.8 : résumé quotidien pour savoir que le bot travaille même sans alerte.
DAILY_SUMMARY_ENABLED = os.environ.get("DAILY_SUMMARY_ENABLED", "true").lower() == "true"
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "20"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "30"))
DAILY_SUMMARY_TIMEZONE = os.environ.get("DAILY_SUMMARY_TIMEZONE", "Europe/Paris")
DAILY_SUMMARY_TOP_N = int(os.environ.get("DAILY_SUMMARY_TOP_N", "3"))

DEFAULT_DEALABS_FEEDS = [
    "https://www.dealabs.com/rss/hot",
    "https://www.dealabs.com/rss/new",
]
DEFAULT_SEARCH_QUERIES = [
    # Jeux physiques PS5 / Switch
    "jeu PS5",
    "jeux PS5",
    "PS5 boite",
    "version physique PS5",
    "jeu PlayStation 5",
    "Final Fantasy XVI PS5",
    "Spider-Man 2 PS5",
    "God of War Ragnarok PS5",
    "Resident Evil PS5",
    "jeu Nintendo Switch",
    "jeux Switch",
    "cartouche Switch",
    "version physique Switch",
    "Mario Kart Switch",
    "Zelda Switch",
    "Pokemon Switch",
    # Manettes / accessoires
    "DualSense",
    "manette PS5",
    "manette sans fil DualSense",
    "Joy-Con",
    "Joy-Con 2",
    "manette Switch Pro",
    "Pro Controller Switch",
    "casque gaming",
    "Pulse 3D",
    "microSD Switch",
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
    price_reliable: bool = True
    uncertainty_reasons: List[str] = None


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
    price_reliable: bool = True


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
            "patterns": ["dualsense", "dual sense", "manette ps5", "manette playstation 5", "manette sans fil dualsense", "manette sans fil ps5", "selection de manette ps5", "sélection de manette ps5"],
            "label": "Manette PS5 / DualSense",
            "format": "petit colis",
            "resale_min": 45,
            "resale_max": 58,
            "buy_max": 40,
            "base_score": 4,
        },
        "joycon": {
            "patterns": ["joy-con", "joy con", "joy-con 2", "joy con 2", "volant joy-con", "volants joy-con"],
            "label": "Joy-Con Switch",
            "format": "petit colis",
            "resale_min": 45,
            "resale_max": 65,
            "buy_max": 35,
            "base_score": 4,
        },
        "switch_pro_controller": {
            "patterns": ["manette switch pro", "switch pro controller", "pro controller", "manette pro controller"],
            "label": "Manette Switch Pro",
            "format": "petit colis",
            "resale_min": 38,
            "resale_max": 55,
            "buy_max": 32,
            "base_score": 4,
        },
        "switch_game": {
            "patterns": ["jeu switch", "jeux switch", "jeu nintendo switch", "cartouche switch", "version physique switch", "mario kart", "zelda switch", "pokemon switch", "pokémon switch", "super mario", "donkey kong", "kirby", "animal crossing", "metroid", "splatoon"],
            "label": "Jeu Switch physique",
            "format": "jeu physique / petit colis",
            "resale_min": 28,
            "resale_max": 42,
            "buy_max": 22,
            "base_score": 4,
        },
        "ps5_game": {
            "patterns": ["jeu ps5", "jeux ps5", "jeu playstation 5", "ps5 boîte", "ps5 boite", "version physique ps5", "disque ps5", "final fantasy", "spider-man", "spiderman", "resident evil", "astro bot", "stellar blade", "god of war", "gran turismo", "silent hill", "call of duty ps5"],
            "label": "Jeu PS5 physique",
            "format": "jeu physique / petit colis",
            "resale_min": 20,
            "resale_max": 32,
            "buy_max": 15,
            "base_score": 4,
        },
        "gaming_headset": {
            "patterns": ["casque gaming", "casque gamer", "pulse 3d", "inzone", "razer", "logitech g", "steelseries", "hyperx", "turtle beach"],
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
                state.setdefault("last_daily_summary_date", None)
                state.setdefault("last_daily_summary", None)
                return state
        except Exception as exc:
            log.warning("Impossible de lire l'état: %s", exc)
    return {"subscribers": [], "seen_deals": [], "last_scan": None, "last_error": None, "last_daily_summary_date": None, "last_daily_summary": None}


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
        "chèque cadeau", "cheque cadeau", "chèque-cadeau", "cheque-cadeau",
        "offert en chèque", "offerts en chèque", "offert en cheque", "offerts en cheque",
        "odr", "rembourse", "remboursé", "remboursee", "coupon",
        "livraison", "frais de port", "fdp", "retrait", "retour",
        "économie", "economie", "remise", "réduction", "reduction",
        "nouveau client", "nouveaux clients", "première commande", "premiere commande",
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




def is_multi_product_or_voucher(title: str) -> bool:
    """Évite les deals Dealabs trop ambigus : sélections, fidélité, coupons.

    Ces posts mélangent souvent plusieurs prix dans la même carte/page, ce qui
    provoquait des alertes à 14,99 € ou 0 € alors que le vrai produit était à
    54,99 € ou juste lié à un avantage fidélité.
    """
    norm = normalize_text(title)
    suspicious = [
        "selection", "sélection", "ex.:", "ex :", "exemple",
        "a partir de", "à partir de", "jusqu'a", "jusqu’à",
        "tous les", "sur tous", "club carrefour", "fidelite", "fidélité",
        "cashback", "bon d'achat", "coupon", "odr", "prime] console",
        "console nintendo switch", "console switch", "console ps5",
    ]
    return any(x in norm for x in suspicious)



def promo_value_reasons(title: str, body: str = "") -> List[str]:
    """V1.7 : repère les montants qui sont des avantages, pas le prix payé.

    Exemples : "+10€ en chèque cadeau", "10€ offerts", "5€ fidélité",
    "39,99€ pour les nouveaux clients". Ces deals peuvent être utiles,
    mais ne doivent jamais être classés comme prix fiable avec marge calculée.
    """
    text = normalize_text(f"{title} {body}")
    checks = [
        ("Avantage chèque cadeau / bon d'achat : montant à ne pas lire comme prix payé", [
            "chèque cadeau", "cheque cadeau", "chèque-cadeau", "cheque-cadeau",
            "bon d'achat", "bon achat", "offert en chèque", "offerts en chèque",
            "offert en cheque", "offerts en cheque", "en chèque c", "en cheque c",
        ]),
        ("Avantage fidélité / cagnotte : prix net réel à vérifier", [
            "fidélité", "fidelite", "club carrefour", "cagnotte", "cagnotté", "cagnotte",
        ]),
        ("Cashback / ODR / coupon : avantage non garanti immédiatement", [
            "cashback", "odr", "offre de remboursement", "coupon", "code promo", "code réduction", "code reduction",
        ]),
        ("Prix réservé aux nouveaux clients : condition d'éligibilité à vérifier", [
            "nouveau client", "nouveaux clients", "nouvelle cliente", "nouveaux comptes",
            "première commande", "premiere commande", "1ere commande", "1ère commande",
        ]),
    ]
    reasons: List[str] = []
    for label, terms in checks:
        if any(t in text for t in terms):
            reasons.append(label)
    return reasons


def price_uncertainty_reasons(title: str, body: str = "") -> List[str]:
    """Repère les deals où le montant Dealabs peut être un exemple, une sélection,
    un coupon, de la fidélité ou un prix annexe. Ces deals peuvent rester utiles,
    mais doivent être affichés comme "à vérifier", sans marge inventée.
    """
    text = normalize_text(f"{title} {body}")
    checks = [
        ("Deal de type sélection / plusieurs produits", ["selection", "sélection", "tous les", "sur tous", "lot de", "plusieurs"]),
        ("Prix possiblement donné comme exemple", ["ex.:", "ex :", "exemple", "par exemple"]),
        ("Prix possiblement à partir de", ["a partir de", "à partir de", "dès", "des "]),
        ("Avantage fidélité/coupon/cashback possible", ["fidelite", "fidélité", "club carrefour", "coupon", "cashback", "odr", "bon d'achat", "bon achat", "chèque cadeau", "cheque cadeau", "offert en chèque", "offerts en chèque"]),
        ("Prix conditionnel / nouveaux clients à vérifier", ["nouveau client", "nouveaux clients", "première commande", "premiere commande", "1ere commande", "1ère commande"]),
        ("Console ou pack trop large à vérifier", ["console nintendo switch", "console switch", "console ps5", "pack console", "bundle console"]),
    ]
    reasons: List[str] = []
    for label, terms in checks:
        if any(t in text for t in terms):
            reasons.append(label)
    return reasons

def extract_price_strict(title: str, body: str = "") -> Tuple[Optional[float], str]:
    """V1.2 : on privilégie le prix présent dans le titre ou un bloc prix isolé.

    Ne jamais prendre aveuglément le premier montant trouvé dans toute la carte
    Dealabs, car elle peut contenir livraison gratuite, commentaires, cashback
    ou d'autres produits d'une sélection.
    """
    title_price, title_text = extract_price(title)
    if title_price is not None:
        return title_price, title_text

    # En mode strict, un deal sans prix dans le titre est trop risqué, sauf vrai gratuit explicite.
    if STRICT_PRICE_MODE:
        if looks_like_free_product(title):
            return 0.0, "0 € / gratuit"
        return None, "Prix non détecté"

    return extract_price(body)

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
                # V1.3: on ne supprime plus les sélections : on les déclare incertaines.
                uncertainty = price_uncertainty_reasons(title, raw_text)
                if PROMO_VALUE_GUARD:
                    uncertainty.extend([r for r in promo_value_reasons(title, raw_text) if r not in uncertainty])
                price_reliable = not uncertainty
                price, price_text = extract_price_strict(title, raw_text)
                if not price_reliable and price is not None:
                    price_text = f"incertain — montant trouvé : {price_text}"
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
                    price_reliable=price_reliable,
                    uncertainty_reasons=uncertainty,
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
    uncertainty = price_uncertainty_reasons(title, text)
    if PROMO_VALUE_GUARD:
        uncertainty.extend([r for r in promo_value_reasons(title, text) if r not in uncertainty])
    price_reliable = not uncertainty

    # Essaye d'abord des blocs prix précis, sinon uniquement le titre.
    price_node = None
    for selector in [
        "[data-t='thread-price']", "[class*='thread-price']",
        ".thread-price", ".cept-thread-price", "[class*='price']"
    ]:
        price_node = card.select_one(selector)
        if price_node and "€" in price_node.get_text(" ", strip=True):
            break
        price_node = None
    price_source = price_node.get_text(" ", strip=True) if price_node else title
    price, price_text = extract_price_strict(price_source, text)
    if not price_reliable and price is not None:
        price_text = f"incertain — montant trouvé : {price_text}"
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
        price_reliable=price_reliable,
        uncertainty_reasons=uncertainty,
    )


def fetch_dealabs_search_pages() -> List[Deal]:
    """Fallback HTML.

    Dealabs peut changer son HTML ou bloquer certains hébergements.
    Cette méthode reste volontairement simple et non agressive.
    """
    if not USE_HTML_FALLBACK:
        return []

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
            cards = soup.select("article[data-t='thread'], article.thread, [data-t='thread']")
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

    # V1.5 : filtre catégorie obligatoire.
    # On ne veut plus d'alertes TV, lingettes, coussins, électroménager, etc.
    # Même si Dealabs parle d'erreur de prix, le deal doit matcher une catégorie cible.
    if CATEGORY_GATE and not category_rule:
        return None

    # Mode legacy possible : si CATEGORY_GATE=false, on peut encore remonter les urgences hors catégorie.
    if not category_rule and not urgent:
        return None

    uncertain_reasons = deal.uncertainty_reasons or []
    promo_reasons = promo_value_reasons(deal.title, deal.description) if PROMO_VALUE_GUARD else []
    if promo_reasons:
        for reason in promo_reasons:
            if reason not in uncertain_reasons:
                uncertain_reasons.append(reason)
        deal.price_reliable = False

    # Prix absent : inutile sauf signal urgent ou deal ambigu très ciblé qu'on envoie en jaune.
    if deal.price is None and not (urgent or (SEND_UNCERTAIN_DEALS and category_rule and uncertain_reasons)):
        return None

    # Sécurité anti-faux 0€ : si le prix est 0 mais que le titre ne dit pas clairement
    # que le produit est gratuit/offert, on garde seulement en alerte jaune incertaine.
    if deal.price == 0 and not looks_like_free_product(deal.title):
        uncertain_reasons.append("Prix 0€ probablement lié à livraison/coupon ou montant parasite")
        deal.price_reliable = False

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

    if uncertain_reasons or not deal.price_reliable:
        # V1.7 : les chèques cadeaux/cashback/nouveaux clients peuvent être utiles,
        # mais ils ne doivent pas produire un score "fiable" ni une marge inventée.
        penalty = CONDITIONAL_PRICE_PENALTY if any(
            any(key in normalize_text(r) for key in ["chèque", "cheque", "fidélité", "fidelite", "cashback", "coupon", "nouveau", "odr"])
            for r in uncertain_reasons
        ) else 1
        score -= penalty
        reasons.extend(uncertain_reasons[:3])

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
    if deal.price is not None and deal.price_reliable and resale_min is not None and resale_max is not None:
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

    if uncertain_reasons or not deal.price_reliable:
        alert_type = "🟡 DEAL À VÉRIFIER MANUELLEMENT"
        action_label = "Ouvre le deal : prix réel possiblement différent"
    elif urgent and score >= 7:
        alert_type = "🚨 URGENT : ERREUR DE PRIX / BUG DE PRIX"
        action_label = "À vérifier immédiatement"
    elif score >= 8:
        alert_type = "🔥 BON PLAN REVENDABLE FIABLE"
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
        price_reliable=bool(deal.price_reliable and not uncertain_reasons),
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
    if analysis.price_reliable:
        price_line = f"💸 Prix payé détecté : <b>{html.escape(deal.price_text)}</b>\n"
        margin_line = f"🧮 Marge brute estimée : <b>{html.escape(format_range(analysis.margin_min, analysis.margin_max))}</b>"
        if analysis.margin_min is not None and analysis.margin_max is not None:
            prudent_min = max(0, analysis.margin_min - 4)
            prudent_max = max(0, analysis.margin_max - 6)
            margin_line += f"\n🧯 Marge prudente estimée : <b>{html.escape(format_range(prudent_min, prudent_max))}</b>"
    else:
        price_line = f"💸 Prix payé réel : <b>à vérifier</b>\n💬 Montant repéré : <b>{html.escape(deal.price_text)}</b>\n"
        margin_line = "🧮 Marge brute estimée : <b>non calculée tant que le prix exact n'est pas confirmé</b>"
    return (
        f"{analysis.alert_type}\n\n"
        f"🎮 Produit : <b>{html.escape(deal.title[:120])}</b>\n"
        f"🏪 Source : <b>{html.escape(deal.merchant)}</b> via Dealabs\n"
        f"{price_line}"
        f"📦 Format : <b>{html.escape(analysis.format_label)}</b>\n"
        f"📈 Revente habituelle Vinted : <b>{html.escape(format_range(analysis.resale_min, analysis.resale_max))}</b>\n"
        f"{margin_line}"
        f"{temp}\n\n"
        f"⚠️ Score : <b>{analysis.score}/10</b>\n"
        f"✅ Action : <b>{html.escape(analysis.action_label)}</b>\n\n"
        f"🔎 Raisons :\n{reasons}\n\n"
        f"⚠️ Vérifie toujours : état réel, frais de port, vendeur, stock, et prix Vinted avant achat."
    )


def passes_alert_threshold(analysis: Analysis) -> bool:
    reliable_ok = analysis.price_reliable and analysis.score >= MIN_SCORE_ALERT
    uncertain_ok = SEND_UNCERTAIN_DEALS and not analysis.price_reliable and analysis.score >= MIN_SCORE_UNCERTAIN
    return bool(reliable_ok or uncertain_ok)


def scan_detailed(include_low_scores: bool = False) -> Tuple[List[Tuple[Deal, Analysis]], Dict[str, Any]]:
    """Scan avec statistiques détaillées pour régler le bot."""
    ensure_config_files()
    keywords = load_json_file(KEYWORDS_FILE, DEFAULT_KEYWORDS)
    rules = load_json_file(RULES_FILE, DEFAULT_RULES)

    stats: Dict[str, Any] = {
        "feeds_configured": split_env_list(os.environ.get("DEALABS_FEEDS", ""), DEFAULT_DEALABS_FEEDS),
        "raw_from_rss": 0,
        "raw_from_html": 0,
        "raw_total": 0,
        "unique_total": 0,
        "expired_ignored": 0,
        "no_category_ignored": 0,
        "urgent_outside_category_ignored": 0,
        "no_price_ignored": 0,
        "exclusion_detected": 0,
        "uncertain_price": 0,
        "analyzed_total": 0,
        "eligible_total": 0,
        "reliable_alerts": 0,
        "uncertain_alerts": 0,
        "best_score": None,
        "best_title": None,
        "last_error": None,
    }

    raw_deals: List[Deal] = []
    try:
        rss_deals = fetch_dealabs_feeds()
        stats["raw_from_rss"] = len(rss_deals)
        raw_deals.extend(rss_deals)
    except Exception as exc:
        stats["last_error"] = f"RSS: {exc}"
        log.exception("Erreur RSS dans scan_detailed")

    try:
        html_deals = fetch_dealabs_search_pages()
        stats["raw_from_html"] = len(html_deals)
        raw_deals.extend(html_deals)
    except Exception as exc:
        stats["last_error"] = f"HTML: {exc}"
        log.exception("Erreur HTML dans scan_detailed")

    stats["raw_total"] = len(raw_deals)
    deals = dedupe_deals(raw_deals)
    stats["unique_total"] = len(deals)

    all_results: List[Tuple[Deal, Analysis]] = []
    eligible_results: List[Tuple[Deal, Analysis]] = []

    for deal in deals:
        full_text = f"{deal.title} {deal.description} {deal.merchant}"
        norm = normalize_text(full_text)
        if is_expired_deal_text(full_text):
            stats["expired_ignored"] += 1
            continue
        if any_keyword(norm, keywords.get("exclusions", [])):
            stats["exclusion_detected"] += 1
        if deal.uncertainty_reasons or not deal.price_reliable:
            stats["uncertain_price"] += 1

        has_category = category_for_deal(deal, rules) is not None
        urgent = any_keyword(norm, keywords.get("urgent", [])) is not None
        if CATEGORY_GATE and not has_category:
            stats["no_category_ignored"] += 1
            if urgent:
                stats["urgent_outside_category_ignored"] += 1
            continue
        if not has_category and not urgent:
            stats["no_category_ignored"] += 1
            continue
        if deal.price is None and not (urgent or (SEND_UNCERTAIN_DEALS and has_category and (deal.uncertainty_reasons or []))):
            stats["no_price_ignored"] += 1

        analysis = analyze_deal(deal, keywords, rules)
        if not analysis:
            continue
        stats["analyzed_total"] += 1
        all_results.append((deal, analysis))
        if stats["best_score"] is None or analysis.score > stats["best_score"]:
            stats["best_score"] = analysis.score
            stats["best_title"] = deal.title[:120]

        if passes_alert_threshold(analysis):
            stats["eligible_total"] += 1
            if analysis.price_reliable:
                stats["reliable_alerts"] += 1
            else:
                stats["uncertain_alerts"] += 1
            eligible_results.append((deal, analysis))

    all_results.sort(key=lambda x: (x[1].score, x[0].temperature or 0), reverse=True)
    eligible_results.sort(key=lambda x: (x[1].score, x[0].temperature or 0), reverse=True)
    return (all_results if include_low_scores else eligible_results), stats


def scan_once(include_low_scores: bool = False) -> List[Tuple[Deal, Analysis]]:
    results, _ = scan_detailed(include_low_scores=include_low_scores)
    return results


def build_debug_message(stats: Dict[str, Any]) -> str:
    feeds = stats.get("feeds_configured") or []
    feeds_text = "\n".join(f"• {html.escape(str(f))}" for f in feeds[:5]) or "• aucun"
    best_score = stats.get("best_score")
    best_title = stats.get("best_title") or "aucun"
    best_line = f"{html.escape(str(best_title))} — score {best_score}/10" if best_score is not None else "aucun"
    return (
        "🔎 <b>Diagnostic Dealabs</b>\n\n"
        f"<b>Sources RSS :</b>\n{feeds_text}\n"
        f"HTML fallback : <b>{USE_HTML_FALLBACK}</b>\n"
        f"Mode prix strict : <b>{STRICT_PRICE_MODE}</b>\n"
        f"Protection chèques/cashback/nouveaux clients : <b>{PROMO_VALUE_GUARD}</b>\n\n"
        f"Deals RSS récupérés : <b>{stats.get('raw_from_rss', 0)}</b>\n"
        f"Deals HTML récupérés : <b>{stats.get('raw_from_html', 0)}</b>\n"
        f"Deals bruts : <b>{stats.get('raw_total', 0)}</b>\n"
        f"Deals uniques : <b>{stats.get('unique_total', 0)}</b>\n\n"
        f"Analysés : <b>{stats.get('analyzed_total', 0)}</b>\n"
        f"Éligibles alerte : <b>{stats.get('eligible_total', 0)}</b>\n"
        f"Alertes fiables : <b>{stats.get('reliable_alerts', 0)}</b>\n"
        f"Alertes jaunes : <b>{stats.get('uncertain_alerts', 0)}</b>\n\n"
        f"Ignorés hors cible : <b>{stats.get('no_category_ignored', 0)}</b>\n"
        f"Ignorés prix absent : <b>{stats.get('no_price_ignored', 0)}</b>\n"
        f"Prix incertains repérés : <b>{stats.get('uncertain_price', 0)}</b>\n"
        f"Exclusions repérées : <b>{stats.get('exclusion_detected', 0)}</b>\n\n"
        f"Meilleur candidat : <b>{best_line}</b>\n"
        f"Dernière erreur : <b>{html.escape(str(stats.get('last_error') or 'aucune'))}</b>"
    )


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
        results, stats = scan_detailed(include_low_scores=(manual_chat_id is not None and MANUAL_INCLUDE_LOW_SCORES))
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = stats.get("last_error")
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
        if not MANUAL_INCLUDE_LOW_SCORES:
            is_uncertain_ok = SEND_UNCERTAIN_DEALS and not analysis.price_reliable and analysis.score >= MIN_SCORE_UNCERTAIN
            is_reliable_ok = analysis.price_reliable and analysis.score >= MIN_SCORE_ALERT
            if not (is_reliable_ok or is_uncertain_ok):
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



def get_summary_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(DAILY_SUMMARY_TIMEZONE))
    except Exception:
        return datetime.now(timezone.utc)


def should_send_daily_summary(state: Dict[str, Any]) -> bool:
    if not DAILY_SUMMARY_ENABLED:
        return False
    now = get_summary_now()
    if now.hour < DAILY_SUMMARY_HOUR or (now.hour == DAILY_SUMMARY_HOUR and now.minute < DAILY_SUMMARY_MINUTE):
        return False
    today_key = now.date().isoformat()
    return state.get("last_daily_summary_date") != today_key



def format_price(value: Optional[float]) -> str:
    """Formate un prix en euros pour les messages Telegram."""
    if value is None:
        return "à vérifier"
    try:
        return f"{float(value):.2f} €".replace(".", ",")
    except (TypeError, ValueError):
        return "à vérifier"


def build_daily_summary_message(stats: Dict[str, Any], candidates: List[Tuple[Deal, Analysis]]) -> str:
    now = get_summary_now()
    lines = [
        f"📊 <b>Récap Bot Revente — {now.strftime('%d/%m %H:%M')}</b>",
        "",
        f"Deals récupérés : <b>{stats.get('unique_total', 0)}</b>",
        f"Deals gaming analysés : <b>{stats.get('analyzed_total', 0)}</b>",
        f"Alertes fiables détectées : <b>{stats.get('reliable_alerts', 0)}</b>",
        f"Alertes jaunes détectées : <b>{stats.get('uncertain_alerts', 0)}</b>",
        f"Prix incertains repérés : <b>{stats.get('uncertain_price', 0)}</b>",
        "",
    ]
    if candidates:
        lines.append(f"🏆 <b>Top {min(DAILY_SUMMARY_TOP_N, len(candidates))} candidats du moment</b>")
        for i, (deal, analysis) in enumerate(candidates[:DAILY_SUMMARY_TOP_N], start=1):
            title = html.escape(deal.title[:95] + ("…" if len(deal.title) > 95 else ""))
            price = "à vérifier" if not analysis.price_reliable else (format_price(deal.price) if deal.price is not None else "non détecté")
            margin = "non calculée" if analysis.margin_min is None else f"{format_price(analysis.margin_min)} – {format_price(analysis.margin_max)}"
            lines.append(f"{i}. <b>{title}</b>")
            lines.append(f"   Score : <b>{analysis.score}/10</b> | Prix : <b>{html.escape(price)}</b> | Marge : <b>{html.escape(margin)}</b>")
    else:
        lines.append("Aucun candidat gaming intéressant trouvé aujourd’hui.")
    lines.extend([
        "",
        "✅ Bot actif. Pas d’alerte = aucun deal assez rentable selon les seuils actuels.",
        "Utilise /check_large pour voir les candidats détaillés."
    ])
    return "\n".join(lines)


async def send_daily_summary(application: Application) -> None:
    state = load_state()
    subscribers = state.get("subscribers", [])
    if not subscribers:
        return
    try:
        candidates, stats = scan_detailed(include_low_scores=True)
        message = build_daily_summary_message(stats, candidates)
        for chat_id in subscribers:
            try:
                await application.bot.send_message(
                    chat_id=int(chat_id),
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                log.warning("Erreur envoi résumé quotidien chat %s: %s", chat_id, exc)
        now = get_summary_now()
        state["last_daily_summary_date"] = now.date().isoformat()
        state["last_daily_summary"] = now.isoformat()
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = stats.get("last_error")
        save_state(state)
        log.info("Résumé quotidien envoyé à %s abonné(s)", len(subscribers))
    except Exception as exc:
        log.exception("Erreur résumé quotidien")
        state["last_error"] = f"Résumé quotidien: {exc}"
        save_state(state)

async def scanner_loop(application: Application) -> None:
    while True:
        state = load_state()
        if state.get("subscribers"):
            total, sent = await run_scan_and_alert(application)
            log.info("Scan automatique terminé: %s deals filtrés, %s alertes envoyées", total, sent)
            refreshed_state = load_state()
            if should_send_daily_summary(refreshed_state):
                await send_daily_summary(application)
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
        candidates, stats = scan_detailed(include_low_scores=True)
        msg = (
            "Aucune opportunité vraiment intéressante trouvée pour l'instant.\n"
            f"Deals récupérés : {stats.get('unique_total', 0)}.\n"
            f"Deals analysés : {stats.get('analyzed_total', 0)}.\n"
            f"Deals éligibles : {stats.get('eligible_total', 0)}.\n"
            f"Meilleur score : {stats.get('best_score') if stats.get('best_score') is not None else 'aucun'}."
        )
        await update.message.reply_text(msg)
        if SEND_BEST_CANDIDATE_ON_MANUAL_CHECK and candidates:
            best_deal, best_analysis = candidates[0]
            await update.message.reply_text("🟡 Meilleur candidat trouvé, mais pas assez fiable/rentable pour alerte automatique :")
            await send_deal(context.application, update.effective_chat.id, best_deal, best_analysis)


async def check_large(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔎 Scan large lancé : j'envoie jusqu'à 5 meilleurs candidats, même non parfaits...")
    try:
        results, stats = scan_detailed(include_low_scores=True)
        if not results:
            await update.message.reply_text(
                "Aucun candidat détecté en mode large.\n"
                f"Deals récupérés : {stats.get('unique_total', 0)} / analysés : {stats.get('analyzed_total', 0)}"
            )
            return
        await update.message.reply_text(
            f"{len(results)} candidat(s) détecté(s). J'envoie les {min(5, len(results))} meilleurs.\n"
            "Attention : certains peuvent être non rentables ou à vérifier."
        )
        for deal, analysis in results[:5]:
            await send_deal(context.application, update.effective_chat.id, deal, analysis)
    except Exception as exc:
        log.exception("Erreur check_large")
        await update.message.reply_text(f"❌ Erreur pendant le scan large : {exc}")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔎 Diagnostic en cours...")
    try:
        _, stats = scan_detailed(include_low_scores=True)
        await update.message.reply_text(build_debug_message(stats), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.exception("Erreur debug")
        await update.message.reply_text(f"❌ Erreur diagnostic : {exc}")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📊 Récap manuel en cours...")
    try:
        candidates, stats = scan_detailed(include_low_scores=True)
        await update.message.reply_text(
            build_daily_summary_message(stats, candidates),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.exception("Erreur summary")
        await update.message.reply_text(f"❌ Erreur pendant le récap : {exc}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    await update.message.reply_text(
        f"🤖 {APP_NAME}\n"
        f"État : en ligne\n"
        f"Abonnés : {len(state.get('subscribers', []))}\n"
        f"Intervalle scan : {CHECK_INTERVAL_SECONDS} sec\n"
        f"Score minimum alerte : {MIN_SCORE_ALERT}/10\n"
        f"Scan manuel élargi : {MANUAL_INCLUDE_LOW_SCORES}\n"
        f"Deals incertains : {SEND_UNCERTAIN_DEALS} / score jaune min {MIN_SCORE_UNCERTAIN}\n"
        f"HTML fallback : {USE_HTML_FALLBACK}\n"
        f"Dernier scan : {state.get('last_scan') or 'aucun'}\n"
        f"Résumé quotidien : {DAILY_SUMMARY_ENABLED} à {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d} ({DAILY_SUMMARY_TIMEZONE})\n"
        f"Dernier récap : {state.get('last_daily_summary') or 'aucun'}\n"
        f"Dernière erreur : {state.get('last_error') or 'aucune'}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commandes disponibles :\n"
        "/start_revente — activer les alertes\n"
        "/check — scanner maintenant\n"
        "/status — voir l'état\n"
        "/summary — récap manuel\n"
        "/stop_revente — arrêter les alertes\n\n"
        "V1.8 = Dealabs avec filtre catégorie, protection prix promo, debug et récap quotidien."
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
    application.add_handler(CommandHandler("check_large", check_large))
    application.add_handler(CommandHandler("debug", debug_cmd))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("summary", summary_cmd))
    application.add_handler(CommandHandler("recap", summary_cmd))
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
