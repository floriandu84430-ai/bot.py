import os
import time
import threading
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ===== LOGS =====
logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ===== CONFIG =====
BOT_TOKEN = "8792949268:AAFEzRs2f0X5MFC7rYsJ72kxDY2BXjCY0Zk"
ADMIN_ID = 6791451829
PAYPAL_LINK = "https://www.paypal.me/FrankRoger149"
SUPPORT_USERNAME = "@fr26ulka"

SUPABASE_URL = "https://htlkwttvzzmkcxrxrcbu.supabase.co"
SUPABASE_KEY = "sb_publishable_IlH7wxPdNjNS1JnSovAIxw_1Fm7vFgK"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# ===== STATES =====
user_state = {}
pending_payments = {}
BOT_OUVERT = True
commandes_count = {}
cart_timestamps = {}
warned_users = set()

# ===== SUPABASE FUNCTIONS =====
def get_stock(tranche):
    """Récupère tous les liens d'une tranche"""
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/liens?tranche=eq.{tranche}&select=id,lien",
            headers=HEADERS
        )
        return r.json()
    except:
        return []

def count_stock(tranche):
    """Compte le nombre de liens dans une tranche"""
    return len(get_stock(tranche))

def retirer_lien(tranche):
    """Retire un lien de Supabase et le retourne"""
    items = get_stock(tranche)
    if not items:
        return None
    item = items[0]
    try:
        httpx.delete(
            f"{SUPABASE_URL}/rest/v1/liens?id=eq.{item['id']}",
            headers=HEADERS
        )
        return item['lien']
    except:
        return None

def remettre_stock(tranche, lien):
    """Remet un lien dans Supabase"""
    try:
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/liens",
            headers=HEADERS,
            json={"tranche": tranche, "lien": lien}
        )
    except:
        pass

def ajouter_lien(tranche, lien):
    """Ajoute un lien dans Supabase"""
    remettre_stock(tranche, lien)

# ===== TRANCHES =====
tranches = {
    "25-49": {"label": "25→49 pts", "prix": 1},
    "50-74": {"label": "50→74 pts", "prix": 2},
    "75-99": {"label": "75→99 pts", "prix": 3},
    "100-124": {"label": "100→124 pts", "prix": 4},
    "125-149": {"label": "125→149 pts", "prix": 5},
    "150-174": {"label": "150→174 pts", "prix": 6},
    "175-199": {"label": "175→199 pts", "prix": 7},
    "200-400": {"label": "200→400 pts", "prix": 8}
}

cart = {}

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

# ===== TOUCH CART =====
def touch_cart(user_id):
    cart_timestamps[user_id] = time.time()
    warned_users.discard(user_id)

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

# ===== SAFE EDIT =====
async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup)

# ===== CART =====
def get_cart(user_id):
    return cart.setdefault(user_id, {})

def cart_total(user_cart):
    return sum(d["qty"] * d["prix"] for d in user_cart.values())

def apply_discount(total, user_cart):
    return round(total * 0.9, 2) if len(user_cart) >= 3 else total

# ===== CLEANUP JOB =====
async def cleanup_carts(context):
    now = time.time()
    WARN_AT = 8 * 60
    TIMEOUT = 10 * 60

    for user_id in list(cart.keys()):
        if user_state.get(user_id) == "awaiting_screenshot":
            continue

        last_seen = cart_timestamps.get(user_id, 0)
        elapsed = now - last_seen

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
            except:
                pass

        elif elapsed > WARN_AT and user_id not in warned_users:
            warned_users.add(user_id)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Ton panier expire dans 2 minutes !\n\nFinis ta commande ou tes articles seront remis en stock."
                )
            except:
                pass

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
        nb = count_stock(t)
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
                await safe_edit(query, "❌ Stock vide")
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
            user_cart = cart.get(user_id, {})

            liens_list = [
                item
                for t, d in user_cart.items()
                for item in d["items"]
            ]

            text = (
                "🎉 Paiement validé !\n\n"
                "🙏 Merci pour ta commande 💙\n\n"
                "📦 Clique sur ton lien ci-dessous ⬇️"
                + "\n\n"
                + PROMO_MESSAGE
                + "\n\n⭐ Merci pour ta confiance ! À bientôt 👋"
            )

            keyboard_liens = [
                [InlineKeyboardButton(f"🍟 Lien McDo {i+1} — Clique ici !", url=lien)]
                for i, lien in enumerate(liens_list)
            ]
            keyboard_liens.append([InlineKeyboardButton("🏪 Retour à la boutique", callback_data="menu")])

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard_liens)
            )

            cart.pop(user_id, None)
            cart_timestamps.pop(user_id, None)
            warned_users.discard(user_id)

            try:
                await query.message.delete()
            except:
                pass

            # ===== FIDÉLITÉ =====
            commandes_count[user_id] = commandes_count.get(user_id, 0) + 1
            count = commandes_count[user_id]

            if count % 5 == 0:
                lien_cadeau = retirer_lien("50-74")
                if lien_cadeau:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "🎁 CADEAU FIDÉLITÉ !\n\n"
                            "🏆 Félicitations ! Tu as atteint 5 commandes !\n\n"
                            "💙 Voici ton lien offert (50→74 pts) :"
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🎀 Clique ici pour ton cadeau !", url=lien_cadeau)]
                        ])
                    )
            else:
                restant = 5 - (count % 5)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⭐ Fidélité : {count % 5}/5\n\n"
                        f"➡️ Plus que {restant} commande(s) pour ton lien offert ! 🎁"
                    )
                )

            await context.bot.send_message(chat_id=ADMIN_ID, text="✅ Commande validée et envoyée !")

        elif data.startswith("reject"):
            user_id = data.split("|")[1]

            keyboard = [
                [InlineKeyboardButton("Mauvais paiement", callback_data=f"r1|{user_id}")],
                [InlineKeyboardButton("Montant incorrect", callback_data=f"r2|{user_id}")],
                [InlineKeyboardButton("Pas de paiement", callback_data=f"r3|{user_id}")]
            ]

            try:
                await query.message.delete()
            except:
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
            except:
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

async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    text = "📦 STOCK ACTUEL :\n\n"
    for t, info in tranches.items():
        nb = count_stock(t)
        text += f"{info['label']} : {nb} liens\n"
    await update.message.reply_text(text)

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
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("fidelite", cmd_fidelite))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern=r"^(approve|reject|r1|r2|r3)\|"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_carts, interval=60, first=10)

    print("🤖 Bot lancé...")
    app.run_polling()
