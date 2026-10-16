import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import telebot
from telebot import types
from dotenv import load_dotenv

import config

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
# ID владельца, которому приходят уведомления о записях (узнать: команда /myid)
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
TZ = ZoneInfo(os.getenv('TIMEZONE', 'Europe/Moscow'))

bot = telebot.TeleBot(TOKEN)

WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

# Кнопки главного меню
BTN_BOOK = '✂️ Записаться'
BTN_MY = '📋 Мои записи'
BTN_PRICES = '💈 Услуги и цены'
BTN_CONTACTS = '📍 Контакты'
BTN_CANCEL = '❌ Отмена'
MENU_BUTTONS = [BTN_BOOK, BTN_MY, BTN_PRICES, BTN_CONTACTS]


# ============================================================
#  БАЗА ДАННЫХ
# ============================================================

conn = sqlite3.connect('bookings.db', check_same_thread=False)
# Замок: бот обрабатывает сообщения в нескольких потоках,
# а запись в базу должна идти строго по очереди
db_lock = threading.Lock()

with db_lock:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            client_name TEXT,
            phone TEXT,
            service TEXT,
            price INTEGER,
            duration INTEGER,
            date TEXT,          -- '2026-10-20'
            start TEXT,         -- '14:30'
            created_at TEXT
        )
    ''')
    conn.commit()

# Незавершённые записи: пользователь выбрал время, но ещё вводит имя/телефон
# {user_id: {'service': 0, 'date': '2026-10-20', 'start': '14:30', 'name': ...}}
pending = {}


# ============================================================
#  РАБОТА СО ВРЕМЕНЕМ
# ============================================================

def now_local():
    """Текущее время в часовом поясе бота."""
    return datetime.now(TZ).replace(tzinfo=None)


def to_minutes(hhmm):
    """'14:30' -> 870 (минут от начала дня)"""
    h, m = map(int, hhmm.split(':'))
    return h * 60 + m


def to_hhmm(minutes):
    """870 -> '14:30'"""
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def pretty_date(date_str):
    """'2026-10-20' -> 'Вт 20.10'"""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return f'{WEEKDAYS[d.weekday()]} {d.strftime("%d.%m")}'


def busy_intervals(date_str):
    """Занятые промежутки дня в минутах: [(начало, конец), ...]"""
    rows = conn.execute(
        'SELECT start, duration FROM bookings WHERE date = ?', (date_str,)
    ).fetchall()
    return [(to_minutes(s), to_minutes(s) + d) for s, d in rows]


def free_slots(date_str, duration):
    """Свободное время начала для услуги заданной длительности."""
    day = datetime.strptime(date_str, '%Y-%m-%d')
    if day.weekday() not in config.WORK_DAYS:
        return []

    busy = busy_intervals(date_str)
    earliest = now_local() + timedelta(minutes=config.MIN_MINUTES_BEFORE)

    slots = []
    start = config.WORK_START * 60
    while start + duration <= config.WORK_END * 60:
        end = start + duration
        slot_dt = day + timedelta(minutes=start)
        # Пересекается ли с чьей-то записью?
        overlaps = any(start < b_end and b_start < end for b_start, b_end in busy)
        if not overlaps and slot_dt >= earliest:
            slots.append(to_hhmm(start))
        start += config.SLOT_STEP
    return slots


def available_days(duration):
    """Дни, в которые есть хотя бы одно свободное время."""
    today = now_local().date()
    days = []
    for i in range(config.DAYS_AHEAD):
        date_str = (today + timedelta(days=i)).strftime('%Y-%m-%d')
        with db_lock:
            if free_slots(date_str, duration):
                days.append(date_str)
    return days


# ============================================================
#  КЛАВИАТУРЫ
# ============================================================

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_BOOK, BTN_MY)
    kb.row(BTN_PRICES, BTN_CONTACTS)
    return kb


def services_keyboard():
    kb = types.InlineKeyboardMarkup()
    for i, s in enumerate(config.SERVICES):
        kb.add(types.InlineKeyboardButton(
            f'{s["name"]} — {s["price"]} ₽', callback_data=f's:{i}'))
    return kb


def days_keyboard(service_id, days):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[types.InlineKeyboardButton(
        pretty_date(d), callback_data=f'd:{service_id}:{d.replace("-", "")}') for d in days])
    kb.add(types.InlineKeyboardButton('« К услугам', callback_data='back'))
    return kb


def times_keyboard(service_id, date_str, slots):
    kb = types.InlineKeyboardMarkup(row_width=4)
    compact = date_str.replace('-', '')
    kb.add(*[types.InlineKeyboardButton(
        t, callback_data=f't:{service_id}:{compact}:{t.replace(":", "")}') for t in slots])
    kb.add(types.InlineKeyboardButton('« К выбору дня', callback_data=f's:{service_id}'))
    return kb


def cancel_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(BTN_CANCEL)
    return kb


def phone_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton('📱 Отправить мой номер', request_contact=True))
    kb.add(BTN_CANCEL)
    return kb


# ============================================================
#  ГЛАВНОЕ МЕНЮ
# ============================================================

@bot.message_handler(commands=['start'])
def start(message):
    pending.pop(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        f'Здравствуйте! Это бот записи в {config.BUSINESS_NAME} 💈\n\n'
        'Нажмите «Записаться», чтобы выбрать услугу и удобное время.',
        reply_markup=main_menu()
    )


@bot.message_handler(commands=['myid'])
def my_id(message):
    bot.send_message(message.chat.id, f'Ваш ID: {message.from_user.id}')


@bot.message_handler(func=lambda m: m.text == BTN_PRICES)
def prices(message):
    lines = [f'• {s["name"]} — {s["price"]} ₽ ({s["duration"]} мин)' for s in config.SERVICES]
    bot.send_message(message.chat.id, 'Наши услуги:\n\n' + '\n'.join(lines))


@bot.message_handler(func=lambda m: m.text == BTN_CONTACTS)
def contacts(message):
    days = ', '.join(WEEKDAYS[d] for d in config.WORK_DAYS)
    bot.send_message(
        message.chat.id,
        f'{config.BUSINESS_NAME}\n'
        f'📍 {config.ADDRESS}\n'
        f'📞 {config.PHONE}\n'
        f'🕙 {days}, {config.WORK_START}:00–{config.WORK_END}:00'
    )


# ============================================================
#  ЗАПИСЬ: услуга -> день -> время -> имя -> телефон -> подтверждение
# ============================================================

@bot.message_handler(func=lambda m: m.text == BTN_BOOK)
def book(message):
    bot.send_message(message.chat.id, 'Выберите услугу:', reply_markup=services_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == 'back')
def back_to_services(call):
    bot.edit_message_text('Выберите услугу:', call.message.chat.id,
                          call.message.message_id, reply_markup=services_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith('s:'))
def choose_service(call):
    service_id = int(call.data.split(':')[1])
    service = config.SERVICES[service_id]
    days = available_days(service['duration'])

    if not days:
        text = 'К сожалению, на ближайшие дни свободного времени нет.'
    else:
        text = f'{service["name"]} — {service["price"]} ₽\n\nВыберите день:'
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                          reply_markup=days_keyboard(service_id, days))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith('d:'))
def choose_day(call):
    _, service_id, compact = call.data.split(':')
    service_id = int(service_id)
    date_str = f'{compact[:4]}-{compact[4:6]}-{compact[6:]}'
    service = config.SERVICES[service_id]

    with db_lock:
        slots = free_slots(date_str, service['duration'])

    if not slots:
        bot.answer_callback_query(call.id, 'На этот день время уже занято', show_alert=True)
        return

    bot.edit_message_text(
        f'{service["name"]}, {pretty_date(date_str)}\n\nВыберите время:',
        call.message.chat.id, call.message.message_id,
        reply_markup=times_keyboard(service_id, date_str, slots)
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith('t:'))
def choose_time(call):
    _, service_id, compact, hhmm = call.data.split(':')
    service_id = int(service_id)
    date_str = f'{compact[:4]}-{compact[4:6]}-{compact[6:]}'
    start_time = f'{hhmm[:2]}:{hhmm[2:]}'
    service = config.SERVICES[service_id]

    # Пока человек смотрел на кнопки, время мог занять кто-то другой
    with db_lock:
        still_free = start_time in free_slots(date_str, service['duration'])
    if not still_free:
        bot.answer_callback_query(call.id, 'Это время только что заняли, выберите другое',
                                  show_alert=True)
        return

    pending[call.from_user.id] = {'service': service_id, 'date': date_str, 'start': start_time}

    bot.edit_message_text(
        f'Вы выбрали: {service["name"]}\n{pretty_date(date_str)} в {start_time}',
        call.message.chat.id, call.message.message_id
    )
    bot.answer_callback_query(call.id)

    msg = bot.send_message(call.message.chat.id, 'Как вас зовут?', reply_markup=cancel_keyboard())
    bot.register_next_step_handler(msg, get_name)


def cancelled(message):
    """Пользователь передумал: нажал «Отмена», кнопку меню или команду."""
    text = message.text or ''
    if text == BTN_CANCEL or text in MENU_BUTTONS or text.startswith('/'):
        pending.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, 'Запись отменена.', reply_markup=main_menu())
        return True
    return False


def get_name(message):
    if cancelled(message) or message.from_user.id not in pending:
        return

    name = (message.text or '').strip()
    if not 2 <= len(name) <= 50:
        msg = bot.send_message(message.chat.id, 'Напишите, пожалуйста, имя текстом:')
        bot.register_next_step_handler(msg, get_name)
        return

    pending[message.from_user.id]['name'] = name
    msg = bot.send_message(
        message.chat.id,
        'Оставьте номер телефона: нажмите кнопку ниже или напишите его.',
        reply_markup=phone_keyboard()
    )
    bot.register_next_step_handler(msg, get_phone)


def get_phone(message):
    if message.contact is None and cancelled(message):
        return
    if message.from_user.id not in pending:
        return

    if message.contact is not None:
        phone = message.contact.phone_number
    else:
        phone = (message.text or '').strip()

    digits = re.sub(r'\D', '', phone)
    if not 10 <= len(digits) <= 12:
        msg = bot.send_message(message.chat.id, 'Не похоже на номер. Пример: +7 900 123-45-67')
        bot.register_next_step_handler(msg, get_phone)
        return

    data = pending[message.from_user.id]
    data['phone'] = phone
    service = config.SERVICES[data['service']]

    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton('✅ Подтвердить', callback_data='ok'),
           types.InlineKeyboardButton('❌ Отменить', callback_data='no'))

    # Убираем клавиатуру с кнопкой телефона и показываем итог
    bot.send_message(message.chat.id, 'Проверьте данные:', reply_markup=main_menu())
    bot.send_message(
        message.chat.id,
        f'💈 {service["name"]} — {service["price"]} ₽\n'
        f'📅 {pretty_date(data["date"])} в {data["start"]}\n'
        f'👤 {data["name"]}\n'
        f'📞 {phone}',
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data in ('ok', 'no'))
def confirm(call):
    data = pending.pop(call.from_user.id, None)
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    if call.data == 'no':
        bot.edit_message_text('Запись отменена.', chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    if data is None or 'phone' not in data:
        bot.edit_message_text('Запись устарела. Начните заново: «Записаться».', chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    service = config.SERVICES[data['service']]

    # Проверка и сохранение под одним замком — двое не займут одно время
    with db_lock:
        if data['start'] not in free_slots(data['date'], service['duration']):
            booking_id = None
        else:
            cur = conn.execute(
                'INSERT INTO bookings (user_id, client_name, phone, service, price, '
                'duration, date, start, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (call.from_user.id, data['name'], data['phone'], service['name'],
                 service['price'], service['duration'], data['date'], data['start'],
                 now_local().strftime('%Y-%m-%d %H:%M'))
            )
            conn.commit()
            booking_id = cur.lastrowid

    if booking_id is None:
        bot.edit_message_text('Увы, это время уже заняли. Выберите другое: «Записаться».',
                              chat_id, msg_id)
        bot.answer_callback_query(call.id)
        return

    bot.edit_message_text(
        f'✅ Вы записаны!\n\n'
        f'💈 {service["name"]} — {service["price"]} ₽\n'
        f'📅 {pretty_date(data["date"])} в {data["start"]}\n'
        f'📍 {config.ADDRESS}\n\n'
        'Посмотреть или отменить запись: «Мои записи».',
        chat_id, msg_id
    )
    bot.answer_callback_query(call.id, 'Готово!')

    notify_admin(
        f'🆕 Новая запись\n\n'
        f'💈 {service["name"]}\n'
        f'📅 {pretty_date(data["date"])} в {data["start"]}\n'
        f'👤 {data["name"]}\n'
        f'📞 {data["phone"]}'
    )


# ============================================================
#  МОИ ЗАПИСИ И ОТМЕНА
# ============================================================

def upcoming_for_user(user_id):
    now = now_local()
    with db_lock:
        rows = conn.execute(
            'SELECT id, service, date, start FROM bookings '
            'WHERE user_id = ? ORDER BY date, start', (user_id,)
        ).fetchall()
    # Только будущие записи
    return [r for r in rows
            if datetime.strptime(f'{r[2]} {r[3]}', '%Y-%m-%d %H:%M') > now]


@bot.message_handler(func=lambda m: m.text == BTN_MY)
def my_bookings(message):
    rows = upcoming_for_user(message.from_user.id)
    if not rows:
        bot.send_message(message.chat.id, 'У вас нет предстоящих записей.')
        return

    kb = types.InlineKeyboardMarkup()
    lines = []
    for i, (bid, service, date_str, start_time) in enumerate(rows, start=1):
        lines.append(f'{i}. {pretty_date(date_str)} в {start_time} — {service}')
        kb.add(types.InlineKeyboardButton(f'❌ Отменить запись {i}', callback_data=f'c:{bid}'))

    bot.send_message(message.chat.id, 'Ваши записи:\n\n' + '\n'.join(lines), reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith('c:'))
def cancel_booking(call):
    booking_id = int(call.data.split(':')[1])

    with db_lock:
        row = conn.execute(
            'SELECT service, date, start, client_name, phone FROM bookings '
            'WHERE id = ? AND user_id = ?', (booking_id, call.from_user.id)
        ).fetchone()
        if row:
            conn.execute('DELETE FROM bookings WHERE id = ?', (booking_id,))
            conn.commit()

    if not row:
        bot.answer_callback_query(call.id, 'Эта запись уже отменена', show_alert=True)
        return

    service, date_str, start_time, name, phone = row
    bot.answer_callback_query(call.id, 'Запись отменена')
    bot.send_message(call.message.chat.id,
                     f'Запись отменена: {pretty_date(date_str)} в {start_time} — {service}')

    # Обновляем список записей в исходном сообщении
    rest = upcoming_for_user(call.from_user.id)
    if rest:
        kb = types.InlineKeyboardMarkup()
        lines = []
        for i, (bid, s, d, t) in enumerate(rest, start=1):
            lines.append(f'{i}. {pretty_date(d)} в {t} — {s}')
            kb.add(types.InlineKeyboardButton(f'❌ Отменить запись {i}', callback_data=f'c:{bid}'))
        bot.edit_message_text('Ваши записи:\n\n' + '\n'.join(lines),
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    else:
        bot.edit_message_text('У вас нет предстоящих записей.',
                              call.message.chat.id, call.message.message_id)

    notify_admin(f'🚫 Отмена записи\n\n💈 {service}\n📅 {pretty_date(date_str)} в {start_time}\n'
                 f'👤 {name}\n📞 {phone}')


# ============================================================
#  ДЛЯ ВЛАДЕЛЬЦА
# ============================================================

def notify_admin(text):
    if not ADMIN_ID:
        return
    try:
        bot.send_message(ADMIN_ID, text)
    except Exception as e:
        print('Не удалось отправить уведомление владельцу:', e)


def is_admin(message):
    return ADMIN_ID and message.from_user.id == ADMIN_ID


def send_day_schedule(chat_id, date_str):
    with db_lock:
        rows = conn.execute(
            'SELECT start, service, client_name, phone FROM bookings '
            'WHERE date = ? ORDER BY start', (date_str,)
        ).fetchall()
    if not rows:
        bot.send_message(chat_id, f'{pretty_date(date_str)}: записей нет.')
        return
    lines = [f'{t} — {s}\n    {n}, {p}' for t, s, n, p in rows]
    bot.send_message(chat_id, f'📅 {pretty_date(date_str)}\n\n' + '\n'.join(lines))


@bot.message_handler(commands=['today'], func=is_admin)
def admin_today(message):
    send_day_schedule(message.chat.id, now_local().strftime('%Y-%m-%d'))


@bot.message_handler(commands=['tomorrow'], func=is_admin)
def admin_tomorrow(message):
    send_day_schedule(message.chat.id, (now_local() + timedelta(days=1)).strftime('%Y-%m-%d'))


@bot.message_handler(commands=['week'], func=is_admin)
def admin_week(message):
    today = now_local().date()
    for i in range(config.DAYS_AHEAD):
        day = today + timedelta(days=i)
        if day.weekday() in config.WORK_DAYS:
            send_day_schedule(message.chat.id, day.strftime('%Y-%m-%d'))


# Любое другое сообщение — подсказываем, что делать
@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, 'Воспользуйтесь кнопками меню 👇', reply_markup=main_menu())


print('Бот записи запущен...')
bot.infinity_polling()
