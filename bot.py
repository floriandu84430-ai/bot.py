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
BOT_TOKEN = os.environ.get("8792949268:AAFEzRs2f0X5MFC7rYsJ72kxDY2BXjCY0Zk")
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

# ===== PENDING ADMIN =====
# Format: {user_id: {"liens": [{"lien": ..., "tranche": ...}], "index": 0, "valides": [], "total": N}}
pending_admin = {}

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
    MAX_SCREENSHOT_WAIT = 2 * 60 * 60

    for user_id in list(cart.keys()):
        last_seen = cart_timestamps.get(user_id, 0)
        elapsed = now - last_seen

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
            liens.append(lien)  # En fin de liste pour ne pas reprendre le même lien
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

    if data.startswith(("approve", "reject", "badlink", "r1", "r2", "r3")):
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
    tous_liens = []

    for t, d in user_cart.items():
        subtotal = d["qty"] * d["prix"]
        total += subtotal
        detail += f"• {tranches[t]['label']} x{d['qty']} = {subtotal}€\n"
        for lien in d["items"]:
            tous_liens.append({"lien": lien, "tranche": t})

    final_total = apply_discount(total, user_cart)
    remise = f"\n⚠️ Remise -10% (base {round(total, 2)}€)" if len(user_cart) >= 3 else ""

    # Stocker les liens en attente de validation admin
    pending_admin[user_id] = {
        "liens": tous_liens,
        "index": 0,
        "valides": [],
        "total": len(tous_liens)
    }

    caption = (
        f"📸 Paiement reçu\n"
        f"👤 {user_id}\n\n"
        f"{detail}\n"
        f"✅ Total attendu : {final_total}€{remise}\n\n"
        f"🔗 Lien 1/{len(tous_liens)} à vérifier :\n{tous_liens[0]['lien']}"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
            InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
        ],
        [
            InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")
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
        # ===== LIEN OK =====
        if data.startswith("approve"):
            user_id = int(data.split("|")[1])
            pending = pending_admin.get(user_id)

            if not pending:
                await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Commande introuvable ou expirée.")
                return

            # Marquer le lien actuel comme valide
            lien_actuel = pending["liens"][pending["index"]]
            pending["valides"].append(lien_actuel["lien"])
            pending["index"] += 1

            # S'il reste des liens à vérifier
            if pending["index"] < len(pending["liens"]):
                prochain = pending["liens"][pending["index"]]
                num = pending["index"] + 1
                total = len(pending["liens"])

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
                        InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
                    ],
                    [
                        InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")
                    ]
                ]

                try:
                    await query.message.delete()
                except Exception:
                    pass

                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"✅ Lien {num-1}/{total} validé !\n\n"
                        f"🔗 Lien {num}/{total} à vérifier :\n{prochain['lien']}"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            else:
                # Tous les liens validés → envoyer au client
                await envoyer_commande(context, user_id)
                try:
                    await query.message.delete()
                except Exception:
                    pass

        # ===== MAUVAIS LIEN =====
        elif data.startswith("badlink"):
            user_id = int(data.split("|")[1])
            pending = pending_admin.get(user_id)

            if not pending:
                await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Commande introuvable ou expirée.")
                return

            lien_actuel = pending["liens"][pending["index"]]
            tranche = lien_actuel["tranche"]

            # Chercher un nouveau lien AVANT de remettre le mauvais en stock
            nouveau_lien = retirer_lien(tranche)

            if nouveau_lien:
                # Le mauvais lien est remis dans la bonne tranche en fin de liste
                remettre_stock(tranche, lien_actuel["lien"])
                # Remplacer dans la liste
                pending["liens"][pending["index"]] = {"lien": nouveau_lien, "tranche": tranche}
                num = pending["index"] + 1
                total = len(pending["liens"])

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
                        InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
                    ],
                    [
                        InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")
                    ]
                ]

                try:
                    await query.message.delete()
                except Exception:
                    pass

                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🔄 Nouveau lien {num}/{total} :\n{nouveau_lien}"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            else:
                # Plus de stock pour cette tranche
                try:
                    await query.message.delete()
                except Exception:
                    pass

                liens_deja_valides = pending["valides"]
                manquants = pending["total"] - len(liens_deja_valides)

                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⚠️ Stock vide pour la tranche {tranches[tranche]['label']} !\n\n"
                        f"📦 Liens valides envoyés : {len(liens_deja_valides)}/{pending['total']}\n"
                        f"❌ Manquants : {manquants}\n\nLe client a été prévenu."
                    )
                )

                if liens_deja_valides:
                    keyboard_liens = [
                        [InlineKeyboardButton(f"🍟 Lien McDo {i+1}", url=l)]
                        for i, l in enumerate(liens_deja_valides)
                    ]
                    keyboard_liens.append([InlineKeyboardButton("🏪 Retour Boutique", callback_data="menu")])

                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🍟 *TA COMMANDE EST PARTIELLEMENT LIVRÉE*\n\n"
                            f"✅ {len(liens_deja_valides)} lien(s) sur {pending['total']} sont disponibles.\n\n"
                            f"😔 Désolé, {manquants} lien(s) n'étaient plus valides et il n'y en a plus en stock.\n\n"
                            f"Tu seras remboursé uniquement pour le(s) lien(s) manquant(s) et on t'offre un cadeau en compensation ! 🎁\n\n"
                            f"Contacte le support : {SUPPORT_USERNAME}"
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard_liens),
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"😔 Désolé, c'était le dernier lien disponible pour ta tranche, il n'était pas valide et il n'y en a plus en stock.\n\n"
                            f"Tu seras remboursé uniquement pour le(s) lien(s) manquant(s) et on t'offre un cadeau en compensation ! 🎁\n\n"
                            f"Contacte le support : {SUPPORT_USERNAME}"
                        )
                    )

                pending_admin.pop(user_id, None)
                cart.pop(user_id, None)
                cart_timestamps.pop(user_id, None)

        # ===== REFUSER PAIEMENT =====
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

            # Remettre les liens en stock
            pending = pending_admin.pop(user_id, None)
            if pending:
                for item in pending["liens"]:
                    remettre_stock(item["tranche"], item["lien"])

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

            cart.pop(user_id, None)
            cart_timestamps.pop(user_id, None)

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(chat_id=ADMIN_ID, text="❌ Refus envoyé !")

    except Exception as e:
        logging.error(e)


# ===== ENVOYER COMMANDE AU CLIENT =====
async def envoyer_commande(context, user_id):
    pending = pending_admin.pop(user_id, None)
    if not pending:
        return

    liens_valides = pending["valides"]
    total_demande = pending["total"]

    msg = (
        f"🍟 *TA COMMANDE EST PRÊTE !*\n\n"
        f"Voici tes *{total_demande}* accès McDo ✅\n"
        f"Régale-toi bien et bon appétit ! 🍗🍟"
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

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"✅ Commande validée et envoyée !\n"
            f"👤 User : {user_id}\n"
            f"📦 Liens envoyés : {len(liens_valides)}/{total_demande}"
        )
    )


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
    app.add_handler(CallbackQueryHandler(admin_actions, pattern=r"^(approve|reject|badlink|r1|r2|r3)\|"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_carts, interval=60, first=10)

    print("🤖 Bot lancé...")
    app.run_polling()
