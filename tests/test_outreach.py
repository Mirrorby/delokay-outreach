import unittest
from datetime import datetime
from outreach import DEFAULTS, TZ, stamp, check_limits, collect_candidates, validate_templates, next_template


HEADER = ['Telegram ID', 'Username', 'Бот', 'Удалённый аккаунт', 'Статус', 'Дата первого сообщения']


class Sheet:
    def __init__(self, title, rows):
        self.title, self.rows = title, rows
    def row_values(self, n):
        return self.rows[n - 1]
    def get_all_values(self):
        return self.rows


class Book:
    def __init__(self, *sheets):
        self.sheets = sheets
    def worksheets(self):
        return self.sheets


class Safeguards(unittest.TestCase):
    def test_all_ten_templates_required(self):
        rows = [[str(i), f'Text {i}'] for i in range(1, 11)]
        self.assertEqual(len(validate_templates(rows)), 10)
        for invalid in [rows[:9], rows + [rows[0]], [[1, '']] + rows[1:]]:
            with self.assertRaises(ValueError):
                validate_templates(invalid)

    def test_rotation_204_deliveries_balanced_and_skips_dont_advance(self):
        logs, counts = [], {i: 0 for i in range(1, 11)}
        for i in range(204):
            number = next_template(logs)
            counts[number] += 1
            logs += [[str(i), '', '', 'Отправлено'], ['skip', '', '', 'Пропущен']]
        self.assertEqual(list(counts.values()), [21]*4 + [20]*6)

    def test_pending_queue_is_not_treated_as_already_sent(self):
        a = Sheet('A', [HEADER, ['1', '@one', 'Нет', 'Нет', 'Новый', '']])
        self.assertEqual(len(collect_candidates(Book(a), [['1', '', '', 'В очереди']], 'A')), 1)
        self.assertEqual(collect_candidates(Book(a), [], 'Different source'), [])

    def test_global_duplicates_and_existing_contact_in_other_group(self):
        a = Sheet('A', [HEADER, ['1', '@one', 'Нет', 'Нет', 'Новый', ''],
                       ['2', '@two', 'Нет', 'Нет', 'Новый', '']])
        b = Sheet('B', [HEADER, ['1', '@one', 'Нет', 'Нет', 'Написал', 'yesterday'],
                       ['2', '@two', 'Нет', 'Нет', 'Новый', '']])
        self.assertEqual([x[0] for x in collect_candidates(Book(a, b), [])], ['2'])

    def test_uncertain_attempt_never_retried(self):
        a = Sheet('A', [HEADER, ['1', '@one', 'Нет', 'Нет', 'Новый', '']])
        self.assertEqual(collect_candidates(Book(a), [['1', '', '', 'Обработка']]), [])

    def test_deleted_bots_and_missing_username_excluded(self):
        a = Sheet('A', [HEADER, ['1', '', 'Нет', 'Нет', 'Новый', ''],
                       ['2', '@two', 'Да', 'Нет', 'Новый', ''],
                       ['3', '@three', 'Нет', 'Да', 'Новый', '']])
        self.assertEqual(collect_candidates(Book(a), []), [])

    def test_pause_day_limit_interval_and_off_hours(self):
        cfg = {r[0]: (r[1], i) for i, r in enumerate(DEFAULTS)}
        now = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)
        self.assertIsNotNone(check_limits(cfg, [], now))
        cfg['Включено'] = ('Да', 0)
        self.assertIsNone(check_limits(cfg, [], now))
        logs = [['1', '', '', 'Обработка', stamp(now)]]
        self.assertIsNotNone(check_limits(cfg, logs, now))
        cfg['Лимит в день'] = ('1', 0)
        self.assertEqual(check_limits(cfg, logs, now), 'Дневной лимит исчерпан.')
        self.assertIsNotNone(check_limits(cfg, [], now.replace(hour=23)))


if __name__ == '__main__':
    unittest.main()
