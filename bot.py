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
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/liens?tranche=eq.{tranche}&select=id,lien",
            headers=HEADERS
        )
        return r.json()
    except:
        return []

def count_stock(tranche):
    return len(get_stock(tranche))

def retirer_lien(tranche):
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
    try:
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/liens",
            headers=HEADERS,
            json={"tranche": tranche, "lien": lien}
        )
    except:
        pass

def ajouter_lien(tranche, lien):
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

# ===== CART =====
def get_cart(user_id):
    return cart.setdefault(user_id, {})

def cart_total(user_cart):
    return sum(d["qty"] * d["prix"] for d in user_cart.values())

def apply_discount(total, user_cart):
    return round(total * 0.9, 2) if len(user_cart) >= 3 else total

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "👋 Bienvenue !"
    keyboard = [[InlineKeyboardButton("🛍️ Boutique", callback_data="menu")]]

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ===== MENU (MODIFIÉ ICI) =====
async def show_menu(query):
    keyboard = []

    for t, info in tranches.items():
        nb = 999  # 🔥 STOCK FORCÉ

        text = f"{info['label']} | 📦 {nb} en stock | {info['prix']}€"
        data = f"add|{t}"

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

    text = "🛒 PANIER\n\n"
    total = 0
    keyboard = []

    for t, d in user_cart.items():
        qty = d["qty"]
        subtotal = qty * d["prix"]
        total += subtotal

        text += f"{tranches[t]['label']}\nx{qty} = {subtotal}€\n\n"

    final_total = apply_discount(total, user_cart)
    text += f"\n💰 TOTAL : {final_total}€"

    keyboard.append([InlineKeyboardButton("🔙 Boutique", callback_data="menu")])

    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))

# ===== CALLBACK =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "menu":
        await show_menu(query)

    elif data == "start":
        await start(update, context)

    elif data == "cart":
        await refresh_cart(query, user_id)

    elif data.startswith("add|"):
        await refresh_cart(query, user_id)

# ===== MAIN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle))

    print("🤖 Bot lancé...")
    app.run_polling()
