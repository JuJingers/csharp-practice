# -*- coding: utf-8 -*-
"""Показывает работы, ждущие проверки. Запуск: python pending.py [--all]"""
import json, os, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.abspath(__file__))
SUBS = os.path.join(ROOT, 'data', 'submissions')
REVS = os.path.join(ROOT, 'data', 'reviews')

show_all = '--all' in sys.argv
found = 0

for fn in sorted(os.listdir(SUBS)):
    if not fn.endswith('.json'):
        continue
    s = json.load(open(os.path.join(SUBS, fn), encoding='utf-8'))
    reviewed = os.path.exists(os.path.join(REVS, s['id'] + '.md'))
    if reviewed and not show_all:
        continue
    found += 1
    r = s.get('run', {})
    print('=' * 70)
    print('ID:      ' + s['id'])
    print('Задача:  гл.%s / %s — %s' % (s['chapter'], s['taskId'], s['taskTitle']))
    print('Отправл: ' + s['ts'].replace('T', ' ') + ('   [УЖЕ ПРОВЕРЕНО]' if reviewed else ''))
    if s.get('note'):
        print('Коммент: ' + s['note'])
    if s.get('stdin'):
        print('STDIN:   ' + s['stdin'].replace('\n', ' \\n '))
    print('-' * 70 + '\nКОД:')
    for i, line in enumerate(s['code'].split('\n'), 1):
        print('%3d| %s' % (i, line))
    print('-' * 70)
    if r.get('buildFailed'):
        print('РЕЗУЛЬТАТ: НЕ СКОМПИЛИРОВАЛОСЬ')
        for d in r.get('diagnostics', []):
            print('   ' + d['text'])
    else:
        print('РЕЗУЛЬТАТ: код %s' % r.get('exitCode'))
        print('ВЫВОД:\n' + (r.get('stdout') or '(пусто)'))
        for d in r.get('diagnostics', []):
            print('   предупреждение: ' + d['text'])
        if (r.get('stderr') or '').strip():
            print('ИСКЛЮЧЕНИЕ:\n' + r['stderr'].strip())
    print()

if not found:
    print('Работ, ждущих проверки, нет.' if not show_all else 'Отправок пока не было.')
else:
    print('Всего: %d' % found)
    print('Рецензия пишется в файл data/reviews/<ID>.md:')
    print('  verdict: accepted | rework | wrong')
    print('  score: 8/10')
    print('  ---')
    print('  текст рецензии в markdown')
