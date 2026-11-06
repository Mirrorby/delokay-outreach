"""One Telegram message per contact; short-lived jobs, no incoming handlers."""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
import secrets
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Minsk')
SETTINGS = 'Рассылка — настройки'
JOURNAL = 'Рассылка — журнал'
TEMPLATES = 'Рассылка — сообщения'
HEADERS = ['Ключ', 'Значение', 'Описание']
LOG_HEADERS = ['Telegram ID', 'Лист', 'Строка', 'Состояние', 'Время', 'Message ID', 'Результат',
               '№ варианта', 'Текст сообщения']
TEMPLATE_HEADERS = ['№', 'Первое сообщение']
QUEUED = 'В очереди'
DEFAULTS = [
    ['Включено', 'Нет', 'Да — автоматическая отправка; Нет — пауза'],
    ['Лист сообщений', TEMPLATES, 'Заполните все 10 сообщений в указанном листе'],
    ['Лист контактов', 'YARDSALE.MD Chat', 'Только участники этого Telegram-листа'],
    ['Размер теста', '204', 'Фиксированный список получателей сохранён в журнале; новые автоматически не добавляются'],
    ['Лимит в день', '10', 'Учитываются и незавершённые попытки; максимум 15'],
    ['Интервал минут', '10', 'Минимальный интервал между попытками'],
    ['Начало работы', '9', 'Час по Минску, включительно'],
    ['Конец работы', '20', 'Час по Минску, не включительно'],
    ['Пауза до', '', 'Автоматическая пауза Telegram; очистить после проверки'],
    ['ID уведомлений', '', 'Личный Telegram ID для сообщений об остановке'],
    ['Последнее уведомление', '', 'Результат отправки или причина ошибки'],
    ['Служебные данные уведомлений', '', 'Заполняет скрипт; не изменять'],
    ['Последняя остановка', '', 'Полный отчёт сохраняется даже при недоступности Telegram'],
]

RESTRICTION_ERRORS = {
    'FloodWaitError', 'FloodPremiumWaitError', 'PeerFloodError',
    'UserDeactivatedBanError', 'PhoneNumberBannedError', 'UserRestrictedError',
    'AuthKeyUnregisteredError', 'AuthKeyDuplicatedError', 'SessionRevokedError',
    'SessionExpiredError', 'UserDeactivatedError', 'FrozenMethodInvalidError',
    'SessionUnavailableError',
}


class SessionUnavailableError(RuntimeError):
    pass


def is_restriction(exc):
    return type(exc).__name__ in RESTRICTION_ERRORS


def restriction_report(exc, logs, context=None):
    context = context or {}
    now = datetime.now(TZ)
    seconds = getattr(exc, 'seconds', None)
    period = (f'{seconds} секунд; не ранее {stamp(now + timedelta(seconds=seconds))} по Минску'
              if seconds is not None else 'Telegram не сообщил срок ограничения')
    delivered = sum(len(r) > 3 and r[3] == 'Отправлено' for r in logs)
    run_id = os.getenv('GITHUB_RUN_ID')
    repo = os.getenv('GITHUB_REPOSITORY')
    link = f'https://github.com/{repo}/actions/runs/{run_id}' if repo and run_id else 'нет ссылки'
    return (
        '⛔ Отправка Delokay остановлена\n\n'
        f'Время: {stamp(now)} (Минск)\n'
        f'Ошибка Telegram: {type(exc).__name__}\n'
        f'Ответ Telegram: {str(getattr(exc, "message", type(exc).__name__))[:500]}\n'
        f'Код: {getattr(exc, "code", "не указан")}\n'
        f'Срок: {period}\n'
        f'Последний контакт: {context.get("uid", "отправка ещё не началась")}\n'
        f'Источник: {context.get("source", "—")}, строка {context.get("row", "—")}\n'
        f'Вариант сообщения: {context.get("variant", "—")}\n'
        f'Подтверждённых отправок в тесте: {delivered} / 204\n'
        f'Запуск: {link}\n\n'
        'Автоотправка выключена. Автоматического возобновления и повтора этому '
        'контакту не будет. Проверьте @SpamBot и журнал перед ручным включением.'
    )


async def notification_peer(client, sws, cfg):
    owner_id = int(cfg['ID уведомлений'][0])
    if owner_id <= 0:
        raise ValueError('Некорректный ID уведомлений.')
    cached = cfg['Служебные данные уведомлений'][0]
    if cached:
        from telethon.tl.types import InputPeerUser
        data = json.loads(cached)
        if int(data['id']) == owner_id:
            return InputPeerUser(owner_id, int(data['access_hash']))
    try:
        peer = await client.get_input_entity(owner_id)
    except ValueError:
        peer = None
    if peer is None:
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.is_user and dialog.id == owner_id:
                peer = dialog.input_entity
                break
    if getattr(peer, 'user_id', None) != owner_id:
        raise ValueError('Нет диалога с заданным личным аккаунтом.')
    set_setting(sws, cfg, 'Служебные данные уведомлений', json.dumps(
        {'id': owner_id, 'access_hash': peer.access_hash}))
    return peer


async def notify_owner(client, sws, cfg, text):
    try:
        peer = await notification_peer(client, sws, cfg)
        await asyncio.wait_for(client.send_message(peer, text, parse_mode=None, link_preview=False), timeout=20)
        set_setting(sws, cfg, 'Последнее уведомление', f'{stamp()} — отправлено')
        summary('Уведомление отправлено в настроенный личный аккаунт.')
        return True
    except Exception as exc:
        set_setting(sws, cfg, 'Последнее уведомление', f'{stamp()} — не отправлено: {type(exc).__name__}')
        summary('Не удалось доставить уведомление. Причина записана в настройках; отправка не возобновляется.')
        return False


async def stop_for_restriction(client, sws, cfg, logs, exc, logws=None, logrow=None, context=None):
    # Persist the stop before attempting an alert; alert failures must never restart outreach.
    set_setting(sws, cfg, 'Включено', 'Нет')
    seconds = getattr(exc, 'seconds', None)
    set_setting(sws, cfg, 'Пауза до',
                stamp(datetime.now(TZ) + timedelta(seconds=seconds + 60)) if seconds is not None else '')
    if logws is not None and logrow is not None:
        logws.update(range_name=f'D{logrow}:G{logrow}',
                     values=[['Остановлено Telegram', stamp(), '', type(exc).__name__]])
    summary('Telegram ограничил работу аккаунта. Автоотправка выключена до ручного включения.')
    report = restriction_report(exc, logs, context)
    set_setting(sws, cfg, 'Последняя остановка', report)
    await notify_owner(client, sws, cfg, report)


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
    logws = worksheet(book, JOURNAL, LOG_HEADERS, create=True)
    tws = worksheet(book, TEMPLATES, TEMPLATE_HEADERS, create=True)
    if len(tws.get_all_values()) == 1:
        tws.append_rows([[str(i), ''] for i in range(1, 11)], value_input_option='RAW')
    if len(logws.get_all_values()) == 1:
        _, cfg = settings(book)
        candidates = collect_candidates(book, [], cfg['Лист контактов'][0])
        count = int(cfg['Размер теста'][0])
        logws.append_rows([[uid, tab.title, str(row), QUEUED, '', '', '', '', '']
                           for uid, _, tab, row, _ in candidates[:count]], value_input_option='RAW')
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


def collect_candidates(book, journal, source_title=None):
    """Scan only collector tabs; globally deduplicate IDs and prior contact dates."""
    blocked = {str(r[0]) for r in journal if r and r[0] and (len(r) < 4 or r[3] != QUEUED)}
    all_candidates = []
    seen = set()
    for ws in book.worksheets():
        if ws.title in {SETTINGS, JOURNAL, TEMPLATES} or (source_title and ws.title != source_title):
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


def read_templates(book, title):
    ws = worksheet(book, title, TEMPLATE_HEADERS)
    return validate_templates(ws.get_all_values()[1:])


def validate_templates(rows):
    messages = {}
    for row in rows:
        if not row or not any(row):
            continue
        if len(row) < 2 or not str(row[0]).isdigit() or not row[1].strip():
            raise ValueError('Заполните тексты всех 10 сообщений.')
        number, text = int(row[0]), row[1].strip()
        if number in messages or len(text) > 3500:
            raise ValueError('Повтор номера варианта или слишком длинное сообщение.')
        messages[number] = text
    if set(messages) != set(range(1, 11)):
        raise ValueError('Нужны ровно 10 сообщений с номерами 1–10.')
    return messages


def next_template(logs):
    # Skips and failed/uncertain attempts do not advance the rotation.
    delivered = sum(len(r) > 3 and r[3] == 'Отправлено' for r in logs)
    return delivered % 10 + 1


def check_limits(cfg, logs, now):
    if cfg['Включено'][0].strip().lower() != 'да':
        return 'Отправка на паузе.'
    start, end = int(cfg['Начало работы'][0]), int(cfg['Конец работы'][0])
    if not 0 <= start < end <= 24:
        raise ValueError('Некорректное рабочее окно.')
    if not start <= now.hour < end:
        return 'Вне рабочего окна по Минску.'
    if cfg['Пауза до'][0] and now < parse_stamp(cfg['Пауза до'][0]):
        return 'Действует пауза Telegram.'
    limit, interval = int(cfg['Лимит в день'][0]), int(cfg['Интервал минут'][0])
    if not 1 <= limit <= 15 or interval < 10:
        raise ValueError('Лимит должен быть 1–15; интервал не меньше 10 минут.')
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
    if 'Последний запуск' in cfg:
        set_setting(sws, cfg, 'Последний запуск',
                    f'{stamp()} — {os.getenv("GITHUB_EVENT_NAME", "local")} / {mode}')
    logws = worksheet(book, JOURNAL, LOG_HEADERS)
    logs = logws.get_all_values()[1:]
    if mode in {'enable', 'pause'}:
        if mode == 'enable':
            read_templates(book, cfg['Лист сообщений'][0])
            if not any(len(r) > 3 and r[3] == QUEUED for r in logs):
                raise ValueError('В фиксированной очереди нет получателей.')
        set_setting(sws, cfg, 'Включено', 'Да' if mode == 'enable' else 'Нет')
        summary('Автоотправка включена.' if mode == 'enable' else 'Автоотправка остановлена.')
        return
    candidates = collect_candidates(book, logs, cfg['Лист контактов'][0])
    queued = {r[0]: i for i, r in enumerate(logs, 2) if len(r) > 3 and r[3] == QUEUED}
    if len({r[0] for r in logs if r}) != len([r for r in logs if r]):
        raise ValueError('В журнале повторяется Telegram ID.')
    candidates = [c for c in candidates if c[0] in queued]
    summary(f'Подходящих новых Telegram-контактов: {len(candidates)}. '
            'Instagram не обрабатывается. Контакты без username пропускаются.')
    if mode == 'send':
        reason = check_limits(cfg, logs, datetime.now(TZ))
        if reason:
            summary(reason)
            return
        if not candidates:
            set_setting(sws, cfg, 'Включено', 'Нет')
            summary('Тест завершён или доступная очередь исчерпана. Автоотправка выключена.')
            return
    if mode in {'send', 'test'}:
        try:
            messages = read_templates(book, cfg['Лист сообщений'][0])
        except ValueError:
            summary('Отправки нет: заполните все 10 сообщений с номерами 1–10 в отдельном листе.')
            return

    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(os.environ['TELEGRAM_SESSION']),
                            int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'],
                            flood_sleep_threshold=0, request_retries=0, connection_retries=1,
                            retry_delay=1, timeout=20, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SessionUnavailableError('Telegram-сессия больше не авторизована.')
        me = await client.get_me()
        summary('Telegram-сессия авторизована. ИИ и обработчиков входящих сообщений нет.')
        if mode == 'notify-test':
            success = await notify_owner(client, sws, cfg,
                '✅ Тест уведомлений Delokay.\n\nЭто проверка доставки с рабочего '
                'аккаунта на личный. Ограничения Telegram сейчас не обнаружены.\n'
                'При ограничении отправка выключится, а сюда придёт отчёт с '
                'причиной, временем, сроком ожидания и ссылкой на запуск.\n\n'
                'Настроенный интервал — 10 минут; окно — 09:00–20:00 по Минску; '
                'дневной лимит — 10 попыток.')
            if not success:
                raise RuntimeError('Тест уведомления не прошёл.')
            return
        if mode == 'check':
            return
        variant = next_template(logs)
        message = messages[variant]
        if mode == 'test':
            await client.send_message('me', message, parse_mode=None, link_preview=False)
            summary('Текст отправлен только в ваше «Избранное». Контактам ничего не отправлено.')
            return
        uid, data, ws, number, header = candidates[0]
        # Claim before any Telegram operation. Unknown failures stay claimed and never retry automatically.
        logrow = queued[uid]
        when = stamp()
        logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Обработка', when, '', '']],
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
            logws.update(range_name=f'H{logrow}:I{logrow}', values=[[str(variant), message]],
                         value_input_option='RAW')
            patch_source(ws, number, header, 'Написал', when)
            result = await client(functions.messages.SendMessageRequest(
                peer=await client.get_input_entity(entity), message=message,
                random_id=secrets.randbits(63), no_webpage=True))
            msg_id = getattr(result, 'id', '')
            logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Отправлено', when, str(msg_id), '']])
            summary(f'Отправлено одно первое сообщение, вариант №{variant}. Дальнейших автоответов не будет.')
        except Exception as exc:
            if is_restriction(exc):
                await stop_for_restriction(client, sws, cfg, logs, exc, logws, logrow,
                    {'uid': uid, 'source': ws.title, 'row': number, 'variant': variant})
                return
            # Never emit exception text: it can contain usernames, message text or credentials.
            logws.update(range_name=f'D{logrow}:G{logrow}', values=[['Проверить вручную', when, '', type(exc).__name__]])
            summary('Попытка остановлена. Проверьте журнал; автоматического повтора этому контакту нет.')
            raise RuntimeError('Отправка не подтверждена; проверьте журнал.') from None
    except Exception as exc:
        if is_restriction(exc):
            await stop_for_restriction(client, sws, cfg, logs, exc)
        else:
            raise
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'check', 'test', 'send', 'enable', 'pause', 'notify-test'])
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(run(args.mode), timeout=150))
    except Exception as exc:
        summary('Операция завершилась ошибкой: ' + type(exc).__name__ + '. Секреты и контакты в логи не выводятся.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
