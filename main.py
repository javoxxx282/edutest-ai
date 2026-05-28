import html
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from extractor import extract_text
from quiz_generator import generate_quiz
from parser import detect_and_parse
from states import QuizSession
from stats import (
    init_db, record_quiz, get_stats, get_top, get_all_user_ids,
    ensure_user, record_referral, get_referral_count, get_top_referrers,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".txt")
REQUIRED_CHANNEL = "@pptxtemplates_hub"
QUESTION_TIMEOUT = 30
SUPPORT_USERNAME = "muzaffarovcc"

# Comma-separated admin Telegram user IDs in the ADMIN_IDS env var, e.g. "123456,789012"
_raw_admin_ids = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admin_ids.split(",") if x.strip().isdigit()}

H = html.escape


# ---------------------------------------------------------------------------
# Subscription helpers
# ---------------------------------------------------------------------------

async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL, user_id=user_id
        )
        return member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception as e:
        logger.warning(f"Obuna tekshirishda xatolik: {e}")
        return False


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Kanalga qo'shilish", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")],
    ])


async def prompt_subscription(update: Update) -> None:
    text = (
        "⚠️ <b>Botdan foydalanish uchun kanalga obuna bo'lishingiz shart!</b>\n\n"
        f"📢 Kanal: {H(REQUIRED_CHANNEL)}\n\n"
        "1️⃣ Quyidagi tugma orqali kanalga qo'shiling\n"
        "2️⃣ So'ngra <b>Tekshirish</b> tugmasini bosing"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, parse_mode="HTML", reply_markup=subscription_keyboard()
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=subscription_keyboard()
        )


async def handle_check_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if await is_subscribed(user_id, context):
        await query.edit_message_text(
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            "Endi botdan to'liq foydalanishingiz mumkin.\n"
            "📎 PDF, DOCX yoki PPTX fayl yuboring va test boshlaylik! 🚀",
            parse_mode="HTML",
        )
    else:
        await query.answer(
            "❌ Siz hali kanalga obuna bo'lmagansiz!", show_alert=True
        )


# ---------------------------------------------------------------------------
# Timer helpers
# ---------------------------------------------------------------------------

def _timer_job_name(user_id: int) -> str:
    return f"quiz_timer_{user_id}"


def cancel_timer(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(_timer_job_name(user_id)):
        job.schedule_removal()


async def question_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id: int = job.data["chat_id"]
    user_id: int = job.data["user_id"]
    question_index: int = job.data["question_index"]

    user_data = context.application.user_data.get(user_id, {})
    session: QuizSession | None = user_data.get("quiz_session")

    if not session or session.is_finished:
        return
    if session.current_index != question_index:
        return

    q = session.current_question()
    correct_key = q["correct"]
    correct_text = q["options"].get(correct_key, "")

    session.wrong_answers.append({
        "question": q["question"],
        "chosen": "—",
        "chosen_text": "javob berilmadi",
        "correct": correct_key,
        "correct_text": correct_text,
        "explanation": q.get("explanation", ""),
    })
    session.current_index += 1

    options_text = "\n".join(
        f"{'✅' if k == correct_key else '▫️'} {H(k)}) {H(v)}"
        for k, v in q["options"].items()
    )

    timeout_msg = (
        f"⏱ <b>Vaqt tugadi!</b>\n\n"
        f"To'g'ri javob: <b>{H(correct_key)}</b>) {H(correct_text)}\n\n"
        f"{options_text}"
    )
    await context.bot.send_message(chat_id, timeout_msg, parse_mode="HTML")

    if session.is_finished:
        await _send_results(context.bot, chat_id, session)
        record_quiz(user_id, None, None, session.score, session.total)
        user_data.pop("quiz_session", None)
    else:
        await _send_question_to_chat(context, session, chat_id, user_id)


# ---------------------------------------------------------------------------
# Quiz keyboard / question helpers
# ---------------------------------------------------------------------------

def quiz_count_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10 ta savol", callback_data="quiz_count:10"),
            InlineKeyboardButton("25 ta savol", callback_data="quiz_count:25"),
        ],
        [
            InlineKeyboardButton("50 ta savol", callback_data="quiz_count:50"),
            InlineKeyboardButton("100 ta savol", callback_data="quiz_count:100"),
        ],
    ])


def get_quiz_keyboard(options: dict) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"{key}) {val}", callback_data=f"answer:{key}")]
        for key, val in options.items()
    ]
    return InlineKeyboardMarkup(buttons)


def format_question(session: QuizSession) -> str:
    q = session.current_question()
    num = session.current_index + 1
    total = session.total
    return (
        f"📝 <b>Savol {num}/{total}</b>  ⏱ {QUESTION_TIMEOUT} soniya\n\n"
        f"{H(q['question'])}"
    )


async def _send_question_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    session: QuizSession,
    chat_id: int,
    user_id: int,
) -> None:
    cancel_timer(context, user_id)
    q = session.current_question()
    text = format_question(session)
    keyboard = get_quiz_keyboard(q["options"])
    await context.bot.send_message(
        chat_id, text, parse_mode="HTML", reply_markup=keyboard
    )
    context.job_queue.run_once(
        question_timeout,
        QUESTION_TIMEOUT,
        data={
            "chat_id": chat_id,
            "user_id": user_id,
            "question_index": session.current_index,
        },
        name=_timer_job_name(user_id),
        chat_id=chat_id,
        user_id=user_id,
    )


async def send_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: QuizSession,
) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await _send_question_to_chat(context, session, chat_id, user_id)


# ---------------------------------------------------------------------------
# Results helper
# ---------------------------------------------------------------------------

async def _send_results(bot, chat_id: int, session: QuizSession) -> None:
    score = session.score
    total = session.total
    percentage = round(score / total * 100) if total > 0 else 0

    if percentage >= 90:
        grade = "🏆 A'lo!"
    elif percentage >= 75:
        grade = "👍 Yaxshi!"
    elif percentage >= 60:
        grade = "📚 Qoniqarli"
    else:
        grade = "📖 Ko'proq o'qish kerak"

    result_text = (
        f"🎉 <b>Test yakunlandi!</b>\n\n"
        f"📊 <b>Natija:</b> {score}/{total} ({percentage}%)\n"
        f"{H(grade)}\n"
    )

    if session.wrong_answers:
        result_text += f"\n❌ <b>Xato/o'tkazib yuborilgan javoblar ({len(session.wrong_answers)} ta):</b>\n\n"
        for i, w in enumerate(session.wrong_answers[:5], 1):
            result_text += (
                f"{i}. {H(w['question'])}\n"
                f"   Sizning javobingiz: {H(w['chosen'])}) {H(w['chosen_text'])}\n"
                f"   To'g'ri javob: {H(w['correct'])}) {H(w['correct_text'])}\n\n"
            )
        if len(session.wrong_answers) > 5:
            result_text += f"<i>...va yana {len(session.wrong_answers) - 5} ta xato</i>\n"

    result_text += "\n💡 Yangi fayl yuborish orqali yana test o'tkazishingiz mumkin!"
    await bot.send_message(chat_id, result_text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Stats / leaderboard handlers
# ---------------------------------------------------------------------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    row = get_stats(user_id)
    if row is None:
        await update.message.reply_text(
            "📊 Sizda hali statistika yo'q.\n"
            "Fayl yuborib, birinchi testingizni bajaring!"
        )
        return

    avg_pct = round(row["correct"] / row["questions"] * 100, 1) if row["questions"] > 0 else 0
    grade = (
        "🏆 A'lo" if avg_pct >= 90 else
        "👍 Yaxshi" if avg_pct >= 75 else
        "📚 Qoniqarli" if avg_pct >= 60 else
        "📖 Mashq kerak"
    )

    name = H(row["full_name"] or "Foydalanuvchi")
    text = (
        f"📊 <b>{name} — Statistika</b>\n\n"
        f"🧪 Testlar soni:          <b>{row['quizzes']}</b>\n"
        f"❓ Jami savollar:         <b>{row['questions']}</b>\n"
        f"✅ To'g'ri javoblar:      <b>{row['correct']}</b>\n"
        f"📈 O'rtacha natija:       <b>{avg_pct}%</b>\n"
        f"🥇 Eng yaxshi natija:     <b>{row['best_pct']}%</b>\n"
        f"📅 Oxirgi test:           <b>{row['last_quiz'] or '—'}</b>\n\n"
        f"Daraja: {grade}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    rows = get_top(10)
    if not rows:
        await update.message.reply_text(
            "🏆 Hali hech kim top ro'yxatiga kirmagan.\n"
            "Birinchi bo'ling!"
        )
        return

    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = []
    for i, row in enumerate(rows):
        display = row.get("username") or row.get("full_name") or f"User {row['user_id']}"
        avg_pct = row.get("avg_pct") or 0
        lines.append(
            f"{medals[i]} <b>{i+1}. {H(str(display))}</b> — "
            f"{avg_pct}% o'rtacha ({row['quizzes']} test)"
        )

    text = "🏆 <b>Top 10 — O'rtacha natija bo'yicha</b>\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        await update.message.reply_text("⛔ Bu buyruq faqat adminlar uchun.")
        return

    context.user_data["awaiting_broadcast"] = True
    await update.message.reply_text(
        "📢 <b>Broadcast xabari</b>\n\n"
        "Barcha foydalanuvchilarga yuboriladigan xabarni yozing.\n"
        "Bekor qilish uchun /cancel yuboring.",
        parse_mode="HTML",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("awaiting_broadcast", False):
        await update.message.reply_text("❌ Broadcast bekor qilindi.")
    else:
        await update.message.reply_text("Bekor qilinadigan jarayon yo'q.")


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the text the admin types after /broadcast."""
    if not context.user_data.get("awaiting_broadcast"):
        return  # not in broadcast mode — let other handlers deal with it

    user_id = update.effective_user.id
    if not _is_admin(user_id):
        context.user_data.pop("awaiting_broadcast", None)
        return

    context.user_data.pop("awaiting_broadcast", None)
    message_text = update.message.text or ""
    if not message_text.strip():
        await update.message.reply_text("⚠️ Xabar bo'sh. Broadcast yuborilmadi.")
        return

    all_ids = get_all_user_ids()
    sent = 0
    failed = 0
    status_msg = await update.message.reply_text(
        f"⏳ Yuborilmoqda… (0 / {len(all_ids)})"
    )

    for uid in all_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>Admin xabari:</b>\n\n{H(message_text)}",
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Broadcast yakunlandi</b>\n\n"
        f"📨 Yuborildi: <b>{sent}</b>\n"
        f"❌ Xato: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Support & profile handlers
# ---------------------------------------------------------------------------

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💬 @muzaffarovcc bilan bog'lanish",
            url=f"https://t.me/{SUPPORT_USERNAME}",
        )
    ]])
    await update.message.reply_text(
        "🛠 <b>Yordam kerakmi?</b>\n\n"
        "Quyidagi tugma orqali admin bilan to'g'ridan-to'g'ri muloqot qilishingiz mumkin.\n"
        "Muammolar, takliflar yoki savollar bo'lsa — bemalol yozing!",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    user = update.effective_user
    row = get_stats(user_id)

    name = H(user.full_name or "Foydalanuvchi")
    username_line = f"👤 Username: <b>@{H(user.username)}</b>\n" if user.username else ""

    if row is None:
        await update.message.reply_text(
            f"👤 <b>{name}</b>\n"
            f"{username_line}"
            f"🆔 ID: <code>{user_id}</code>\n\n"
            "📊 Hali hech qanday test o'tkazilmagan.\n"
            "Fayl yuboring va birinchi testingizni boshlang!",
            parse_mode="HTML",
        )
        return

    avg_pct = round(row["correct"] / row["questions"] * 100, 1) if row["questions"] > 0 else 0
    grade = (
        "🏆 A'lo"       if avg_pct >= 90 else
        "👍 Yaxshi"     if avg_pct >= 75 else
        "📚 Qoniqarli"  if avg_pct >= 60 else
        "📖 Mashq kerak"
    )
    joined = row.get("joined_at") or "—"

    text = (
        f"👤 <b>{name} — Profil</b>\n"
        f"{username_line}"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"📅 A'zo bo'lgan: <b>{joined}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧪 Testlar soni:        <b>{row['quizzes']}</b>\n"
        f"❓ Jami savollar:       <b>{row['questions']}</b>\n"
        f"✅ To'g'ri javoblar:    <b>{row['correct']}</b>\n"
        f"❌ Noto'g'ri javoblar:  <b>{row['questions'] - row['correct']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 O'rtacha natija:     <b>{avg_pct}%</b>\n"
        f"🥇 Eng yaxshi natija:   <b>{row['best_pct']}%</b>\n"
        f"📅 Oxirgi test:         <b>{row['last_quiz'] or '—'}</b>\n\n"
        f"Daraja: {grade}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id

    # Register the user so they appear in the DB even before completing a quiz
    ensure_user(user_id, user.username, user.full_name)

    # Handle referral deep-link: /start ref_<referrer_id>
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                recorded = record_referral(user_id, referrer_id)
                if recorded:
                    try:
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                f"🎉 <b>Yangi taklif!</b>\n\n"
                                f"<b>{H(user.full_name)}</b> sizning havolangiz orqali qo'shildi.\n"
                                f"Jami takliflar: <b>{get_referral_count(referrer_id)}</b>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass  # referrer may have blocked the bot
            except ValueError:
                pass

    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    await update.message.reply_text(
        "🤖 <b>Salom! Quiz Bot ga xush kelibsiz!</b>\n\n"
        "Men sizga hujjatlaringizdan avtomatik test savollari tayyorlab beraman.\n\n"
        "📎 <b>Qanday ishlaydi:</b>\n"
        "1. PDF, DOCX yoki PPTX fayl yuboring\n"
        "2. Nechta savol kerakligini tanlang\n"
        f"3. Har bir savolga <b>{QUESTION_TIMEOUT} soniya</b> ichida javob bering\n"
        "4. Natijani ko'rasiz\n\n"
        "Do'stlaringizni taklif qilish uchun /referral buyrug'ini ishlating! 🔗\n\n"
        "Hoziroq fayl yuboring! 🚀",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    await update.message.reply_text(
        "ℹ️ <b>Yordam</b>\n\n"
        "📁 <b>Qo'llab-quvvatlanadigan formatlar:</b>\n"
        "• PDF (.pdf)\n"
        "• Word (.docx)\n"
        "• PowerPoint (.pptx)\n\n"
        f"⏱ <b>Har bir savolga {QUESTION_TIMEOUT} soniya vaqt beriladi.</b>\n"
        "Vaqt tugasa, savol avtomatik o'tib ketadi.\n\n"
        "📌 <b>Buyruqlar:</b>\n"
        "/start — Botni ishga tushirish\n"
        "/help — Yordam\n"
        "/stop — Testni to'xtatish\n"
        "/profile — Mening profilim\n"
        "/stats — Mening statistikam\n"
        "/top — Top 10 natijalar\n"
        "/referral — Taklif havolam\n"
        "/toprefs — Top taklif qiluvchilar\n"
        "/support — Yordam markazi\n\n"
        "💡 Shunchaki fayl yuboring va test boshlanadi!",
        parse_mode="HTML",
    )


async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    cancel_timer(context, user_id)
    if "quiz_session" in context.user_data:
        del context.user_data["quiz_session"]
        await update.message.reply_text("⏹ Test to'xtatildi. Yangi fayl yuborishingiz mumkin.")
    else:
        await update.message.reply_text("Hozirda faol test yo'q.")


# ---------------------------------------------------------------------------
# Referral handlers
# ---------------------------------------------------------------------------

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    count = get_referral_count(user_id)

    await update.message.reply_text(
        f"🔗 <b>Sizning taklif havolangiz</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Taklif qilganlar soni: <b>{count}</b>\n\n"
        "Ushbu havolani do'stlaringizga yuboring.\n"
        "Kimdir havola orqali botga kirsa, sizga bildirishnoma keladi!",
        parse_mode="HTML",
    )


async def toprefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    rows = get_top_referrers(10)
    if not rows:
        await update.message.reply_text(
            "👥 Hali hech kim do'stlarini taklif qilmagan.\n"
            "Birinchi bo'ling — /referral buyrug'ini ishlating!"
        )
        return

    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = []
    for i, row in enumerate(rows):
        display = row.get("username") or row.get("full_name") or f"User {row['user_id']}"
        lines.append(
            f"{medals[i]} <b>{i + 1}. {H(str(display))}</b> — "
            f"<b>{row['referral_count']}</b> ta taklif"
        )

    text = "👥 <b>Top 10 — Eng ko'p taklif qilganlar</b>\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Document handler
# ---------------------------------------------------------------------------

_FORMAT_LABELS = {
    "txt":  "📋 Hash-format (TXT)",
    "docx": "📊 Jadval-format (DOCX)",
    "ai":   "🤖 AI generatsiya",
}


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context):
        await prompt_subscription(update)
        return

    doc = update.message.document
    file_name = doc.file_name or ""

    if not file_name.lower().endswith(SUPPORTED_EXTENSIONS):
        await update.message.reply_text(
            "❌ Bu fayl turi qo'llab-quvvatlanmaydi.\n"
            "Iltimos, PDF, DOCX, PPTX yoki TXT fayl yuboring."
        )
        return

    status_msg = await update.message.reply_text("⏳ Fayl yuklanmoqda...")

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())

        await status_msg.edit_text("📖 Format aniqlanmoqda...")

        raw_text = extract_text(file_bytes, file_name)
        mode, payload = detect_and_parse(file_bytes, file_name, raw_text)

        if mode == "parsed":
            questions: list[dict] = payload  # type: ignore[assignment]
            if not questions:
                await status_msg.edit_text(
                    "❌ Faylda savollar topilmadi. Formatni tekshirib, qaytadan urinib ko'ring."
                )
                return
            context.user_data["pending_questions"] = questions
            context.user_data.pop("pending_text", None)
            context.user_data["pending_filename"] = file_name

            ext = file_name.rsplit(".", 1)[-1].lower()
            fmt_label = _FORMAT_LABELS.get(ext, "📋 Tayyor format")

            await status_msg.edit_text(
                f"✅ <b>Savollar muvaffaqiyatli o'qildi!</b>\n\n"
                f"📄 Fayl: <code>{H(file_name)}</code>\n"
                f"📌 Format: {fmt_label}\n"
                f"📝 Topilgan savollar: <b>{len(questions)} ta</b>\n\n"
                f"❓ <b>Nechta savol o'ynalsin?</b>",
                parse_mode="HTML",
                reply_markup=quiz_count_keyboard(),
            )

        else:  # "ai"
            text: str = payload  # type: ignore[assignment]
            if len(text.strip()) < 100:
                await status_msg.edit_text(
                    "❌ Faylda yetarli matn topilmadi. Boshqa fayl yuborib ko'ring."
                )
                return
            context.user_data["pending_text"] = text
            context.user_data.pop("pending_questions", None)
            context.user_data["pending_filename"] = file_name

            await status_msg.edit_text(
                f"✅ <b>Fayl muvaffaqiyatli o'qildi!</b>\n\n"
                f"📄 Fayl: <code>{H(file_name)}</code>\n"
                f"📌 Format: {_FORMAT_LABELS['ai']}\n\n"
                f"❓ <b>Nechta savol tayyorlansin?</b>",
                parse_mode="HTML",
                reply_markup=quiz_count_keyboard(),
            )

    except ValueError as e:
        await status_msg.edit_text(f"❌ Xatolik: {H(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Document processing error: {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ Xatolik yuz berdi. Iltimos qaytadan urinib ko'ring."
        )


# ---------------------------------------------------------------------------
# Quiz count selection
# ---------------------------------------------------------------------------

async def handle_quiz_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    count = int(query.data.split(":")[1])
    file_name = context.user_data.get("pending_filename", "fayl")

    pre_parsed = context.user_data.get("pending_questions")
    ai_text    = context.user_data.get("pending_text")

    if not pre_parsed and not ai_text:
        await query.edit_message_text(
            "❌ Fayl topilmadi. Iltimos, qaytadan fayl yuboring."
        )
        return

    context.user_data.pop("pending_questions", None)
    context.user_data.pop("pending_text", None)
    context.user_data.pop("pending_filename", None)

    # ------------------------------------------------------------------ #
    # Branch A — pre-parsed (Format 2 or Format 3): no AI needed          #
    # ------------------------------------------------------------------ #
    if pre_parsed:
        import random as _random
        available = len(pre_parsed)
        if count > available:
            # Use all available questions, inform the user
            questions = list(pre_parsed)
            note = (
                f"\n⚠️ Faylda faqat <b>{available} ta</b> savol bor — barchasi ishlatiladi."
            )
        else:
            questions = _random.sample(pre_parsed, count)
            note = ""

        session = QuizSession(questions=questions)
        context.user_data["quiz_session"] = session

        await query.edit_message_text(
            f"✅ <b>{len(questions)} ta savol tayyor!</b>{note}\n\n"
            f"📄 Fayl: <code>{H(file_name)}</code>\n"
            f"⏱ Har bir savolga <b>{QUESTION_TIMEOUT} soniya</b> vaqt beriladi\n"
            f"📝 Test hozir boshlanadi...",
            parse_mode="HTML",
        )
        await send_question(update, context, session)
        return

    # ------------------------------------------------------------------ #
    # Branch B — AI generation (Format 1)                                 #
    # ------------------------------------------------------------------ #
    await query.edit_message_text(
        f"⏳ <b>{count} ta savol tayyorlanmoqda...</b>\n\n"
        f"📄 Fayl: <code>{H(file_name)}</code>\n"
        f"🤖 Gemini AI ishlamoqda, bir oz kuting...",
        parse_mode="HTML",
    )

    try:
        questions = generate_quiz(ai_text, count=count)

        if not questions:
            await query.edit_message_text(
                "❌ Savollar yaratishda xatolik yuz berdi. Boshqa fayl yuborib ko'ring."
            )
            return

        session = QuizSession(questions=questions)
        context.user_data["quiz_session"] = session

        await query.edit_message_text(
            f"✅ <b>{len(questions)} ta savol tayyorlandi!</b>\n\n"
            f"📄 Fayl: <code>{H(file_name)}</code>\n"
            f"⏱ Har bir savolga <b>{QUESTION_TIMEOUT} soniya</b> vaqt beriladi\n"
            f"📝 Test hozir boshlanadi...",
            parse_mode="HTML",
        )
        await send_question(update, context, session)

    except Exception as e:
        logger.error(f"Quiz generation error: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Savollar yaratishda xatolik yuz berdi. Iltimos qaytadan urinib ko'ring."
        )


# ---------------------------------------------------------------------------
# Answer handler
# ---------------------------------------------------------------------------

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("answer:"):
        return

    chosen = data.split(":")[1]

    session: QuizSession = context.user_data.get("quiz_session")
    if session is None or session.is_finished:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    cancel_timer(context, update.effective_user.id)

    q = session.current_question()
    is_correct = session.answer(chosen)

    options_text = "\n".join(
        f"{'✅' if k == q['correct'] else ('❌' if k == chosen else '▫️')} {H(k)}) {H(v)}"
        for k, v in q["options"].items()
    )

    result_icon = (
        "✅ To'g'ri!" if is_correct
        else f"❌ Noto'g'ri! To'g'ri javob: <b>{H(q['correct'])}</b>"
    )
    explanation = q.get("explanation", "")

    feedback = f"{result_icon}\n\n{options_text}"
    if explanation and not is_correct:
        feedback += f"\n\n💡 <i>{H(explanation)}</i>"

    await query.edit_message_text(
        f"📝 <b>Savol {session.current_index}/{session.total}</b>\n\n"
        f"{H(q['question'])}\n\n"
        f"{feedback}",
        parse_mode="HTML",
    )

    if session.is_finished:
        chat_id = update.effective_chat.id
        user = update.effective_user
        await _send_results(context.bot, chat_id, session)
        record_quiz(user.id, user.username, user.full_name, session.score, session.total)
        del context.user_data["quiz_session"]
    else:
        await send_question(update, context, session)


# ---------------------------------------------------------------------------
# Health check server — runs in background thread on BOT_HEALTH_PORT (8089)
# The proxy routes GET / to this port so UptimeRobot can ping the live bot.
# ---------------------------------------------------------------------------

HEALTH_PORT = int(os.environ.get("BOT_HEALTH_PORT", 8089))


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress per-request noise


def _start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(f"Health check server listening on port {HEALTH_PORT}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

    init_db()
    _start_health_server()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stop", stop_quiz))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("toprefs", toprefs_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    # Broadcast text must come before the document handler (higher priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_check_subscription, pattern=r"^check_sub$"))
    app.add_handler(CallbackQueryHandler(handle_quiz_count, pattern=r"^quiz_count:"))
    app.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^answer:"))

    logger.info("Bot ishga tushirilmoqda...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
