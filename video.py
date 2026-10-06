# bot.py
import asyncio, os, json, random, logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, TelegramError
import yt_dlp, requests as req

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = "8602685843:AAHrepWIf_Fl_eI6ec4E65tVOCU1DKWGDCc"
ADMIN_ID = 8677640533
SUPPORT_USERNAME = "dev_vexaro"
ADMIN_CHANNEL = "vexarostudio"
USERS_FILE, ADS_FILE, GAMES_FILE = "users.json", "ads.json", "games.json"

# Реклама: строго 5⭐ за час, оплата только звёздами Telegram (XTR)
STARS_PER_HOUR = 5
AD_DURATIONS = [1, 3, 7, 30]  # часы
STAR_PRICES = {h: h * STARS_PER_HOUR for h in AD_DURATIONS}  # {1:5, 3:15, 7:35, 30:150}

def load(f): return json.load(open(f)) if os.path.exists(f) else {}
def save(f, d): json.dump(d, open(f, 'w'), indent=2, ensure_ascii=False)

request = HTTPXRequest(read_timeout=60, connect_timeout=15)
app = Application.builder().token(BOT_TOKEN).request(request).build()
pending_ads, games = {}, {}

async def safe_answer(q, *args, **kwargs):
    """Отвечает на callback_query, но не роняет хендлер, если запрос уже устарел
    (BadRequest: Query is too old / query id is invalid — обычная ситуация при
    медленном интернете или долгой обработке предыдущего апдейта)."""
    try:
        await q.answer(*args, **kwargs)
    except BadRequest as e:
        if "Query is too old" in str(e) or "query id is invalid" in str(e):
            log.warning("Пропущен устаревший callback_query: %s", e)
        else:
            raise

async def safe_edit(q, *args, **kwargs):
    """edit_text, который не падает, если сообщение не изменилось или его нельзя
    отредактировать (например, оно слишком старое)."""
    try:
        await q.message.edit_text(*args, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            log.warning("edit_text не удался: %s", e)

def get_stats():
    users = load(USERS_FILE)
    now = datetime.now()
    today = sum(1 for u in users.values() if datetime.fromisoformat(u['date']).date() == now.date())
    week = sum(1 for u in users.values() if (now - datetime.fromisoformat(u['date'])).days <= 7)
    month = sum(1 for u in users.values() if (now - datetime.fromisoformat(u['date'])).days <= 30)
    return len(users), today, week, month

def get_game_stats():
    s = load(GAMES_FILE)
    return s.get('total_games',0), s.get('total_won',0), s.get('total_lost',0), s.get('biggest_win',0)

def save_game_stats(won, amount):
    s = load(GAMES_FILE)
    s['total_games'] = s.get('total_games',0) + 1
    if won:
        s['total_won'] = s.get('total_won',0) + 1
        s['biggest_win'] = max(s.get('biggest_win',0), amount)
    else: s['total_lost'] = s.get('total_lost',0) + 1
    save(GAMES_FILE, s)

# ---------- Реклама: истечение по времени ----------

def get_active_ads():
    ads = load(ADS_FILE)
    now = datetime.now()
    active = []
    for aid, ad in ads.items():
        if ad.get('status') == 'published' and ad.get('expires_at'):
            if datetime.fromisoformat(ad['expires_at']) > now:
                active.append((aid, ad))
    return active

async def check_expired_ads(ctx: ContextTypes.DEFAULT_TYPE):
    """Периодическая job: помечает рекламу истёкшей и уведомляет автора."""
    ads = load(ADS_FILE)
    now = datetime.now()
    changed = False
    for aid, ad in ads.items():
        if ad.get('status') == 'published' and ad.get('expires_at'):
            if datetime.fromisoformat(ad['expires_at']) <= now:
                ad['status'] = 'expired'
                changed = True
                try:
                    await ctx.bot.send_message(ad['user'], f"⌛ Ваша реклама ({ad['days']}ч) истекла.\nМожно заказать новую 📢")
                except TelegramError:
                    pass
    if changed:
        save(ADS_FILE, ads)

# ---------- Скачивание видео ----------

async def download_video(url, chat_id, ctx):
    if 'tiktok' in url:
        try:
            r = req.get(f"https://tikwm.com/api/?url={url}", timeout=15).json()
            if r.get('code')==0 and r.get('data',{}).get('play'):
                vd = req.get(r['data']['play'], timeout=60).content
                fn = f"downloads/t_{random.randint(1000,9999)}.mp4"
                open(fn,'wb').write(vd)
                with open(fn,'rb') as f:
                    await ctx.bot.send_video(chat_id, video=f, caption="🎬 TikTok\n\nСпасибо что используете бота! 🤝", supports_streaming=True)
                os.remove(fn); return True
        except Exception as e:
            log.warning("TikTok download failed: %s", e)
    try:
        with yt_dlp.YoutubeDL({'format':'mp4/best','outtmpl':'downloads/%(id)s.%(ext)s','quiet':True,'noplaylist':True}) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            if not os.path.exists(fn):
                fn = f"downloads/{[f for f in os.listdir('downloads') if info['id'] in f][0]}"
            with open(fn,'rb') as f:
                await ctx.bot.send_video(chat_id, video=f, caption=f"🎬 {info.get('title','')[:200]}\n\nСпасибо что используете бота! 🤝", supports_streaming=True)
            os.remove(fn); return True
    except Exception as e:
        log.warning("yt-dlp download failed: %s", e)
        await ctx.bot.send_message(chat_id, "❌ Ошибка скачивания 🤔"); return False

# ---------- Команды ----------

async def start(update, ctx):
    u = update.effective_user
    users = load(USERS_FILE)
    if str(u.id) not in users:
        users[str(u.id)] = {"name": u.full_name, "date": datetime.now().isoformat()}
        save(USERS_FILE, users)
    total, today, week, month = get_stats()
    kb = [[InlineKeyboardButton("🎬 YouTube", callback_data='yt'), InlineKeyboardButton("📱 TikTok", callback_data='tt')],
          [InlineKeyboardButton("🎮 Crash 🎲", callback_data='game'), InlineKeyboardButton("📢 Реклама", callback_data='buy_ad')],
          [InlineKeyboardButton("👤 Поддержка 🛠", url=f'https://t.me/{SUPPORT_USERNAME[1:]}'), InlineKeyboardButton("ℹ️ Помощь", callback_data='help')]]
    await update.message.reply_text(f"🤖 *Бот*\n\n📊 24ч:+{today} | Нед:+{week} | Мес:+{month} | Все:{total}\n\n📌 Ссылку — скачаю 🎬\n🎮 Crash Game 💰\n📢 Реклама", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def crash(update, ctx):
    uid = update.effective_user.id
    mult = round(random.uniform(1.2,5),1)
    ct = random.randint(3,12)
    games[uid] = {'mult':mult,'time':datetime.now(),'crash':ct}
    tg,tw,tl,bw = get_game_stats()
    kb = [[InlineKeyboardButton(f"💰 ЗАБРАТЬ x{mult} ({int(100*mult)}🪙)", callback_data='cash')],
          [InlineKeyboardButton("🔄 Новая", callback_data='game'), InlineKeyboardButton("📊 Статы", callback_data='gstats')]]
    await update.message.reply_text(f"💥 *CRASH*\n\n📈 x{mult}\n🎯 {int(100*mult)}🪙\n💣 Краш: {ct}с\n\n📊 {tg} игр | ✅{tw} | ❌{tl}\n⏱️ *ЖМИ!* 🚀", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

def ad_menu_text_and_kb():
    kb = [[InlineKeyboardButton(f"⏱ {h}ч — ⭐{STAR_PRICES[h]}", callback_data=f'ad_{h}')] for h in AD_DURATIONS]
    kb.append([InlineKeyboardButton("👤 Поддержка 🛠", url=f'https://t.me/{SUPPORT_USERNAME[1:]}')])
    text = f"📢 *Реклама в {ADMIN_CHANNEL}*\n\n💫 Тариф: {STARS_PER_HOUR}⭐ за 1 час\n💳 Оплата — Telegram Stars\n\nВыбери срок:"
    return text, InlineKeyboardMarkup(kb)

async def buy_ad(update, ctx):
    text, kb = ad_menu_text_and_kb()
    await update.message.reply_text(text, reply_markup=kb, parse_mode='Markdown')

async def my_ads(update, ctx):
    uid = update.effective_user.id
    active = [(aid, ad) for aid, ad in get_active_ads() if ad['user'] == uid]
    if not active:
        await update.message.reply_text("У вас нет активной рекламы 📢\n\nЗаказать: /buy_ad")
        return
    lines = ["📢 *Ваша активная реклама:*\n"]
    for aid, ad in active:
        left = datetime.fromisoformat(ad['expires_at']) - datetime.now()
        h, rem = divmod(int(left.total_seconds()), 3600)
        m = rem // 60
        lines.append(f"#{aid} — до окончания: {h}ч {m}м")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

# ---------- Callback-кнопки ----------

async def btn(update, ctx):
    q = update.callback_query
    await safe_answer(q)
    d = q.data
    uid = q.from_user.id

    if d in ['yt','tt']:
        await safe_edit(q, "📌 Ссылку на YouTube 🎬" if d=='yt' else "📌 Ссылку на TikTok 📱")

    elif d == 'buy_ad':
        text, kb = ad_menu_text_and_kb()
        await safe_edit(q, text, reply_markup=kb, parse_mode='Markdown')

    elif d.startswith('ad_'):
        hours = int(d.split('_')[1])
        if hours not in STAR_PRICES:
            await safe_edit(q, "❌ Неверный тариф")
            return
        pending_ads[uid] = {'days': hours, 'stars': STAR_PRICES[hours], 'status': 'invoice_sent'}
        try:
            await ctx.bot.send_invoice(
                chat_id=uid,
                title=f"Реклама на {hours}ч",
                description=f"Размещение поста в {ADMIN_CHANNEL} на {hours} час(ов)",
                payload=f"ad_{hours}_{uid}",
                provider_token="",  # для XTR всегда пусто
                currency="XTR",
                prices=[LabeledPrice(f"Реклама {hours}ч", STAR_PRICES[hours])]
            )
            await safe_edit(q, f"📢 *Заказ*\n\n⏱ {hours}ч\n⭐ {STAR_PRICES[hours]}\n\n👆 Оплатите счёт выше", parse_mode='Markdown')
        except TelegramError as e:
            log.error("send_invoice failed: %s", e)
            await ctx.bot.send_message(uid, "❌ Не удалось выставить счёт. Попробуйте позже или напишите в поддержку.")

    elif d == 'game':
        mult = round(random.uniform(1.2,5),1); ct = random.randint(3,12)
        games[uid] = {'mult':mult,'time':datetime.now(),'crash':ct}
        kb = [[InlineKeyboardButton(f"💰 ЗАБРАТЬ x{mult} ({int(100*mult)}🪙)", callback_data='cash')],
              [InlineKeyboardButton("🔄 Новая", callback_data='game')]]
        await safe_edit(q, f"💥 *CRASH*\n\n📈 x{mult}\n🎯 {int(100*mult)}🪙\n💣 Краш: {ct}с\n\n⏱️ *ЖМИ!* 🚀", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif d == 'cash':
        g = games.get(uid)
        if g:
            el = (datetime.now()-g['time']).seconds
            if el < g['crash']:
                w = int(100*g['mult']); save_game_stats(True,w)
                kb = [[InlineKeyboardButton("🔄 Ещё", callback_data='game')]]
                await safe_edit(q, f"🎉 *УСПЕХ!*\n\n⏱ {el}с\n💰 {w}🪙", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            else:
                save_game_stats(False,0)
                kb = [[InlineKeyboardButton("🔄 Снова", callback_data='game')]]
                await safe_edit(q, f"💥 *КРАШ!*\n\n😢 Не успел!", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            kb = [[InlineKeyboardButton("🎮 Играть", callback_data='game')]]
            await safe_edit(q, "❌ Нет игры", reply_markup=InlineKeyboardMarkup(kb))

    elif d == 'gstats':
        tg,tw,tl,bw = get_game_stats()
        kb = [[InlineKeyboardButton("🎮 Играть", callback_data='game')]]
        await safe_edit(q, f"📊 *Статы*\n\n🎮 {tg}\n✅ {tw}\n❌ {tl}\n🏆 {bw}🪙\n📈 {int(tw/tg*100) if tg else 0}%", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif d == 'help':
        total,today,week,month = get_stats()
        kb = [[InlineKeyboardButton("👤 Поддержка 🛠", url=f'https://t.me/{SUPPORT_USERNAME[1:]}')]]
        await safe_edit(q, f"🤖 *Помощь*\n\n📊 24ч:+{today} | Нед:+{week} | Мес:+{month} | Все:{total}\n\n📌 Ссылку — скачаю\n🎮 /crash\n📢 /buy_ad — реклама ({STARS_PER_HOUR}⭐/час)\n📋 /my_ads — моя реклама\n👤 {SUPPORT_USERNAME}\n📢 {ADMIN_CHANNEL}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# ---------- Оплата звёздами ----------

async def pre_checkout(update, ctx):
    q = update.pre_checkout_query
    payload_parts = q.invoice_payload.split('_')
    # Валидация payload — защита от левых/просроченных счетов
    if len(payload_parts) != 3 or payload_parts[0] != 'ad':
        await q.answer(ok=False, error_message="Счёт устарел, оформите заказ заново через /buy_ad")
        return
    hours = int(payload_parts[1])
    if hours not in STAR_PRICES or q.total_amount != STAR_PRICES[hours]:
        await q.answer(ok=False, error_message="Сумма счёта не совпадает с текущим тарифом, оформите заново")
        return
    await q.answer(ok=True)

async def successful_payment(update, ctx):
    uid = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    hours = int(payload.split('_')[1])
    pending_ads[uid] = {'days': hours, 'stars': STAR_PRICES[hours], 'status': 'text'}
    await update.message.reply_text(f"✅ *Оплачено!* ⭐{STAR_PRICES[hours]}\n\n⏱ Срок: {hours}ч\n\n📝 Теперь отправь текст рекламного поста", parse_mode='Markdown')

# ---------- Приём материалов рекламы ----------

async def skip_photo(update, ctx):
    uid = update.effective_user.id
    if uid in pending_ads and pending_ads[uid]['status'] == 'photo':
        await publish_ad(uid, update, ctx)
    else:
        await update.message.reply_text("Нечего пропускать 🤔")

async def publish_ad(uid, update, ctx):
    ad = pending_ads[uid]
    txt = f"#реклама\n\n{ad['text']}"
    try:
        if ad.get('media_type') == 'photo':
            await ctx.bot.send_photo(ADMIN_CHANNEL, ad['media_id'], caption=txt)
        elif ad.get('media_type') == 'video':
            await ctx.bot.send_video(ADMIN_CHANNEL, ad['media_id'], caption=txt)
        else:
            await ctx.bot.send_message(ADMIN_CHANNEL, txt)
    except TelegramError as e:
        await ctx.bot.send_message(ADMIN_ID, f"❌ Не удалось опубликовать рекламу пользователя {uid}: {e}")
        await update.message.reply_text("❌ Не удалось опубликовать рекламу. Мы уже разбираемся, напишите в поддержку.")
        return

    hours = ad['days']
    now = datetime.now()
    ads = load(ADS_FILE)
    aid = str(len(ads) + 1)
    ads[aid] = {
        'user': uid,
        'days': hours,
        'stars': ad['stars'],
        'text': ad['text'],
        'status': 'published',
        'date': now.isoformat(),
        'expires_at': (now + timedelta(hours=hours)).isoformat(),
    }
    save(ADS_FILE, ads)
    await update.message.reply_text(f"🎉 *ОПУБЛИКОВАНО!*\n\n📢 {ADMIN_CHANNEL}\n⏱ Действует {hours}ч", parse_mode='Markdown')
    del pending_ads[uid]

async def msg(update, ctx):
    if not update.message: return
    uid, cid = update.effective_user.id, update.effective_chat.id

    if uid in pending_ads and pending_ads[uid]['status'] == 'text':
        if update.message.text:
            pending_ads[uid]['text'] = update.message.text
            pending_ads[uid]['status'] = 'photo'
            await update.message.reply_text("✅ Текст принят!\n\n📸 Отправь фото/видео к посту\nИли команду /skip чтобы опубликовать без медиа", parse_mode='Markdown')
            return

    if uid in pending_ads and pending_ads[uid]['status'] == 'photo':
        if update.message.photo:
            pending_ads[uid]['media_type'] = 'photo'
            pending_ads[uid]['media_id'] = update.message.photo[-1].file_id
        elif update.message.video:
            pending_ads[uid]['media_type'] = 'video'
            pending_ads[uid]['media_id'] = update.message.video.file_id
        else:
            await update.message.reply_text("📸 Отправь фото/видео, или /skip")
            return
        await publish_ad(uid, update, ctx)
        return

    if update.message.text and 'http' in update.message.text and any(dm in update.message.text for dm in ['youtu','tiktok','instagram','likee','vk.com']):
        s = await update.message.reply_text("⏳ Скачиваю 🎬")
        ok = await download_video(update.message.text, cid, ctx)
        try:
            await s.delete()
        except TelegramError:
            pass
        if ok:
            kb = [[InlineKeyboardButton("🎮 Crash 🎲", callback_data='game'), InlineKeyboardButton("📢 Реклама", callback_data='buy_ad')]]
            await update.message.reply_text("🙏 Спасибо! 🤝", reply_markup=InlineKeyboardMarkup(kb))
    else:
        kb = [[InlineKeyboardButton("🎮 Crash 🎲", callback_data='game'), InlineKeyboardButton("📢 Реклама", callback_data='buy_ad')]]
        await update.message.reply_text("📌 Ссылку на видео 🎬", reply_markup=InlineKeyboardMarkup(kb))

# ---------- Обработчик ошибок ----------

async def error_handler(update, ctx):
    log.error("Необработанное исключение", exc_info=ctx.error)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("crash", crash))
app.add_handler(CommandHandler("buy_ad", buy_ad))
app.add_handler(CommandHandler("my_ads", my_ads))
app.add_handler(CommandHandler("skip", skip_photo))
app.add_handler(PreCheckoutQueryHandler(pre_checkout))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, msg))
app.add_handler(CallbackQueryHandler(btn))
app.add_error_handler(error_handler)

if __name__ == "__main__":
    os.makedirs("downloads", exist_ok=True)
    if app.job_queue:
        app.job_queue.run_repeating(check_expired_ads, interval=300, first=10)  # каждые 5 минут
    print("🤖 Бот запущен")
    app.run_polling()
