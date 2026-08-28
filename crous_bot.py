import os
import json
import logging
import re
import asyncio
from typing import List, Dict, Optional, Set

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Configuration des logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CrousBot")

# Chargement des variables d'environnement
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
CROUS_SEARCH_URL = os.getenv(
    "CROUS_SEARCH_URL",
    "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=1.3503956_43.668708_1.5153795_43.532654&locationName=Toulouse",
)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

COOKIES_FILE = "cookies.json"
VUS_FILE = "logements_vus.json"

# Image de secours si l'image principale n'est pas disponible
DEFAULT_HOUSING_IMAGE = "https://trouverunlogement.lescrous.fr/favicon.ico"


# ----------------------------------------------------
# GESTION DU STOCKAGE LOCAL (Anti-spam / Doublons)
# ----------------------------------------------------
def load_seen_logements() -> Set[str]:
    """Charge la liste des identifiants de logements déjà vus."""
    if not os.path.exists(VUS_FILE):
        return set()
    try:
        with open(VUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logger.error(f"Erreur lors de la lecture de {VUS_FILE}: {e}")
        return set()


def save_seen_logement(housing_id: str) -> None:
    """Ajoute un ID de logement au fichier des logements vus."""
    seen = load_seen_logements()
    seen.add(housing_id)
    try:
        with open(VUS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur lors de la sauvegarde dans {VUS_FILE}: {e}")


# ----------------------------------------------------
# GESTION PLAYWRIGHT & SESSION
# ----------------------------------------------------
async def load_cookies_into_context(context: BrowserContext) -> bool:
    """Charge les cookies depuis la variable d'environnement CROUS_COOKIES_JSON ou depuis cookies.json."""
    raw_cookies = None

    # 1. Recherche dans la variable d'environnement (idéal pour Render / Railway / Cloud)
    env_cookies = os.getenv("CROUS_COOKIES_JSON")
    if env_cookies:
        try:
            raw_cookies = json.loads(env_cookies)
            logger.info("Cookies chargés depuis la variable d'environnement CROUS_COOKIES_JSON.")
        except Exception as e:
            logger.error(f"Erreur de lecture JSON depuis CROUS_COOKIES_JSON: {e}")

    # 2. Repli sur le fichier local cookies.json
    if not raw_cookies and os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                raw_cookies = json.load(f)
                logger.info(f"Cookies chargés depuis le fichier {COOKIES_FILE}.")
        except Exception as e:
            logger.error(f"Erreur lors du chargement des cookies depuis {COOKIES_FILE}: {e}")

    if not raw_cookies:
        logger.warning("Aucun cookie de session trouvé (ni dans CROUS_COOKIES_JSON, ni dans cookies.json).")
        return False

    try:
        clean_cookies = []
        for c in raw_cookies:
            cookie = {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", "trouverunlogement.lescrous.fr"),
                "path": c.get("path", "/"),
            }
            if "httpOnly" in c:
                cookie["httpOnly"] = bool(c["httpOnly"])
            if "secure" in c:
                cookie["secure"] = bool(c["secure"])
            if "expires" in c and c["expires"] is not None:
                cookie["expires"] = float(c["expires"])
            if "sameSite" in c and c["sameSite"]:
                same_site = str(c["sameSite"]).capitalize()
                if same_site in ["Lax", "Strict", "None"]:
                    cookie["sameSite"] = same_site
            clean_cookies.append(cookie)
        await context.add_cookies(clean_cookies)
        logger.info(f"{len(clean_cookies)} cookies de session appliqués avec succès.")
        return True
    except Exception as e:
        logger.error(f"Erreur lors du chargement des cookies: {e}")
        return False


async def check_session_expired(page: Page, bot, chat_id: str) -> bool:
    """
    Vérifie si la session a expiré (ex: redirection vers la page de login).
    Si expirée, envoie une alerte sur Telegram.
    """
    url = page.url
    # Les URL typiques de login Crous / MesServicesEtudiant
    is_login_page = "login" in url.lower() or "connexion" in url.lower() or "cas.etudiant.gouv.fr" in url.lower()

    if is_login_page:
        logger.warning("Session expirée détectée ! Redirection vers la page de connexion.")
        msg = (
            "⚠️ <b>ALERTE SESSION CROUS EXPIRÉE !</b>\n\n"
            "Le script a été redirigé vers la page de connexion.\n"
            "Veuillez mettre à jour le fichier <code>cookies.json</code> avec votre nouvelle session."
        )
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Erreur envoi alerte Telegram: {e}")
        return True
    return False


async def scrape_crous_toulouse(playwright, bot, chat_id: str) -> List[Dict]:
    """
    Scrape la liste des logements Crous disponibles à Toulouse.
    Renvoie une liste de dictionnaires contenant les détails des logements.
    """
    browser: Browser = await playwright.chromium.launch(headless=HEADLESS)
    context: BrowserContext = await browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )

    has_cookies = await load_cookies_into_context(context)
    page: Page = await context.new_page()

    offres = []
    try:
        logger.info(f"Navigation vers la page de recherche Crous: {CROUS_SEARCH_URL}")
        await page.goto(CROUS_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        # Vérification d'expiration de session
        if has_cookies and await check_session_expired(page, bot, chat_id):
            await browser.close()
            return []

        # 1. Extraction directe via les liens d'offres /offre/
        offer_links = await page.query_selector_all("a[href*='/offre/']")
        logger.info(f"{len(offer_links)} liens d'offres directs trouvés.")

        seen_ids_in_page = set()

        for link in offer_links:
            try:
                href = await link.get_attribute("href") or ""
                id_match = re.search(r"/offre/([a-zA-Z0-9_-]+)", href)
                if not id_match:
                    continue
                housing_id = id_match.group(1)
                if housing_id in seen_ids_in_page:
                    continue
                seen_ids_in_page.add(housing_id)

                link_url = href if href.startswith("http") else f"https://trouverunlogement.lescrous.fr{href}"

                # Recherche de l'élément conteneur parent
                card = await link.evaluate_handle("el => el.closest('article, .fr-card, .card, li, div') || el")
                card_text = await card.evaluate("el => el.innerText || ''")

                # Titre / Résidence
                title_match = re.search(r"(Résidence\s+[^|\n]+)", card_text, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else "Résidence Crous Toulouse"

                # Type
                type_match = re.search(r"(Studio|T1|T2|T3|Chambre|Colocation)", card_text, re.IGNORECASE)
                type_logement = type_match.group(1).capitalize() if type_match else "Studio / Logement étudiant"

                # Surface
                surface_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*m²", card_text)
                surface = surface_match.group(1) if surface_match else "N/C"

                # Loyer
                price_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*€", card_text)
                loyer = price_match.group(1) if price_match else "N/C"

                # Image
                img_elem = await card.query_selector("img")
                photo_url = DEFAULT_HOUSING_IMAGE
                if img_elem:
                    src = await img_elem.get_attribute("src") or await img_elem.get_attribute("data-src")
                    if src:
                        photo_url = src if src.startswith("http") else f"https://trouverunlogement.lescrous.fr{src}"

                offres.append({
                    "id": housing_id,
                    "title": title,
                    "type": type_logement,
                    "surface": surface,
                    "loyer": loyer,
                    "photo": photo_url,
                    "link": link_url
                })
            except Exception as card_err:
                logger.error(f"Erreur d'extraction d'une offre: {card_err}")
                continue

        # 2. Repli si aucun lien direct extrait
        if not offres:
            cards = await page.query_selector_all(".fr-card, article, .housing-item, .card, div[class*='card']")
            logger.info(f"Repli : {len(cards)} cartes d'offres trouvées par sélecteur de carte.")
            for card in cards:
                try:
                    link_elem = await card.query_selector("a.fr-card__link, a[href*='/offre/'], a")
                    link_url = ""
                    housing_id = ""
                    if link_elem:
                        href = await link_elem.get_attribute("href")
                        if href:
                            link_url = href if href.startswith("http") else f"https://trouverunlogement.lescrous.fr{href}"
                            id_match = re.search(r"/(?:offre|logement)/([a-zA-Z0-9_-]+)", href)
                            if id_match:
                                housing_id = id_match.group(1)

                    if not housing_id:
                        data_id = await card.get_attribute("data-id") or await card.get_attribute("id")
                        housing_id = data_id if data_id else str(hash(link_url))

                    title_elem = await card.query_selector(".fr-card__title, .card-title, h3, h2, .title")
                    title = (await title_elem.inner_text()).strip() if title_elem else "Résidence Crous Toulouse"

                    detail_elem = await card.query_selector(".fr-card__detail, .card-subtitle, .detail, p")
                    detail_text = (await detail_elem.inner_text()).strip() if detail_elem else ""

                    type_logement = "Studio / Logement étudiant"
                    surface = "N/C"
                    type_match = re.search(r"(Studio|T1|T2|T3|Chambre|Colocation)", detail_text, re.IGNORECASE)
                    if type_match:
                        type_logement = type_match.group(1).capitalize()
                    surface_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*m²", detail_text)
                    if surface_match:
                        surface = surface_match.group(1)

                    price_elem = await card.query_selector(".fr-card__desc, .price, .loyer, span:has-text('€')")
                    price_text = (await price_elem.inner_text()).strip() if price_elem else ""
                    if not price_text:
                        card_full_text = await card.inner_text()
                        price_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*€", card_full_text)
                        loyer = price_match.group(1) if price_match else "N/C"
                    else:
                        price_match = re.search(r"(\d+(?:[\.,]\d+)?)", price_text)
                        loyer = price_match.group(1) if price_match else "N/C"

                    img_elem = await card.query_selector("img")
                    photo_url = DEFAULT_HOUSING_IMAGE
                    if img_elem:
                        src = await img_elem.get_attribute("src") or await img_elem.get_attribute("data-src")
                        if src:
                            photo_url = src if src.startswith("http") else f"https://trouverunlogement.lescrous.fr{src}"

                    offres.append({
                        "id": housing_id,
                        "title": title,
                        "type": type_logement,
                        "surface": surface,
                        "loyer": loyer,
                        "photo": photo_url,
                        "link": link_url or CROUS_SEARCH_URL
                    })
                except Exception as card_err:
                    logger.error(f"Erreur d'extraction d'une carte: {card_err}")
                    continue

    except Exception as e:
        logger.error(f"Erreur lors du scraping Crous: {e}")
    finally:
        await browser.close()

    return offres


async def action_ajouter_aux_voeux(housing_id: str, housing_url: str) -> bool:
    """
    Exécute l'action Playwright pour ajouter le logement spécifique aux vœux.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()

        has_cookies = await load_cookies_into_context(context)
        if not has_cookies:
            logger.error("Impossible d'ajouter aux vœux: cookies.json invalide ou inexistant.")
            await browser.close()
            return False

        page = await context.new_page()
        try:
            logger.info(f"Navigation vers le logement {housing_id} : {housing_url}")
            await page.goto(housing_url, wait_until="domcontentloaded", timeout=30000)

            # Vérification de session
            if "login" in page.url.lower() or "connexion" in page.url.lower():
                logger.error("Session expirée lors de l'ajout aux vœux.")
                await browser.close()
                return False

            # Recherche du bouton d'ajout aux vœux
            button_selectors = [
                "button:has-text('Ajouter à mes vœux')",
                "button:has-text('Ajouter aux vœux')",
                "button:has-text('Demander ce logement')",
                "a:has-text('Ajouter à mes vœux')",
                ".btn-voeu",
                "[data-action='add-voeu']"
            ]

            click_success = False
            for selector in button_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        logger.info(f"Bouton trouvé avec le sélecteur: {selector}")
                        await btn.click()
                        click_success = True
                        break
                except Exception:
                    continue

            if not click_success:
                logger.error(f"Bouton d'ajout aux vœux introuvable pour le logement {housing_id}")
                await browser.close()
                return False

            # Attente de la confirmation (toast, modal ou modification d'état du bouton)
            await page.wait_for_timeout(3000)
            logger.info(f"Action d'ajout aux vœux exécutée avec succès pour {housing_id}.")
            await browser.close()
            return True

        except Exception as e:
            logger.error(f"Erreur lors de l'action d'ajout aux vœux ({housing_id}): {e}")
            await browser.close()
            return False


# ----------------------------------------------------
# TELEGRAM BOT HANDLERS & NOTIFICATIONS
# ----------------------------------------------------
async def send_housing_notification(bot, chat_id: str, housing: Dict):
    """Formatte et envoie une notification Telegram interactive pour un nouveau logement."""
    caption = (
        f"🏠 <b>NOUVEAU LOGEMENT CROUS TOULOUSE !</b>\n\n"
        f"🏢 <b>Résidence :</b> {housing['title']}\n"
        f"📐 <b>Type & Surface :</b> {housing['type']} ({housing['surface']} m²)\n"
        f"💰 <b>Loyer mensuel :</b> {housing['loyer']} € / mois\n"
        f"🆔 <b>Identifiant :</b> <code>{housing['id']}</code>\n"
    )

    # Boutons interactifs Telegram (Inline Keyboard)
    keyboard = [
        [
            InlineKeyboardButton("🔗 Voir l'annonce", url=housing['link']),
        ],
        [
            InlineKeyboardButton(
                "⚡ Ajouter automatiquement à mes vœux",
                callback_data=f"add_voeu:{housing['id']}"
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Envoi de la photo avec les détails HTML et les boutons
        await bot.send_photo(
            chat_id=chat_id,
            photo=housing['photo'],
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except Exception as photo_err:
        logger.warning(f"Échec de l'envoi de la photo, envoi du message texte à la place: {photo_err}")
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def handle_callback_add_voeu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur le bouton '⚡ Ajouter automatiquement à mes vœux'."""
    query = update.callback_query
    await query.answer("⚡ Traitement de votre demande d'ajout aux vœux en cours...")

    data = query.data
    if not data.startswith("add_voeu:"):
        return

    housing_id = data.split(":")[1]
    logger.info(f"Clic reçu sur Telegram pour l'ajout du logement ID: {housing_id}")

    # Récupération de l'URL du logement depuis le bouton 'Voir l'annonce'
    housing_url = f"https://trouverunlogement.lescrous.fr/offre/{housing_id}"
    if query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.url and "trouverunlogement" in btn.url:
                    housing_url = btn.url
                    break

    # Lancement de l'action d'ajout automatique Playwright
    success = await action_ajouter_aux_voeux(housing_id, housing_url)

    if success:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ <b>Logement ID {housing_id} ajouté avec succès à vos vœux !</b>",
            parse_mode="HTML",
            reply_to_message_id=query.message.message_id
        )
    else:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"❌ <b>Échec lors de l'ajout aux vœux pour l'ID {housing_id}.</b>\n\n"
                "Vérifiez que votre fichier <code>cookies.json</code> est toujours valide et actif."
            ),
            parse_mode="HTML",
            reply_to_message_id=query.message.message_id
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start pour vérifier, enregistrer et envoyer tous les logements actuellement disponibles."""
    global TELEGRAM_CHAT_ID
    chat_id = str(update.effective_chat.id)
    TELEGRAM_CHAT_ID = chat_id

    await update.message.reply_text(
        f"🤖 <b>Bot Crous Toulouse d'automatisation actif !</b>\n\n"
        f"✅ Chat ID : <code>{chat_id}</code>\n"
        f"🔎 <b>Recherche et envoi de tous les logements actuellement disponibles sur Telegram...</b>",
        parse_mode="HTML",
    )

    # Force l'envoi de tous les logements actuellement disponibles sur le site Crous
    asyncio.create_task(run_monitoring_cycle(context.bot, chat_id, force_notify_all=True))


# ----------------------------------------------------
# TÂCHE RÉCURRENTE DE MONITORING
# ----------------------------------------------------
async def run_monitoring_cycle(bot, chat_id: str, force_notify_all: bool = False):
    """Exécute un cycle de scraping et notifie les logements."""
    if not chat_id or chat_id == "YOUR_TELEGRAM_CHAT_ID_HERE":
        logger.warning("TELEGRAM_CHAT_ID non configuré. Envoyez /start à votre bot sur Telegram pour enregistrer votre Chat ID.")
        return

    logger.info("--- Démarrage du cycle de surveillance Crous Toulouse ---")
    seen_ids = set() if force_notify_all else load_seen_logements()

    async with async_playwright() as p:
        offres = await scrape_crous_toulouse(p, bot, chat_id)

    nouveaux_compteur = 0
    for housing in offres:
        if force_notify_all or housing['id'] not in seen_ids:
            logger.info(f"Notification logement : {housing['title']} (ID: {housing['id']})")
            await send_housing_notification(bot, chat_id, housing)
            save_seen_logement(housing['id'])
            seen_ids.add(housing['id'])
            nouveaux_compteur += 1

    logger.info(f"Cycle terminé : {nouveaux_compteur} logement(s) notifié(s).")


async def scheduled_monitoring_job(context: ContextTypes.DEFAULT_TYPE):
    """Job exécuté périodiquement par le bot Telegram."""
    await run_monitoring_cycle(context.bot, TELEGRAM_CHAT_ID)


# ----------------------------------------------------
# MAIN ENTRY POINT & SERVEUR HTTP POUR RENDER GRATUIT
# ----------------------------------------------------
def start_health_server():
    """Démarre un petit serveur HTTP pour valider le plan Web Service Gratuit de Render."""
    import http.server
    import socketserver
    import threading

    port = int(os.getenv("PORT", "8080"))

    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Bot Crous Toulouse OK")

        def log_message(self, format, *args):
            pass

    try:
        server = socketserver.TCPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Serveur HTTP de santé démarré sur le port {port} (Mode Render Free Web Service)")
    except Exception as e:
        logger.warning(f"Impossible de démarrer le serveur HTTP de santé: {e}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERREUR: TELEGRAM_BOT_TOKEN doit être configuré dans le fichier .env")
        return

    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "YOUR_TELEGRAM_CHAT_ID_HERE":
        print("⚠️ TELEGRAM_CHAT_ID non renseigné. Le bot va démarrer. Envoyez /start au bot sur Telegram pour vous enregistrer.")

    # Démarrage du serveur web si lancé en tant que Web Service (Render Free)
    if os.getenv("PORT"):
        start_health_server()

    print("🚀 Initialisation du Bot Crous Toulouse...")

    # Création de l'application Telegram
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback_add_voeu, pattern="^add_voeu:"))

    # Planification du Job de surveillance
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(scheduled_monitoring_job, interval=CHECK_INTERVAL, first=5)
        logger.info(f"Job de surveillance planifié toutes les {CHECK_INTERVAL} secondes.")

    # Démarrage du bot Telegram en mode polling
    application.run_polling()


if __name__ == "__main__":
    main()
