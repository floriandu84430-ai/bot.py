import os
import time
import threading
import logging
import requests
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ===== LOGS =====
logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "METS_TON_TOKEN_ICI")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6791451829"))
PAYPAL_LINK = "https://www.paypal.me/FrankRoger149"
SUPPORT_USERNAME = "@fr26ulka"

# ===== STATES =====
user_state = {}
pending_payments = {}
BOT_OUVERT = True
commandes_count = {}
approved_orders = set()

# ===== CART TIMER =====
cart_timestamps = {}
warned_users = set()

# ===== TRANCHES =====
tranches = {
    "25-49":   {"label": "25→49 pts",   "file": "25-49.txt",   "prix": 1, "min_pts": 25},
    "50-74":   {"label": "50→74 pts",   "file": "50-74.txt",   "prix": 2, "min_pts": 50},
    "75-99":   {"label": "75→99 pts",   "file": "75-99.txt",   "prix": 3, "min_pts": 75},
    "100-124": {"label": "100→124 pts", "file": "100-124.txt", "prix": 4, "min_pts": 100},
    "125-149": {"label": "125→149 pts", "file": "125-149.txt", "prix": 5, "min_pts": 125},
    "150-174": {"label": "150→174 pts", "file": "150-174.txt", "prix": 6, "min_pts": 150},
    "175-199": {"label": "175→199 pts", "file": "175-199.txt", "prix": 7, "min_pts": 175},
    "200-400": {"label": "200→400 pts", "file": "200-400.txt", "prix": 8, "min_pts": 200},
}

cart = {}
locks = {t: threading.Lock() for t in tranches}

# ===== PROMO MESSAGE =====
PROMO_MESSAGE = """
🍟 MC DO COMMUNITY

🔥 Rejoins la communauté pour accéder à :

⚡ Accès anticipé aux nouveaux liens
🍟 Liens exclusifs sélectionnés
🚨 Alertes instantanées

🌐 Groupe public :
https://t.me/fr26ulkaa

🔒 Groupe privé :
https://t.me/+zj7floPVaj02NjJk

💡 Les meilleurs liens partent ici en premier
"""

MESSAGE_FERME = """
🔒 La boutique est temporairement fermée.

⏰ Reviens plus tard !

📩 Pour être informé de la réouverture :
👉 https://t.me/fr26ulkaa
"""

# ===== VÉRIFICATION LIEN (triple vérification) =====
def check_link_and_get_points(url):
    """
    Triple vérification :
    1. Disponibilité (code 200)
    2. Absence de mots d'erreur
    3. Lecture du nombre de points réels
    Retourne le nombre de points (int) ou None si invalide.
    """
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"}
    try:
        r = requests.get(url, headers=headers, timeout=5)

        # 1. Disponibilité
        if r.status_code != 200:
            return None

        page_text = r.text.lower()

        # 2. Détection d'erreurs
        erreurs = ["indisponible", "expire", "erreur", "oups", "tentative"]
        if any(err in page_text for err in erreurs):
            return None

        # 3. Lecture des points
        match = re.search(r'(\d+)\s*point', page_text)
        if match:
            points = int(match.group(1))
            if points == 0:
                return None
            return points

    except Exception:
        pass
    return None


# ===== TROUVER LA BONNE TRANCHE SELON LES POINTS =====
def get_tranche_from_points(points):
    if 25 <= points <= 49:
        return "25-49"
    elif 50 <= points <= 74:
        return "50-74"
    elif 75 <= points <= 99:
        return "75-99"
    elif 100 <= points <= 124:
        return "100-124"
    elif 125 <= points <= 149:
        return "125-149"
    elif 150 <= points <= 174:
        return "150-174"
    elif 175 <= points <= 199:
        return "175-199"
    elif 200 <= points <= 400:
        return "200-400"
    return None


# ===== SAFE EDIT =====
async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup)


# ===== TOUCH CART =====
def touch_cart(user_id):
    cart_timestamps[user_id] = time.time()
    warned_users.discard(user_id)


# ===== HELPER TIMER =====
def get_timer_text(user_id):
    last_seen = cart_timestamps.get(user_id, time.time())
    elapsed = time.time() - last_seen
    remaining_total_sec = max(0, int(10 * 60 - elapsed))
    remaining_min = remaining_total_sec // 60
    remaining_sec = remaining_total_sec % 60

    if remaining_min >= 2:
        return f"⏳ Panier valide encore {remaining_min} min {remaining_sec:02d} sec"
    elif remaining_total_sec > 0:
        return f"🚨 Expire dans {remaining_min} min {remaining_sec:02d} sec !"
    else:
        return "🚨 Panier expiré !"


# ===== CLEANUP JOB =====
async def cleanup_carts(context):
    now = time.time()
    WARN_AT = 8 * 60
    TIMEOUT = 10 * 60
    MAX_SCREENSHOT_WAIT = 2 * 60 * 60  # 2h max même en attente screenshot

    for user_id in list(cart.keys()):
        last_seen = cart_timestamps.get(user_id, 0)
        elapsed = now - last_seen

        # Panier en attente screenshot : expire après 2h max
        if user_state.get(user_id) == "awaiting_screenshot":
            if elapsed > MAX_SCREENSHOT_WAIT:
                user_cart = cart.pop(user_id, {})
                cart_timestamps.pop(user_id, None)
                user_state.pop(user_id, None)
                warned_users.discard(user_id)
                for t, d in user_cart.items():
                    for lien in d["items"]:
                        remettre_stock(t, lien)
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "⏳ Ta commande en attente a expiré (délai de 2h dépassé).\n\n"
                            f"Contacte-moi si tu as déjà payé : {SUPPORT_USERNAME}"
                        )
                    )
                except Exception:
                    pass
            continue

        if elapsed > TIMEOUT:
            user_cart = cart.pop(user_id, {})
            cart_timestamps.pop(user_id, None)
            user_state.pop(user_id, None)
            warned_users.discard(user_id)

            for t, d in user_cart.items():
                for lien in d["items"]:
                    remettre_stock(t, lien)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⏳ Ton panier a expiré.\n\nTes articles ont été remis en stock.\n\nTape /start pour recommencer."
                )
            except Exception:
                pass

        elif elapsed > WARN_AT and user_id not in warned_users:
            warned_users.add(user_id)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Ton panier expire dans 2 minutes !\n\nFinis ta commande ou tes articles seront remis en stock."
                )
            except Exception:
                pass


# ===== STOCK =====
def lire_liens(tranche):
    f = tranches[tranche]["file"]
    if os.path.exists(f):
        with open(f, encoding="utf-8") as file:
            return [l.strip() for l in file if l.strip()]
    return []

def retirer_lien(tranche):
    with locks[tranche]:
        liens = lire_liens(tranche)
        if not liens:
            return None
        lien = liens.pop(0)
        with open(tranches[tranche]["file"], "w", encoding="utf-8") as f:
            f.writelines(l + "\n" for l in liens)
        return lien

def remettre_stock(tranche, lien):
    with locks[tranche]:
        liens = lire_liens(tranche)
        if lien not in liens:
            liens.insert(0, lien)
            with open(tranches[tranche]["file"], "w", encoding="utf-8") as f:
                f.writelines(l + "\n" for l in liens)


# ===== CART =====
def get_cart(user_id):
    return cart.setdefault(user_id, {})

def cart_total(user_cart):
    return sum(d["qty"] * d["prix"] for d in user_cart.values())

def apply_discount(total, user_cart):
    return round(total * 0.9, 2) if len(user_cart) >= 3 else total


# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_OUVERT:
        if update.message:
            await update.message.reply_text(MESSAGE_FERME)
        else:
            await update.callback_query.message.reply_text(MESSAGE_FERME)
        return

    text = (
        "👋 Bienvenue !\n\n"
        "🛍️ Comment utiliser le bot :\n"
        "1. Boutique\n"
        "2. Panier\n"
        "3. Paiement\n"
        "4. Envoie ton screenshot 📸\n"
        "5. Je valide et tu reçois ton lien"
    )

    keyboard = [
        [InlineKeyboardButton("🛍️ Boutique", callback_data="menu")],
        [InlineKeyboardButton("❓ Aide", callback_data="help")]
    ]

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ===== MENU =====
async def show_menu(query):
    keyboard = []

    for t, info in tranches.items():
        nb = len(lire_liens(t))
        if nb > 0:
            text = f"{info['label']} | 📦 {nb} en stock | {info['prix']}€"
            data = f"add|{t}"
        else:
            text = f"{info['label']} | ❌ Rupture de stock"
            data = "none"
        keyboard.append([InlineKeyboardButton(text, callback_data=data)])

    keyboard.append([InlineKeyboardButton("🛒 Panier", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("🔙 Retour", callback_data="start")])

    await safe_edit(query, "🔥 Boutique :", InlineKeyboardMarkup(keyboard))


# ===== PANIER =====
async def refresh_cart(query, user_id):
    user_cart = get_cart(user_id)

    if not user_cart:
        await safe_edit(query, "🛒 Panier vide")
        return

    text = "🛒 TON PANIER :\n\n"
    total = 0
    keyboard = []

    for t, d in user_cart.items():
        qty = d["qty"]
        subtotal = qty * d["prix"]
        total += subtotal
        text += f"{tranches[t]['label']}\n📦 x{qty} = {subtotal}€\n\n"
        keyboard.append([
            InlineKeyboardButton("➖", callback_data=f"minus|{t}"),
            InlineKeyboardButton(str(qty), callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"plus|{t}")
        ])

    final_total = apply_discount(total, user_cart)

    if len(user_cart) >= 3:
        text += "🔥 -10% appliqué\n"

    text += f"\n💰 TOTAL : {final_total}€\n\n"
    text += get_timer_text(user_id)

    keyboard.append([InlineKeyboardButton("🗑️ Vider le panier", callback_data="clear_cart")])
    keyboard.append([InlineKeyboardButton("💳 Payer", callback_data="pay")])
    keyboard.append([InlineKeyboardButton("🔙 Boutique", callback_data="menu")])

    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


# ===== UNKNOWN MESSAGE =====
async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Pour accéder à la boutique, tape /start")


# ===== CALLBACK USER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not BOT_OUVERT:
        await safe_edit(query, MESSAGE_FERME)
        return

    data = query.data
    user_id = query.from_user.id

    if data.startswith(("approve", "reject", "r1", "r2", "r3")):
        return

    try:
        if data == "menu":
            await show_menu(query)

        elif data == "start":
            await start(update, context)

        elif data == "clear_cart":
            user_cart = cart.get(user_id, {})
            for t, d in user_cart.items():
                for lien in d["items"]:
                    remettre_stock(t, lien)
            cart.pop(user_id, None)
            cart_timestamps.pop(user_id, None)
            await safe_edit(query, "🗑️ Panier vidé !\n\nTape /start pour recommencer.", InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Retour à la boutique", callback_data="menu")]
            ]))

        elif data.startswith("add|"):
            t = data.split("|")[1]
            lien = retirer_lien(t)

            if not lien:
                await safe_edit(query, "❌ Stock vide pour cette tranche.")
                return

            # Vérification rapide dès l'ajout au panier
            points = check_link_and_get_points(lien)
            min_pts = tranches[t]["min_pts"]

            if points is None or points < min_pts:
                # Lien invalide ou mal classé : on le recycle dans la bonne tranche
                if points is not None:
                    tranche_reelle = get_tranche_from_points(points)
                    if tranche_reelle:
                        remettre_stock(tranche_reelle, lien)
                # On essaie le lien suivant
                lien = retirer_lien(t)
                if lien:
                    points2 = check_link_and_get_points(lien)
                    if points2 is None or points2 < min_pts:
                        if points2 is not None:
                            tranche_reelle2 = get_tranche_from_points(points2)
                            if tranche_reelle2:
                                remettre_stock(tranche_reelle2, lien)
                        await safe_edit(query, "❌ Aucun lien valide disponible pour cette tranche.\n\nEssaie une autre tranche ou reviens plus tard.")
                        return
                else:
                    await safe_edit(query, "❌ Stock vide pour cette tranche.")
                    return

            user_cart = get_cart(user_id)

            if t not in user_cart:
                user_cart[t] = {"qty": 0, "items": [], "prix": tranches[t]["prix"]}

            user_cart[t]["qty"] += 1
            user_cart[t]["items"].append(lien)

            touch_cart(user_id)
            await refresh_cart(query, user_id)

        elif data == "cart":
            await refresh_cart(query, user_id)

        elif data.startswith("plus|"):
            t = data.split("|")[1]
            user_cart = get_cart(user_id)

            if t in user_cart:
                lien = retirer_lien(t)
                if lien:
                    points = check_link_and_get_points(lien)
                    min_pts = tranches[t]["min_pts"]
                    if points is None or points < min_pts:
                        if points is not None:
                            tranche_reelle = get_tranche_from_points(points)
                            if tranche_reelle:
                                remettre_stock(tranche_reelle, lien)
                    else:
                        user_cart[t]["qty"] += 1
                        user_cart[t]["items"].append(lien)

            touch_cart(user_id)
            await refresh_cart(query, user_id)

        elif data.startswith("minus|"):
            t = data.split("|")[1]
            user_cart = get_cart(user_id)

            if t in user_cart:
                item = user_cart[t]

                if item["qty"] > 1:
                    item["qty"] -= 1
                    remettre_stock(t, item["items"].pop())
                else:
                    for lien in item["items"]:
                        remettre_stock(t, lien)
                    del user_cart[t]

            touch_cart(user_id)
            await refresh_cart(query, user_id)

        elif data == "pay":
            user_cart = get_cart(user_id)

            if not user_cart:
                await safe_edit(query, "🛒 Vide")
                return

            total = apply_discount(cart_total(user_cart), user_cart)
            touch_cart(user_id)

            keyboard = [
                [InlineKeyboardButton("💰 Via PayPal", url=PAYPAL_LINK)],
                [InlineKeyboardButton("📸 J'ai payé", callback_data="paid")],
                [InlineKeyboardButton("🔙 Panier", callback_data="cart")]
            ]

            await safe_edit(
                query,
                f"💳 PAIEMENT\n\n💰 Total : {total}€\n\n{get_timer_text(user_id)}",
                InlineKeyboardMarkup(keyboard)
            )

        elif data == "paid":
            touch_cart(user_id)
            user_state[user_id] = "awaiting_screenshot"
            await safe_edit(query, "📸 Envoie ton screenshot maintenant.")

        elif data == "help":
            await safe_edit(
                query,
                f"📌 Support : {SUPPORT_USERNAME}",
                InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Boutique", callback_data="menu")]])
            )

    except Exception as e:
        logging.error(e)
        await safe_edit(query, "❌ Erreur")


# ===== PHOTO =====
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_state.get(user_id) != "awaiting_screenshot":
        return

    photo = update.message.photo[-1].file_id

    user_cart = cart.get(user_id, {})
    detail = ""
    total = 0

    for t, d in user_cart.items():
        subtotal = d["qty"] * d["prix"]
        total += subtotal
        detail += f"• {tranches[t]['label']} x{d['qty']} = {subtotal}€\n"

    final_total = apply_discount(total, user_cart)
    remise = f"\n⚠️ Remise -10% (base {round(total, 2)}€)" if len(user_cart) >= 3 else ""

    caption = (
        f"📸 Paiement reçu\n"
        f"👤 {user_id}\n\n"
        f"{detail}\n"
        f"✅ Total attendu : {final_total}€{remise}"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Valider", callback_data=f"approve|{user_id}"),
            InlineKeyboardButton("❌ Refuser", callback_data=f"reject|{user_id}")
        ]
    ]

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await update.message.reply_text("✅ Reçu ! En attente validation.")
    user_state[user_id] = None


# ===== ADMIN CALLBACK =====
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data.startswith("approve"):
            user_id = int(data.split("|")[1])

            # Protection double-clic admin
            if user_id in approved_orders:
                await query.answer("⚠️ Déjà validé !", show_alert=True)
                return
            approved_orders.add(user_id)

            user_cart = cart.get(user_id, {})

            # Panier expiré entre screenshot et validation
            if not user_cart:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⚠️ Ton panier avait expiré au moment de la validation.\n\n"
                        f"Contacte-moi si tu as déjà payé : {SUPPORT_USERNAME}"
                    )
                )
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text="⚠️ Panier expiré avant validation ! Le client a été prévenu."
                )
                approved_orders.discard(user_id)
                return

            liens_valides = []
            liens_manquants = 0
            total_demande = sum(d["qty"] for d in user_cart.values())

            for t, d in list(user_cart.items()):
                tranche_attendue = t
                min_pts = tranches[t]["min_pts"]

                for _ in range(d["qty"]):
                    lien = d["items"].pop(0) if d["items"] else None
                    envoye = False
                    tentatives = 0
                    MAX_TENTATIVES = 10

                    while not envoye and tentatives < MAX_TENTATIVES:
                        tentatives += 1

                        if lien is None:
                            lien = retirer_lien(tranche_attendue)

                        if lien is None:
                            # Stock vide pour cette tranche
                            liens_manquants += 1
                            break

                        points = check_link_and_get_points(lien)

                        if points is None:
                            # Lien mort → on jette et on cherche le suivant
                            lien = None
                            continue

                        if points < min_pts:
                            # Lien mal classé → recyclage dans la bonne tranche
                            tranche_reelle = get_tranche_from_points(points)
                            if tranche_reelle:
                                remettre_stock(tranche_reelle, lien)
                            lien = None
                            continue

                        # ✅ Lien parfait
                        liens_valides.append(lien)
                        envoye = True

                    if not envoye and tentatives >= MAX_TENTATIVES:
                        liens_manquants += 1

            # ===== COMMUNICATION ADAPTATIVE =====
            if len(liens_valides) == total_demande:
                # Succès total
                msg = (
                    f"🍟 *TA COMMANDE EST PRÊTE !*\n\n"
                    f"Voici tes *{total_demande}* accès McDo ✅\n"
                    f"Régale-toi bien et bon appétit ! 🍗🍟"
                )
            elif len(liens_valides) > 0:
                # Succès partiel
                msg = (
                    f"🍟 *INFOS SUR TA COMMANDE*\n\n"
                    f"Sur les *{total_demande}* liens commandés, *{len(liens_valides)}* sont confirmés ✅\n\n"
                    f"⚠️ *{liens_manquants}* lien(s) n'ont pas pu être livrés (stock épuisé ou liens invalides).\n\n"
                    f"👉 Contacte-moi pour un remboursement ou un remplacement : {SUPPORT_USERNAME}"
                )
            else:
                # Échec total
                msg = (
                    f"😔 *Désolé, nous n'avons pas pu livrer ta commande.*\n\n"
                    f"Tous les liens de ta tranche sont actuellement indisponibles.\n\n"
                    f"👉 Contacte-moi immédiatement pour être remboursé : {SUPPORT_USERNAME}"
                )

            keyboard_liens = [
                [InlineKeyboardButton(f"🍟 Lien McDo {i+1}", url=l)]
                for i, l in enumerate(liens_valides)
            ]
            keyboard_liens.append([InlineKeyboardButton("🏪 Retour Boutique", callback_data="menu")])

            await context.bot.send_message(
                chat_id=user_id,
                text=msg + "\n\n" + PROMO_MESSAGE,
                reply_markup=InlineKeyboardMarkup(keyboard_liens),
                parse_mode="Markdown"
            )

            # Nettoyage
            cart.pop(user_id, None)
            cart_timestamps.pop(user_id, None)
            warned_users.discard(user_id)

            # ===== FIDÉLITÉ =====
            commandes_count[user_id] = commandes_count.get(user_id, 0) + 1
            count = commandes_count[user_id]

            if count % 5 == 0:
                lien_cadeau = retirer_lien("50-74")
                if lien_cadeau:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "🎁 *CADEAU FIDÉLITÉ !*\n\n"
                            "🏆 Félicitations ! Tu as atteint 5 commandes !\n\n"
                            "💙 Voici ton lien offert (50→74 pts) :"
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🎀 Clique ici pour ton cadeau !", url=lien_cadeau)]
                        ]),
                        parse_mode="Markdown"
                    )
            else:
                restant = 5 - (count % 5)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⭐ Fidélité : {count % 5}/5\n\n➡️ Plus que {restant} commande(s) pour ton lien offert ! 🎁"
                )

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"✅ Commande validée !\n"
                    f"👤 User : {user_id}\n"
                    f"📦 Envoyés : {len(liens_valides)}/{total_demande}\n"
                    f"❌ Manquants : {liens_manquants}"
                )
            )

        elif data.startswith("reject"):
            user_id = data.split("|")[1]

            keyboard = [
                [InlineKeyboardButton("Mauvais paiement",  callback_data=f"r1|{user_id}")],
                [InlineKeyboardButton("Montant incorrect", callback_data=f"r2|{user_id}")],
                [InlineKeyboardButton("Pas de paiement",   callback_data=f"r3|{user_id}")]
            ]

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="❌ Motif du refus :",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif data.startswith("r1") or data.startswith("r2") or data.startswith("r3"):
            code, user_id = data.split("|")
            user_id = int(user_id)

            reasons = {
                "r1": "Mauvais paiement",
                "r2": "Montant incorrect",
                "r3": "Pas de paiement"
            }

            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ Paiement refusé : {reasons[code]}\n\n"
                    "😕 Ne t'inquiète pas, tu peux recommencer !\n\n"
                    "👇 Retourne à la boutique :"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛍️ Retour à la boutique", callback_data="menu")]
                ])
            )

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(chat_id=ADMIN_ID, text="❌ Refus envoyé !")

    except Exception as e:
        logging.error(e)


# ===== COMMANDES ADMIN =====
async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OUVERT
    if update.message.from_user.id != ADMIN_ID:
        return
    BOT_OUVERT = True
    await update.message.reply_text("✅ Boutique ouverte !")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_OUVERT
    if update.message.from_user.id != ADMIN_ID:
        return
    BOT_OUVERT = False
    await update.message.reply_text("🔒 Boutique fermée !")

async def cmd_fidelite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage : /fidelite [user_id]")
        return
    uid = int(args[0])
    count = commandes_count.get(uid, 0)
    await update.message.reply_text(f"👤 User {uid} : {count} commande(s)")


# ===== MAIN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("fidelite", cmd_fidelite))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern=r"^(approve|reject|r1|r2|r3)\|"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_carts, interval=60, first=10)

    print("🤖 Bot lancé...")
    app.run_polling()
