"""One Telegram message per contact; short-lived jobs, no incoming handlers."""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Minsk')
SETTINGS = 'Рассылка — настройки'
JOURNAL = 'Рассылка — журнал'
HEADERS = ['Ключ', 'Значение', 'Описание']
LOG_HEADERS = ['Telegram ID', 'Лист', 'Строка', 'Состояние', 'Время', 'Message ID', 'Результат']
DEFAULT_MESSAGE = (
    'Привет! Я делаю Delokay — сервис, в котором можно собрать свой '
    'интернет-каталог с товарами и приёмом заказов, а управлять им из Telegram.\n\n'
    'Могу показать пример, как это выглядит для небольшого магазина. Вам актуально?'
)
DEFAULTS = [
    ['Включено', 'Нет', 'Да — автоматическая отправка; Нет — пауза'],
    ['Первое сообщение', DEFAULT_MESSAGE, 'Один текст, без ИИ и автоматических продолжений'],
    ['Лимит в день', '10', 'Учитываются и незавершённые попытки; максимум 15'],
    ['Интервал минут', '40', 'Минимальный интервал между попытками'],
    ['Начало работы', '11', 'Час по Минску, включительно'],
    ['Конец работы', '18', 'Час по Минску, не включительно'],
    ['Пауза до', '', 'Автоматическая пауза Telegram; очистить после проверки'],
]


def stamp(now=None):
    return (now or datetime.now(TZ)).strftime('%d.%m.%Y %H:%M:%S')


def parse_stamp(value):
    return datetime.strptime(value, '%d.%m.%Y %H:%M:%S').replace(tzinfo=TZ)


def summary(message):
    print(message)
    target = os.getenv('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a') as f:
            f.write(message + '\n\n')


def book_from_env():
    import gspread
    raw = os.environ['GOOGLE_SERVICE_ACCOUNT_JSON']
    client = gspread.service_account_from_dict(json.loads(raw), scopes=[
        'https://www.googleapis.com/auth/spreadsheets'])
    return client.open_by_key(os.environ['GOOGLE_SHEET_ID'])


def worksheet(book, title, header, create=False):
    import gspread
    try:
        ws = book.worksheet(title)
    except gspread.WorksheetNotFound:
        if not create:
            raise ValueError('Сначала выполните setup.') from None
        ws = book.add_worksheet(title, rows=1000, cols=len(header))
        ws.append_row(header, value_input_option='RAW')
        ws.freeze(rows=1)
    if ws.row_values(1) != header:
        raise ValueError('Заголовки служебного листа изменены; остановка.')
    return ws


def setup(book):
    ws = worksheet(book, SETTINGS, HEADERS, create=True)
    existing = {r[0] for r in ws.get_all_values()[1:] if r}
    missing = [r for r in DEFAULTS if r[0] not in existing]
    if missing:
        ws.append_rows(missing, value_input_option='RAW')
    worksheet(book, JOURNAL, LOG_HEADERS, create=True)
    summary('Настройки и журнал готовы. Существующие контакты не изменены.')


def settings(book):
    ws = worksheet(book, SETTINGS, HEADERS)
    data = {}
    for i, row in enumerate(ws.get_all_values()[1:], 2):
        if row:
            if row[0] in data:
                raise ValueError('В настройках повторяется ключ.')
            data[row[0]] = (row[1] if len(row) > 1 else '', i)
    if not {r[0] for r in DEFAULTS} <= data.keys():
        raise ValueError('В настройках отсутствуют обязательные строки.')
    return ws, data


def set_setting(ws, data, key, value):
    ws.update_cell(data[key][1], 2, value)


def collect_candidates(book, journal):
    """Scan only collector tabs; globally deduplicate IDs and prior contact dates."""
    blocked = {str(r[0]) for r in journal if r and r[0]}
    all_candidates = []
    seen = set()
    for ws in book.worksheets():
        if ws.title in {SETTINGS, JOURNAL}:
            continue
        header = ws.row_values(1)
        required = ['Telegram ID', 'Username', 'Бот', 'Удалённый аккаунт',
                    'Статус', 'Дата первого сообщения']
        if not all(x in header for x in required):
            continue
        for number, row in enumerate(ws.get_all_values()[1:], 2):
            data = dict(zip(header, row))
            uid = str(data.get('Telegram ID', '')).strip()
            if not uid.isdigit():
                continue
            if data.get('Дата первого сообщения', '').strip() or data.get('Статус', '') not in {'', 'Новый'}:
                blocked.add(uid)
            if (data.get('Бот') == 'Да' or data.get('Удалённый аккаунт') == 'Да'
                    or not data.get('Username', '').strip()):
                continue
            all_candidates.append((uid, data, ws, number, header))
    result = []
    for candidate in all_candidates:
        uid = candidate[0]
        if uid not in blocked and uid not in seen:
            result.append(candidate)
            seen.add(uid)
    return result


def check_limits(cfg, logs, now):
    if cfg['Включено'][0].strip().lower() != 'да':
        return 'Отправка на паузе.'
    message = cfg['Первое сообщение'][0].strip()
    if not message or len(message) > 3500:
        raise ValueError('Сообщение пустое или длиннее 3500 символов.')
    start, end = int(cfg['Начало работы'][0]), int(cfg['Конец работы'][0])
    if not 0 <= start < end <= 24:
        raise ValueError('Некорректное рабочее окно.')
    if not start <= now.hour < end:
        return 'Вне рабочего окна по Минску.'
    if cfg['Пауза до'][0] and now < parse_stamp(cfg['Пауза до'][0]):
        return 'Действует пауза Telegram.'
    limit, interval = int(cfg['Лимит в день'][0]), int(cfg['Интервал минут'][0])
    if not 1 <= limit <= 15 or interval < 30:
        raise ValueError('Лимит должен быть 1–15; интервал не меньше 30 минут.')
    times = [parse_stamp(r[4]) for r in logs if len(r) > 4 and r[4]]
    if sum(t.date() == now.date() for t in times) >= limit:
        return 'Дневной лимит исчерпан.'
    if times and now - max(times) < timedelta(minutes=interval):
        return 'Интервал между попытками ещё не истёк.'
    return None


def patch_source(ws, row, header, state, when=None):
    from gspread.utils import rowcol_to_a1
    edits = [{'range': rowcol_to_a1(row, header.index('Статус') + 1), 'values': [[state]]}]
    if when:
        edits.append({'range': rowcol_to_a1(row, header.index('Дата первого сообщения') + 1),
                      'values': [[when]]})
    ws.batch_update(edits, value_input_option='RAW')


async def run(mode):
    book = book_from_env()
    if mode == 'setup':
        setup(book)
        return
    sws, cfg = settings(book)
    logws = worksheet(book, JOURNAL, LOG_HEADERS)
    logs = logws.get_all_values()[1:]
    if mode in {'enable', 'pause'}:
        if mode == 'enable' and not cfg['Первое сообщение'][0].strip():
            raise ValueError('Сначала заполните текст сообщения.')
        set_setting(sws, cfg, 'Включено', 'Да' if mode == 'enable' else 'Нет')
        summary('Автоотправка включена.' if mode == 'enable' else 'Автоотправка остановлена.')
        return
    candidates = collect_candidates(book, logs)
    summary(f'Подходящих новых Telegram-контактов: {len(candidates)}. '
            'Instagram не обрабатывается. Контакты без username пропускаются.')
    if mode == 'send':
        reason = check_limits(cfg, logs, datetime.now(TZ))
        if reason:
            summary(reason)
            return
        if not candidates:
            return

    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError, PeerFloodError, RPCError
    client = TelegramClient(StringSession(os.environ['TELEGRAM_SESSION']),
                            int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'],
                            flood_sleep_threshold=0, request_retries=0, connection_retries=1,
                            retry_delay=1, timeout=20, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Telegram-сессия не авторизована; интерактивный вход отключён.')
        me = await client.get_me()
        summary('Telegram-сессия авторизована. ИИ и обработчиков входящих сообщений нет.')
        if mode == 'check':
            return
        message = cfg['Первое сообщение'][0].strip()
        if mode == 'test':
            await client.send_message('me', message, parse_mode=None, link_preview=False)
            summary('Текст отправлен только в ваше «Избранное». Контактам ничего не отправлено.')
            return
        uid, data, ws, number, header = candidates[0]
        # Claim before any Telegram operation. Unknown failures stay claimed and never retry automatically.
        logrow = len(logs) + 2
        when = stamp()
        logws.append_row([uid, ws.title, str(number), 'Обработка', when, '', ''],
                         value_input_option='RAW')
        try:
            entity = await client.get_entity(data['Username'].strip().lstrip('@'))
            if not isinstance(entity, types.User) or str(entity.id) != uid or entity.bot or entity.deleted or entity.id == me.id:
                outcome = 'Пропущен: несовпадение ID или неподходящий аккаунт'
                logws.update(range_name=f'D{logrow}:G{logrow}', values=[[outcome, when, '', outcome]])
                return
            if (await client.get_messages(entity, limit=1)):
                outcome = 'Пропущен: уже есть переписка'
                logws.update(range_name=f'D{logrow}:G{logrow}', values=[[outcome, when, '', outcome]])
                return
            # Fail closed if the sheet was edited while resolving the recipient.
            current = ws.row_values(number)
            current_data = dict(zip(header, current))
            if (current_data.get('Telegram ID') != uid
                    or current_data.get('Статус', '') not in {'', 'Новый'}
                    or current_data.get('Дата первого сообщения', '').strip()):
                logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Пропущен', when, '', 'Строка изменена']])
                return
            # Source claim prevents sends even if a separate caller bypasses this journal.
            patch_source(ws, number, header, 'Написал', when)
            result = await client(functions.messages.SendMessageRequest(
                peer=await client.get_input_entity(entity), message=message,
                random_id=secrets.randbits(63), no_webpage=True))
            msg_id = getattr(result, 'id', '')
            logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Отправлено', when, str(msg_id), '']])
            summary('Отправлено одно первое сообщение. Дальнейших автоответов не будет.')
        except (FloodWaitError, PeerFloodError) as exc:
            seconds = exc.seconds if isinstance(exc, FloodWaitError) else 86400
            set_setting(sws, cfg, 'Пауза до', stamp(datetime.now(TZ) + timedelta(seconds=seconds + 60)))
            set_setting(sws, cfg, 'Включено', 'Нет')
            logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Остановлено Telegram', when, '', type(exc).__name__]])
            summary('Telegram ограничил отправку. Автоотправка выключена; повторов и ожидания в runner нет.')
        except Exception as exc:
            # Never emit exception text: it can contain usernames, message text or credentials.
            logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Проверить вручную', when, '', type(exc).__name__]])
            summary('Попытка остановлена. Проверьте журнал; автоматического повтора этому контакту нет.')
            raise RuntimeError('Отправка не подтверждена; проверьте журнал.') from None
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'check', 'test', 'send', 'enable', 'pause'])
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(run(args.mode), timeout=150))
    except Exception as exc:
        summary('Операция завершилась ошибкой: ' + type(exc).__name__ + '. Секреты и контакты в логи не выводятся.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
