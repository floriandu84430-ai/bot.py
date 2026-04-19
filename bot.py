import os
import time
import threading
import logging
from io import BytesIO
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from PIL import Image, ImageDraw, ImageFont

# ===== LOGS =====
logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6791451829"))
PAYPAL_LINK = "https://www.paypal.me/FrankRoger149"
SUPPORT_USERNAME = "@fr26ulka"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN manquant !")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ SUPABASE_URL ou SUPABASE_KEY manquant !")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===== TRANCHES =====
tranches = {
    "25-49":   {"label": "25→49 pts",   "prix": 1},
    "50-74":   {"label": "50→74 pts",   "prix": 2},
    "75-99":   {"label": "75→99 pts",   "prix": 3},
    "100-124": {"label": "100→124 pts", "prix": 4},
    "125-149": {"label": "125→149 pts", "prix": 5},
    "150-174": {"label": "150→174 pts", "prix": 6},
    "175-199": {"label": "175→199 pts", "prix": 7},
    "200-400": {"label": "200→400 pts", "prix": 8},
}

stock_lock = threading.Lock()

# ===== STATES =====
user_state = {}
BOT_OUVERT = True
commandes_count = {}
cart = {}
cart_timestamps = {}
warned_users = set()
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

# ===== STOCK FUNCTIONS =====
def lire_liens(tranche):
    with stock_lock:
        res = supabase.table("stock").select("id, lien").eq("tranche", tranche).order("id").execute()
        return res.data or []

def retirer_lien(tranche):
    with stock_lock:
        res = supabase.table("stock").select("id, lien").eq("tranche", tranche).order("id").limit(1).execute()
        if not res.data:
            return None
        row = res.data[0]
        supabase.table("stock").delete().eq("id", row["id"]).execute()
        return row["lien"]

def supprimer_lien_stock(tranche, lien):
    """Supprime définitivement un lien du stock d'une tranche"""
    with stock_lock:
        supabase.table("stock").delete().eq("tranche", tranche).eq("lien", lien).execute()

def remettre_stock(tranche, lien):
    """Remet un lien dans une tranche (utilisé pour annulations/remboursements)"""
    with stock_lock:
        res = supabase.table("stock").select("id").eq("tranche", tranche).eq("lien", lien).execute()
        if not res.data:
            supabase.table("stock").insert({"tranche": tranche, "lien": lien}).execute()

def deplacer_lien(ancienne_tranche, nouvelle_tranche, lien):
    """Déplace un lien d'une tranche vers une autre"""
    with stock_lock:
        # Supprimer de l'ancienne tranche
        supabase.table("stock").delete().eq("tranche", ancienne_tranche).eq("lien", lien).execute()
        # Ajouter dans la nouvelle tranche si pas déjà présent
        res = supabase.table("stock").select("id").eq("tranche", nouvelle_tranche).eq("lien", lien).execute()
        if not res.data:
            supabase.table("stock").insert({"tranche": nouvelle_tranche, "lien": lien}).execute()

def ajouter_liens(tranche, nouveaux_liens):
    with stock_lock:
        for lien in nouveaux_liens:
            res = supabase.table("stock").select("id").eq("tranche", tranche).eq("lien", lien).execute()
            if not res.data:
                supabase.table("stock").insert({"tranche": tranche, "lien": lien}).execute()

# ===== CART =====
def get_cart(user_id):
    return cart.setdefault(user_id, {})

def cart_total(user_cart):
    return sum(d["qty"] * d["prix"] for d in user_cart.values())

def apply_discount(total, user_cart):
    return round(total * 0.9, 2) if len(user_cart) >= 3 else total

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

# ===== BOUTONS TRANCHE =====
def get_keyboard_tranche(user_id):
    """Retourne le clavier de choix de tranche pour remettre un mauvais lien"""
    keyboard = [
        [
            InlineKeyboardButton("25-49 pts", callback_data=f"move|{user_id}|25-49"),
            InlineKeyboardButton("50-74 pts", callback_data=f"move|{user_id}|50-74"),
            InlineKeyboardButton("75-99 pts", callback_data=f"move|{user_id}|75-99"),
        ],
        [
            InlineKeyboardButton("100-124 pts", callback_data=f"move|{user_id}|100-124"),
            InlineKeyboardButton("125-149 pts", callback_data=f"move|{user_id}|125-149"),
            InlineKeyboardButton("150-174 pts", callback_data=f"move|{user_id}|150-174"),
        ],
        [
            InlineKeyboardButton("175-199 pts", callback_data=f"move|{user_id}|175-199"),
            InlineKeyboardButton("200-400 pts", callback_data=f"move|{user_id}|200-400"),
            InlineKeyboardButton("🗑️ Supprimer", callback_data=f"move|{user_id}|delete"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

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
        liens = lire_liens(t)
        nb = len(liens)
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
    if data.startswith(("approve", "reject", "badlink", "move", "r1", "r2", "r3")):
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
        [InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")]
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

            lien_actuel = pending["liens"][pending["index"]]
            pending["valides"].append(lien_actuel["lien"])
            pending["index"] += 1

            if pending["index"] < len(pending["liens"]):
                prochain = pending["liens"][pending["index"]]
                num = pending["index"] + 1
                total = len(pending["liens"])
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
                        InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
                    ],
                    [InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")]
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
            num = pending["index"] + 1
            total = len(pending["liens"])

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🔄 Lien {num}/{total} — Mauvais lien détecté !\n\n"
                    f"Dans quelle tranche remettre ce lien ?\n"
                    f"(ou 🗑️ Supprimer si le lien est mort)"
                ),
                reply_markup=get_keyboard_tranche(user_id)
            )

        # ===== DÉPLACER VERS TRANCHE =====
        elif data.startswith("move"):
            parts = data.split("|")
            user_id = int(parts[1])
            destination = parts[2]

            pending = pending_admin.get(user_id)
            if not pending:
                await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Commande introuvable ou expirée.")
                return

            lien_actuel = pending["liens"][pending["index"]]
            tranche_actuelle = lien_actuel["tranche"]
            lien = lien_actuel["lien"]

            # Supprimer de la tranche actuelle
            supprimer_lien_stock(tranche_actuelle, lien)

            if destination == "delete":
                action_text = "🗑️ Lien supprimé définitivement."
            else:
                # Remettre dans la bonne tranche
                remettre_stock(destination, lien)
                action_text = f"✅ Lien remis dans la tranche {tranches[destination]['label']}."

            # Prendre le prochain lien de la même tranche
            nouveau_lien = retirer_lien(tranche_actuelle)

            try:
                await query.message.delete()
            except Exception:
                pass

            if nouveau_lien:
                # On remplace le lien actuel par le nouveau
                pending["liens"][pending["index"]] = {"lien": nouveau_lien, "tranche": tranche_actuelle}
                num = pending["index"] + 1
                total = len(pending["liens"])

                keyboard = [
                    [
                        InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
                        InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
                    ],
                    [InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")]
                ]

                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"{action_text}\n\n"
                        f"🔗 Nouveau lien {num}/{total} à vérifier :\n{nouveau_lien}"
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                # Plus de stock pour ce lien → on passe au lien suivant de la commande
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"{action_text}\n\n⚠️ Plus de stock pour cette tranche, on passe au lien suivant."
                )
                pending["index"] += 1

                if pending["index"] < len(pending["liens"]):
                    # Il reste des liens à vérifier dans la commande
                    prochain = pending["liens"][pending["index"]]
                    num = pending["index"] + 1
                    total = len(pending["liens"])

                    keyboard = [
                        [
                            InlineKeyboardButton("✅ Lien OK", callback_data=f"approve|{user_id}"),
                            InlineKeyboardButton("🔄 Mauvais lien", callback_data=f"badlink|{user_id}"),
                        ],
                        [InlineKeyboardButton("❌ Refuser paiement", callback_data=f"reject|{user_id}")]
                    ]

                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"🔗 Lien {num}/{total} à vérifier :\n{prochain['lien']}"
                        ),
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    # Tous les liens ont été traités → on envoie ce qui est bon
                    liens_deja_valides = pending["valides"]
                    manquants = pending["total"] - len(liens_deja_valides)

                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"⚠️ Fin de la commande.\n"
                            f"📦 Liens valides : {len(liens_deja_valides)}/{pending['total']}\n"
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
                                f"✅ {len(liens_deja_valides)} lien(s) sur {pending['total']} disponibles.\n\n"
                                f"😔 Désolé, {manquants} lien(s) n'étaient plus disponibles en stock.\n\n"
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
                                f"😔 Désolé, aucun lien valide disponible pour ta commande.\n\n"
                                f"Tu seras remboursé et on t'offre un cadeau en compensation ! 🎁\n\n"
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

# ===== CAPTURER BARCODE =====
def capturer_barcode(lien):
    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=800,600")
        options.binary_location = "/usr/bin/chromium"

        driver = webdriver.Chrome(options=options)
        driver.get(lien)

        # Attendre que le barcode apparaisse
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "canvas, svg, img"))
        )

        import time
        time.sleep(3)

        # Prendre une capture d'écran
        screenshot = driver.get_screenshot_as_png()
        driver.quit()

        bio = BytesIO(screenshot)
        bio.seek(0)
        return bio

    except Exception as e:
        logging.error(f"Erreur capture barcode: {e}")
        return None

def generer_qr_fallback(lien, label):
    # Générer le QR code (pour scanner depuis téléphone)
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=15,
        border=6
    )
    qr.add_data(lien)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    width, height = qr_img.size
    padding = 100
    new_height = height + padding
    final_img = Image.new("RGB", (width, new_height), "white")
    final_img.paste(qr_img, (0, 0))

    draw = ImageDraw.Draw(final_img)
    try:
        font_big = ImageFont.truetype("arial.ttf", 36)
        font_small = ImageFont.truetype("arial.ttf", 24)
    except:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    text1 = "COMPTE MCDO"
    text1_bbox = draw.textbbox((0, 0), text1, font=font_big)
    text1_width = text1_bbox[2] - text1_bbox[0]
    draw.text(((width - text1_width) / 2, height + 10), text1, fill=(0, 0, 0), font=font_big)

    text2 = label
    text2_bbox = draw.textbbox((0, 0), text2, font=font_small)
    text2_width = text2_bbox[2] - text2_bbox[0]
    draw.text(((width - text2_width) / 2, height + 55), text2, fill=(220, 0, 0), font=font_small)

    bio = BytesIO()
    bio.name = "qr_mcdo.png"
    final_img.save(bio, "PNG")
    bio.seek(0)
    return bio

# ===== ENVOYER COMMANDE =====
async def envoyer_commande(context, user_id):
    pending = pending_admin.pop(user_id, None)
    if not pending:
        return

    liens_valides = pending["valides"]
    total_demande = pending["total"]
    manquants = total_demande - len(liens_valides)

    if manquants == 0:
        msg = (
            f"🍟 *TA COMMANDE EST PRÊTE !*\n\n"
            f"Voici tes *{len(liens_valides)}* accès McDo sous forme de QR Codes ✅\n"
            f"Scanne chaque QR code pour accéder à tes points !\n\n"
            f"🍗🍟 Bon appétit !"
        )
    else:
        msg = (
            f"🍟 *TA COMMANDE EST PARTIELLEMENT LIVRÉE*\n\n"
            f"✅ {len(liens_valides)} lien(s) sur {total_demande} disponibles.\n\n"
            f"😔 Désolé, {manquants} lien(s) n'étaient plus disponibles en stock.\n\n"
            f"Tu seras remboursé uniquement pour le(s) lien(s) manquant(s) et on t'offre un cadeau en compensation ! 🎁\n\n"
            f"Contacte le support : {SUPPORT_USERNAME}"
        )

    await context.bot.send_message(
        chat_id=user_id,
        text=msg,
        parse_mode="Markdown"
    )

    # Envoyer le barcode pour chaque lien
    for i, lien in enumerate(liens_valides):
        tranche = pending["liens"][i]["tranche"] if i < len(pending["liens"]) else list(tranches.keys())[0]
        label = tranches.get(tranche, {}).get("label", "McDo")

        # Essayer de capturer le barcode avec Selenium
        barcode_bio = capturer_barcode(lien)

        if barcode_bio:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=barcode_bio,
                caption="Code McDo - Presente ce code a la borne !",

📱 Présente ce code à la borne !"
            )
        else:
            # Fallback : envoyer le lien si la capture échoue
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🍟 Accès McDo {i+1}/{len(liens_valides)} — {label}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🍟 Accéder à mon code", url=lien)]
                ])
            )

    await context.bot.send_message(
        chat_id=user_id,
        text=PROMO_MESSAGE,
        parse_mode="Markdown"
    )

    cart.pop(user_id, None)
    cart_timestamps.pop(user_id, None)
    warned_users.discard(user_id)

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
            f"✅ Commande envoyée !\n"
            f"👤 User : {user_id}\n"
            f"📦 Liens : {len(liens_valides)}/{total_demande}"
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

async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    text = "📦 STOCK ACTUEL :\n\n"
    for t, info in tranches.items():
        nb = len(lire_liens(t))
        text += f"{info['label']} : {nb} liens\n"
    await update.message.reply_text(text)

async def cmd_addstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"Usage : /addstock [tranche] [lien1] [lien2] ...\n\nTranches : {', '.join(tranches.keys())}"
        )
        return
    tranche = args[0]
    if tranche not in tranches:
        await update.message.reply_text(f"❌ Tranche inconnue : {tranche}")
        return
    nouveaux_liens = args[1:]
    ajouter_liens(tranche, nouveaux_liens)
    await update.message.reply_text(
        f"✅ {len(nouveaux_liens)} lien(s) ajouté(s) à {tranches[tranche]['label']} !"
    )

# ===== MAIN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("fidelite", cmd_fidelite))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("addstock", cmd_addstock))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern=r"^(approve|reject|badlink|move|r1|r2|r3)\|"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_carts, interval=60, first=10)

    print("🤖 Bot lancé...")
    app.run_polling()
