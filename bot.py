import os
import time
import threading
import logging
from datetime import datetime
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ===== LOGS =====
logging.basicConfig(level=logging.WARNING, format="%(message)s")

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6791451829"))
PAYPAL_LINK = "https://www.paypal.com/paypalme/FrankRoger149"
SUPPORT_USERNAME = "@fr26ulka"
MAX_COMMANDES_PAR_JOUR = 3

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
commandes_jour = {}
cart = {}
cart_timestamps = {}
warned_users = set()
pending_admin = {}
pending_screenshots = {}
tous_clients = set()

# ===== PARRAINAGE =====
filleuls = {}

def generer_code_parrainage(user_id):
    return f"MC{user_id}"

def get_parrain_from_code(code):
    try:
        parrain_id = int(code.replace("MC", ""))
        return parrain_id
    except Exception:
        return None

# ===== MESSAGE SELON HEURE =====
def get_greeting():
    heure = datetime.utcnow().hour + 1
    if 6 <= heure < 12:
        return "☀️ Bonne matinée !"
    elif 12 <= heure < 14:
        return "🍔 C'est l'heure du déjeuner !"
    elif 14 <= heure < 19:
        return "😎 Bonne après-midi !"
    elif 19 <= heure < 22:
        return "🌙 Bonne soirée !"
    else:
        return "🌙 Bonne nuit !"

# ===== LIMITE COMMANDES PAR JOUR =====
def get_commandes_jour(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if user_id in commandes_jour:
        date, count = commandes_jour[user_id]
        if date == today:
            return count
    return 0

def incrementer_commandes_jour(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    count = get_commandes_jour(user_id)
    commandes_jour[user_id] = (today, count + 1)

# ===== PROMO MESSAGE =====
PROMO_MESSAGE = """
🔥 Rejoins notre communauté McDo !

📲 Ouvre Telegram et clique ici :
👉 https://t.me/fr26ulkaa

⚡ Liens exclusifs en avant-première
🎁 Cadeaux & promos réservés aux membres
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
    with stock_lock:
        supabase.table("stock").delete().eq("tranche", tranche).eq("lien", lien).execute()

def remettre_stock(tranche, lien):
    with stock_lock:
        res = supabase.table("stock").select("id").eq("tranche", tranche).eq("lien", lien).execute()
        if not res.data:
            supabase.table("stock").insert({"tranche": tranche, "lien": lien}).execute()

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

def freeze_cart(user_id):
    cart_timestamps.pop(user_id, None)
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
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# ===== BOUTONS TRANCHE =====
def get_keyboard_tranche(user_id):
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

    for user_id in list(cart.keys()):
        last_seen = cart_timestamps.get(user_id)
        if last_seen is None:
            continue
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
    if update.message:
        user_id = update.message.from_user.id
        args = context.args
    else:
        user_id = update.callback_query.from_user.id
        args = []

    tous_clients.add(user_id)

    if args and args[0].startswith("MC") and user_id not in filleuls:
        parrain_id = get_parrain_from_code(args[0])
        if parrain_id and parrain_id != user_id:
            filleuls[user_id] = parrain_id

    if not BOT_OUVERT:
        msg = MESSAGE_FERME
        if update.message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.message.reply_text(msg)
        return

    greeting = get_greeting()
    est_nouveau = commandes_count.get(user_id, 0) == 0
    est_filleul = user_id in filleuls and filleuls[user_id] != "done"
    code_parrainage = generer_code_parrainage(user_id)

    if est_filleul:
        text = (
            f"{greeting}\n\n"
            f"🎁 *Bienvenue chez McDo Plans !*\n\n"
            f"Ton ami t'a offert un *lien 50→74 pts gratuit* ! 🎉\n"
            f"Il sera ajouté automatiquement à ta première commande ✅\n\n"
            f"🛍️ *Comment ça marche :*\n"
            f"1️⃣ Choisis ta tranche de points\n"
            f"2️⃣ Ajoute au panier\n"
            f"3️⃣ Paie via PayPal\n"
            f"4️⃣ Envoie ton screenshot 📸\n"
            f"5️⃣ Reçois ta capture d'écran avec le code barre 🍟\n\n"
            f"🤝 Ton code parrainage : `{code_parrainage}`\n"
            f"Partage-le et gagne *2 liens offerts* par ami !"
        )
    elif est_nouveau:
        text = (
            f"{greeting}\n\n"
            f"🍟 *Bienvenue chez McDo Plans !*\n\n"
            f"Le meilleur endroit pour obtenir tes points McDo 🔥\n\n"
            f"🛍️ *Comment ça marche :*\n"
            f"1️⃣ Choisis ta tranche de points\n"
            f"2️⃣ Ajoute au panier\n"
            f"3️⃣ Paie via PayPal\n"
            f"4️⃣ Envoie ton screenshot 📸\n"
            f"5️⃣ Reçois ta capture d'écran avec le code barre 🍟\n\n"
            f"🤝 Ton code parrainage : `{code_parrainage}`\n"
            f"Partage-le et gagne *2 liens offerts* par ami !\n\n"
            f"💡 Accumule 5 commandes et reçois un *lien gratuit* 🎁"
        )
    else:
        count = commandes_count.get(user_id, 0)
        restant = 5 - (count % 5)
        text = (
            f"{greeting}\n\n"
            f"🍟 *Content de te revoir !*\n\n"
            f"⭐ Tu as {count} commande(s) — encore {restant} pour ton lien offert ! 🎁\n\n"
            f"🤝 Ton code parrainage : `{code_parrainage}`\n"
            f"Partage-le et gagne *2 liens offerts* par ami !"
        )

    keyboard = [
        [InlineKeyboardButton("🛍️ Boutique", callback_data="menu")],
        [InlineKeyboardButton("❓ Aide", callback_data="help")]
    ]

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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
    await safe_edit(query, "🔥 *Boutique :*", InlineKeyboardMarkup(keyboard))

# ===== PANIER =====
async def refresh_cart(query, user_id):
    user_cart = get_cart(user_id)
    if not user_cart:
        await safe_edit(query, "🛒 Panier vide")
        return
    text = "🛒 *TON PANIER :*\n\n"
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
    if data.startswith(("approve", "reject", "badlink", "move", "r1", "r2", "r3", "confirm_pay", "cancel_pay")):
        return
    try:
        if data == "menu":
            await show_menu(query)

        elif data == "start":
            await start(update, context)

        elif data == "noop":
            pass

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
            if get_commandes_jour(user_id) >= MAX_COMMANDES_PAR_JOUR:
                await safe_edit(query, f"❌ Tu as atteint la limite de {MAX_COMMANDES_PAR_JOUR} commandes par jour.\n\nReviens demain ! 😊")
                return
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
            recap = "🧾 *RÉCAP DE TA COMMANDE :*\n\n"
            for t, d in user_cart.items():
                recap += f"• {tranches[t]['label']} x{d['qty']} = {d['qty'] * d['prix']}€\n"
            if len(user_cart) >= 3:
                recap += "\n🔥 -10% appliqué\n"
            recap += f"\n💰 *Total : {total}€*\n\nC'est bon pour toi ?"
            keyboard = [
                [InlineKeyboardButton("✅ Oui je confirme", callback_data="confirm_pay")],
                [InlineKeyboardButton("❌ Annuler", callback_data="cancel_pay")]
            ]
            await safe_edit(query, recap, InlineKeyboardMarkup(keyboard))

        elif data == "paid":
            touch_cart(user_id)
            user_state[user_id] = "awaiting_screenshot"
            await safe_edit(query, "📸 Envoie ton screenshot maintenant.")

        elif data == "help":
            await safe_edit(
                query,
                (
                    f"❓ *AIDE*\n\n"
                    f"1️⃣ Choisis ta tranche dans la boutique\n"
                    f"2️⃣ Ajoute au panier\n"
                    f"3️⃣ Confirme et paie via PayPal\n"
                    f"4️⃣ Envoie le screenshot de ton paiement\n"
                    f"5️⃣ Reçois ta capture d'écran avec le code barre !\n\n"
                    f"📩 Support : {SUPPORT_USERNAME}"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ Boutique", callback_data="menu")]]),
            )

        elif data.startswith("send_screenshots|"):
            # Bouton cliquable pour envoyer les screenshots
            target_id = int(data.split("|")[1])
            user_state[ADMIN_ID] = f"sending_to_{target_id}"
            await query.message.reply_text(
                f"📸 Envoie maintenant la/les capture(s) pour l'user `{target_id}`",
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(e)
        await safe_edit(query, "❌ Erreur")

# ===== CALLBACK CONFIRMATION PAIEMENT =====
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "confirm_pay":
        user_cart = get_cart(user_id)
        total = apply_discount(cart_total(user_cart), user_cart)
        touch_cart(user_id)
        keyboard = [
            [InlineKeyboardButton("💰 Via PayPal", url=PAYPAL_LINK)],
            [InlineKeyboardButton("📸 J'ai payé", callback_data="paid")],
            [InlineKeyboardButton("🔙 Panier", callback_data="cart")]
        ]
        await safe_edit(
            query,
            f"💳 *PAIEMENT*\n\n💰 Total : {total}€\n\n{get_timer_text(user_id)}",
            InlineKeyboardMarkup(keyboard)
        )

    elif data == "cancel_pay":
        await refresh_cart(query, user_id)

    elif data == "paid":
        touch_cart(user_id)
        user_state[user_id] = "awaiting_screenshot"
        await safe_edit(query, "📸 Envoie ton screenshot maintenant.")

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

    est_filleul = user_id in filleuls and filleuls[user_id] != "done"
    if est_filleul:
        lien_filleul = retirer_lien("50-74")
        if lien_filleul:
            tous_liens.append({"lien": lien_filleul, "tranche": "50-74"})
            detail += f"• 🎁 Lien offert parrainage (50→74 pts)\n"

    final_total = apply_discount(total, user_cart)
    remise = f"\n⚠️ Remise -10% (base {round(total, 2)}€)" if len(user_cart) >= 3 else ""

    pending_admin[user_id] = {
        "liens": tous_liens,
        "index": 0,
        "valides": [],
        "total": len(tous_liens),
        "detail": detail,
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

    freeze_cart(user_id)
    user_state[user_id] = "en_validation"

    await update.message.reply_text(
        "✅ *Reçu !*\n\n"
        "⏳ Ta commande est en cours de traitement...\n"
        "Tu recevras ta capture d'écran dans quelques minutes ! 🍟",
        parse_mode="Markdown"
    )

# ===== ADMIN CALLBACK =====
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
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
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Lien {pending['index']}/{pending['total']} validé... encore un peu ! ⏳"
                    )
                except Exception:
                    pass

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

        elif data.startswith("badlink"):
            user_id = int(data.split("|")[1])
            pending = pending_admin.get(user_id)
            if not pending:
                await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Commande introuvable ou expirée.")
                return

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

            supprimer_lien_stock(tranche_actuelle, lien)

            if destination == "delete":
                action_text = "🗑️ Lien supprimé définitivement."
            else:
                remettre_stock(destination, lien)
                action_text = f"✅ Lien remis dans la tranche {tranches[destination]['label']}."

            nouveau_lien = retirer_lien(tranche_actuelle)

            try:
                await query.message.delete()
            except Exception:
                pass

            if nouveau_lien:
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
                    text=(f"{action_text}\n\n🔗 Nouveau lien {num}/{total} à vérifier :\n{nouveau_lien}"),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"{action_text}\n\n⚠️ Plus de stock pour cette tranche, on passe au lien suivant."
                )
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
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"🔗 Lien {num}/{total} à vérifier :\n{prochain['lien']}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
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
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🍟 *TA COMMANDE EST PARTIELLEMENT LIVRÉE*\n\n"
                                f"✅ {len(liens_deja_valides)} lien(s) sur {pending['total']} disponibles.\n\n"
                                f"😔 Désolé, {manquants} lien(s) n'étaient plus disponibles.\n\n"
                                f"Tu seras remboursé et on t'offre un cadeau en compensation ! 🎁\n\n"
                                f"Contacte le support : {SUPPORT_USERNAME}"
                            ),
                            parse_mode="Markdown"
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"😔 Désolé, aucun lien valide disponible.\n\n"
                                f"Tu seras remboursé et on t'offre un cadeau ! 🎁\n\n"
                                f"Contacte le support : {SUPPORT_USERNAME}"
                            )
                        )
                    pending_admin.pop(user_id, None)
                    cart.pop(user_id, None)
                    cart_timestamps.pop(user_id, None)
                    user_state.pop(user_id, None)

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
            user_state.pop(user_id, None)
            pending_screenshots.pop(user_id, None)

            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=ADMIN_ID, text="❌ Refus envoyé !")

    except Exception as e:
        logging.error(e)

# ===== ENVOYER COMMANDE =====
async def envoyer_commande(context, user_id):
    pending = pending_admin.pop(user_id, None)
    if not pending:
        return

    liens_valides = pending["valides"]
    total_demande = pending["total"]
    manquants = total_demande - len(liens_valides)
    greeting = get_greeting()

    if manquants == 0:
        msg = (
            f"{greeting}\n\n"
            f"🍟 *TA COMMANDE EST PRÊTE !*\n\n"
            f"✅ {len(liens_valides)} accès McDo validés !\n\n"
            f"📸 Tu vas recevoir tes captures d'écran avec le code barre.\n"
            f"Présente-le à la borne McDo et c'est tout ! 🍗🍟"
        )
    else:
        msg = (
            f"🍟 *TA COMMANDE EST PARTIELLEMENT LIVRÉE*\n\n"
            f"✅ {len(liens_valides)} lien(s) sur {total_demande} disponibles.\n\n"
            f"😔 Désolé, {manquants} lien(s) n'étaient plus disponibles.\n\n"
            f"Tu seras remboursé et on t'offre un cadeau ! 🎁\n\n"
            f"Contacte le support : {SUPPORT_USERNAME}"
        )

    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")

    detail_str = pending.get("detail", "")
    pending_screenshots[user_id] = {
        "detail": detail_str,
        "nb_liens": len(liens_valides)
    }

    # Rappel admin avec bouton cliquable
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📸 *N'oublie pas d'envoyer les screenshots !*\n\n"
            f"👤 User : `{user_id}`\n"
            f"📦 {len(liens_valides)} capture(s) à envoyer"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Envoyer les screenshots", callback_data=f"send_screenshots|{user_id}")]
        ]),
        parse_mode="Markdown"
    )

    cart.pop(user_id, None)
    cart_timestamps.pop(user_id, None)
    warned_users.discard(user_id)
    user_state.pop(user_id, None)

    incrementer_commandes_jour(user_id)
    commandes_count[user_id] = commandes_count.get(user_id, 0) + 1
    count = commandes_count[user_id]

    # Récompense parrain
    if user_id in filleuls and filleuls[user_id] != "done":
        parrain_id = filleuls[user_id]
        filleuls[user_id] = "done"
        lien1 = retirer_lien("50-74")
        lien2 = retirer_lien("50-74")
        liens_parrain = [l for l in [lien1, lien2] if l]
        if liens_parrain:
            pending_screenshots[parrain_id] = {
                "detail": f"🎁 Cadeau parrainage x{len(liens_parrain)} (50→74 pts)",
                "nb_liens": len(liens_parrain)
            }
            await context.bot.send_message(
                chat_id=parrain_id,
                text=(
                    "🎉 *CADEAU PARRAINAGE !*\n\n"
                    "👏 Ton ami vient de faire sa première commande !\n\n"
                    f"📸 Tu vas recevoir tes *{len(liens_parrain)} capture(s)* offertes (50→74 pts) !"
                ),
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"📸 *Screenshot cadeau parrainage à envoyer !*\n\n"
                    f"👤 Parrain : `{parrain_id}`\n"
                    f"📦 {len(liens_parrain)} capture(s)"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📸 Envoyer les screenshots", callback_data=f"send_screenshots|{parrain_id}")]
                ]),
                parse_mode="Markdown"
            )

    # Fidélité
    if count % 5 == 0:
        lien_cadeau = retirer_lien("50-74")
        if lien_cadeau:
            pending_screenshots[user_id] = {"detail": "🎁 Cadeau fidélité (50→74 pts)", "nb_liens": 1}
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🎁 *CADEAU FIDÉLITÉ !*\n\n"
                    "🏆 Félicitations ! Tu as atteint 5 commandes !\n\n"
                    "📸 Tu vas recevoir ta capture d'écran offerte (50→74 pts) !"
                ),
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📸 *Screenshot cadeau fidélité à envoyer !*\n\n👤 User : `{user_id}`\n📦 1 capture",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📸 Envoyer les screenshots", callback_data=f"send_screenshots|{user_id}")]
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
        text=f"✅ Commande envoyée !\n👤 User : {user_id}\n📦 Liens : {len(liens_valides)}/{total_demande}"
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
    text = "📦 *STOCK ACTUEL :*\n\n"
    for t, info in tranches.items():
        nb = len(lire_liens(t))
        text += f"{info['label']} : {nb} liens\n"
    await update.message.reply_text(text, parse_mode="Markdown")

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
    await update.message.reply_text(f"✅ {len(nouveaux_liens)} lien(s) ajouté(s) à {tranches[tranche]['label']} !")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    total_commandes = sum(commandes_count.values())
    total_clients = len(commandes_count)
    total_parrainages = len([v for v in filleuls.values() if v == "done"])
    text = (
        f"📊 *STATS*\n\n"
        f"👥 Clients total : {len(tous_clients)}\n"
        f"🛍️ Clients ayant commandé : {total_clients}\n"
        f"📦 Commandes total : {total_commandes}\n"
        f"🤝 Parrainages réussis : {total_parrainages}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not pending_screenshots:
        await update.message.reply_text("✅ Aucune commande en attente de screenshots !")
        return
    text = "📋 *COMMANDES EN ATTENTE DE SCREENSHOTS :*\n\n"
    keyboard = []
    for i, (uid, info) in enumerate(pending_screenshots.items(), 1):
        text += (
            f"{i}. 👤 `{uid}`\n"
            f"   📦 {info['nb_liens']} capture(s)\n"
            f"   📝 {info['detail']}\n\n"
        )
        keyboard.append([InlineKeyboardButton(f"📸 Envoyer à {uid}", callback_data=f"send_screenshots|{uid}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage : /broadcast [message]")
        return
    message = " ".join(context.args)
    sent = 0
    failed = 0
    for uid in tous_clients:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 {message}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Message envoyé à {sent} clients ({failed} échecs)")

# ===== HANDLER PHOTOS ADMIN =====
async def handle_admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id == ADMIN_ID and isinstance(user_state.get(ADMIN_ID), str) and user_state[ADMIN_ID].startswith("sending_to_"):
        target_id = int(user_state[ADMIN_ID].replace("sending_to_", ""))
        photo = update.message.photo[-1].file_id
        try:
            await context.bot.send_photo(
                chat_id=target_id,
                photo=photo,
                caption="🍟 *Ton code McDo !*\n\nPrésente ce code barre à la borne McDo 🍗",
                parse_mode="Markdown"
            )
            await update.message.reply_text(f"✅ Screenshot envoyé à `{target_id}` !", parse_mode="Markdown")
            # Envoyer le promo après la capture
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=PROMO_MESSAGE,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur : {e}")
        return

    await handle_photo(update, context)

# ===== MAIN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("fidelite", cmd_fidelite))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("addstock", cmd_addstock))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    app.add_handler(CallbackQueryHandler(admin_actions, pattern=r"^(approve|reject|badlink|move|r1|r2|r3)\|"))
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^(confirm_pay|cancel_pay|paid)$"))
    app.add_handler(CallbackQueryHandler(handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle_admin_photo))

    app.job_queue.run_repeating(cleanup_carts, interval=60, first=10)

    print("🤖 Bot lancé...")
    app.run_polling()
