# -*- coding: utf-8 -*-
"""Локальный тренажёр C#. Запуск: python server.py  ->  http://127.0.0.1:8777"""
import json, os, re, subprocess, sys, datetime, copy, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT     = os.path.dirname(os.path.abspath(__file__))
WEB      = os.path.join(ROOT, 'web')
CONTENT  = os.path.join(ROOT, 'content')
DATA     = os.path.join(ROOT, 'data')
SUBS     = os.path.join(DATA, 'submissions')
REVIEWS  = os.path.join(DATA, 'reviews')
RUNS     = os.path.join(ROOT, 'runs')
PROGRESS = os.path.join(DATA, 'progress.json')
ACTIVE   = os.path.join(DATA, 'active.json')
PORT     = 8777

for d in (SUBS, REVIEWS, RUNS):
    os.makedirs(d, exist_ok=True)

RUN_ENV = dict(os.environ,
               DOTNET_CLI_TELEMETRY_OPTOUT='1',
               DOTNET_NOLOGO='1',
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1')

# ---------------------------------------------------------------- content ---
def load_content():
    topics = []
    for fn in sorted(os.listdir(CONTENT)):
        if fn.endswith('.json'):
            with open(os.path.join(CONTENT, fn), encoding='utf-8') as f:
                topics.append(json.load(f))
    topics.sort(key=lambda t: t.get('chapter', 999))
    return topics

def public_content(topics):
    """Копия без правильных ответов - чтобы нельзя было подсмотреть в DevTools."""
    pub = copy.deepcopy(topics)
    for t in pub:
        for block in ('quiz', 'exam'):
            for q in t.get(block, []) or []:
                q.pop('answer', None)
                q.pop('explain', None)
    return pub

def find_question(topics, topic_id, block, qid):
    for t in topics:
        if t['id'] == topic_id:
            for q in t.get(block, []) or []:
                if q['id'] == qid:
                    return q
    return None

def find_task(topics, task_id):
    for t in topics:
        for task in t.get('tasks', []) or []:
            if task['id'] == task_id:
                return t, task
    return None, None

# --------------------------------------------------------------- progress ---
def blank_progress():
    return {'checkins': {}, 'quiz': [], 'tasksSolved': {},
            'sessions': [], 'chapterSeconds': {}, 'read': {}}

def load_progress():
    if not os.path.exists(PROGRESS):
        return blank_progress()
    try:
        with open(PROGRESS, encoding='utf-8') as f:
            p = json.load(f)
    except Exception:
        return blank_progress()
    base = blank_progress()
    base.update(p)
    return base

def save_progress(p):
    tmp = PROGRESS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)

# ------------------------------------------------------- таймер занятия ---
# Активное занятие живёт на сервере, а не во вкладке: отсчёт переживает
# перезагрузку страницы и закрытие браузера.
#   segments — уже закрытые отрезки по главам
#   topicId + since — открытый отрезок (глава, которую изучает прямо сейчас)

def load_active():
    if not os.path.exists(ACTIVE):
        return None
    try:
        with open(ACTIVE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def save_active(a):
    if a is None:
        if os.path.exists(ACTIVE):
            os.remove(ACTIVE)
        return
    tmp = ACTIVE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(a, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACTIVE)

def close_segment(a):
    """Закрывает текущий отрезок, добавляя накопленные секунды к его главе."""
    if a.get('paused') or not a.get('since'):
        return a
    secs = int(max(0, time.time() - a['since']))
    if secs:
        a.setdefault('segments', []).append({'topicId': a['topicId'], 'seconds': secs})
    a['since'] = None
    return a

def by_topic(a):
    """Сводка секунд по главам, включая незакрытый отрезок."""
    agg = {}
    for s in a.get('segments', []):
        agg[s['topicId']] = agg.get(s['topicId'], 0) + s['seconds']
    if not a.get('paused') and a.get('since'):
        cur = int(max(0, time.time() - a['since']))
        agg[a['topicId']] = agg.get(a['topicId'], 0) + cur
    return agg

def active_view(a):
    if not a:
        return None
    agg = by_topic(a)
    return {'topicId': a['topicId'], 'paused': bool(a.get('paused')),
            'startedAt': a['startedAt'], 'elapsed': sum(agg.values()),
            'byTopic': agg, 'touched': list(agg.keys())}

def record_session(seconds_by_topic, note='', read=(), started=None):
    """Записывает завершённое занятие в статистику."""
    p = load_progress()
    total = sum(seconds_by_topic.values())
    started = started or time.time()
    day = datetime.date.fromtimestamp(started).isoformat()

    p['sessions'].append({
        'date': day,
        'startedAt': datetime.datetime.fromtimestamp(started).isoformat(timespec='seconds'),
        'endedAt': datetime.datetime.now().isoformat(timespec='seconds'),
        'seconds': total, 'byTopic': seconds_by_topic,
        'note': note, 'read': list(read),
    })
    for tid, sec in seconds_by_topic.items():
        p['chapterSeconds'][tid] = p['chapterSeconds'].get(tid, 0) + sec

    # чек-ин дня: минуты складываются, если за день несколько занятий
    cur = p['checkins'].get(day, {})
    p['checkins'][day] = {
        'minutes': int(cur.get('minutes', 0)) + max(1, int(round(total / 60))),
        'note': (note or cur.get('note', '')),
        'chapter': cur.get('chapter', ''),
        'ts': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    for tid in read:
        if tid not in p['read']:
            p['read'][tid] = {'ts': datetime.datetime.now().isoformat(timespec='seconds')}
    save_progress(p)
    return p

# ------------------------------------------------------------ submissions ---
VERDICTS = {'accepted': 'Зачтено', 'rework': 'Доработать', 'wrong': 'Не зачтено'}

def parse_review(path):
    """Ревью - это .md файл: строки 'ключ: значение', затем '---', затем текст."""
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    meta, body = {}, raw
    if '---' in raw:
        head, body = raw.split('---', 1)
        for line in head.splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                meta[k.strip().lower()] = v.strip()
    verdict = meta.get('verdict', 'rework')
    return {
        'verdict': verdict,
        'verdictLabel': VERDICTS.get(verdict, verdict),
        'score': meta.get('score', ''),
        'body': body.strip(),
        'ts': datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec='seconds'),
    }

def load_submissions():
    out = []
    for fn in sorted(os.listdir(SUBS)):
        if not fn.endswith('.json'):
            continue
        with open(os.path.join(SUBS, fn), encoding='utf-8') as f:
            s = json.load(f)
        rp = os.path.join(REVIEWS, s['id'] + '.md')
        if os.path.exists(rp):
            s['review'] = parse_review(rp)
            s['status'] = 'reviewed'
        else:
            s['status'] = 'pending'
        out.append(s)
    out.sort(key=lambda s: s['ts'], reverse=True)
    return out

def sync_solved():
    """Задача считается решённой, когда ревью = accepted."""
    p = load_progress()
    changed = False
    for s in load_submissions():
        if s['status'] == 'reviewed' and s['review']['verdict'] == 'accepted':
            if s['taskId'] not in p['tasksSolved']:
                p['tasksSolved'][s['taskId']] = {'ts': s['review']['ts'], 'subId': s['id']}
                changed = True
    if changed:
        save_progress(p)
    return p

# ------------------------------------------------------------ code runner ---
# строки вида  C:\...\main.cs(9,23): warning CS8600: текст
DIAG_RE = re.compile(r'^(?:.*[\\/])?([\w.-]+\.cs)\((\d+),(\d+)\):\s*(error|warning)\s+(\w+):\s*(.*)$')

def split_diagnostics(text):
    """Отделяет сообщения компилятора от того, что напечатала сама программа."""
    diags, rest = [], []
    for line in text.splitlines():
        m = DIAG_RE.match(line.strip())
        if m:
            f, ln, col, kind, code, msg = m.groups()
            diags.append({'kind': kind, 'line': int(ln), 'col': int(col),
                          'code': code, 'msg': msg,
                          'text': 'строка %s, поз. %s: %s %s — %s' % (ln, col, kind, code, msg)})
        else:
            rest.append(line)
    return diags, '\n'.join(rest)

# В книге каждый пример заканчивается Console.ReadKey() — приём, чтобы окно консоли
# не закрылось в Visual Studio. У нас ввод перенаправлен, и ReadKey бросил бы
# InvalidOperationException, поэтому такие вызовы обезвреживаем: код из книги
# должен запускаться дословно, без правок.
READKEY_RE = re.compile(r'Console\s*\.\s*ReadKey\s*\(\s*(?:true|false)?\s*\)\s*;')

def neutralize_readkey(code):
    return READKEY_RE.sub('{ }  // Console.ReadKey() — в тренажёре не нужен', code)

def run_csharp(code, stdin_text='', slot='scratch'):
    slot = re.sub(r'[^A-Za-z0-9_.-]', '_', slot) or 'scratch'
    d = os.path.join(RUNS, slot)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'main.cs')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(neutralize_readkey(code))
    try:
        pr = subprocess.run(['dotnet', 'run', '--property:Nullable=disable', path],
                            input=stdin_text.encode('utf-8'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=40, cwd=d, env=RUN_ENV, shell=False)
        dec = lambda b: b.decode('utf-8', errors='replace')
        diags, out = split_diagnostics(dec(pr.stdout))
        err_diags, err = split_diagnostics(dec(pr.stderr))
        diags += err_diags
        failed = any(x['kind'] == 'error' for x in diags)
        if failed:
            out = ''          # программа не запускалась — это вывод сборки
        return {'ok': pr.returncode == 0, 'exitCode': pr.returncode,
                'buildFailed': failed, 'diagnostics': diags,
                'stdout': out.strip('\n'), 'stderr': err.strip()}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exitCode': -1, 'buildFailed': False, 'diagnostics': [],
                'stdout': '', 'stderr': 'Превышено время выполнения (40 с). Похоже на бесконечный цикл — проверь, меняется ли переменная в условии.'}
    except FileNotFoundError:
        return {'ok': False, 'exitCode': -1, 'buildFailed': False, 'diagnostics': [],
                'stdout': '', 'stderr': 'Не найден dotnet. Установлен ли .NET SDK?'}

# --------------------------------------------------------------- проверка ---
def norm(s):
    s = (s or '').strip().lower().replace('ё', 'е')
    return re.sub(r'\s+', ' ', s)

def check_answer(q, given):
    t = q.get('type', 'single')
    if t == 'single':
        return given == q['answer']
    if t == 'multi':
        try:
            return sorted(given) == sorted(q['answer'])
        except TypeError:
            return False
    if t == 'input':
        variants = q['answer'] if isinstance(q['answer'], list) else [q['answer']]
        g = norm(given)
        if q.get('strip_spaces'):
            g = g.replace(' ', '')
            return any(g == norm(v).replace(' ', '') for v in variants)
        return any(g == norm(v) for v in variants)
    return False

# ----------------------------------------------------------------- server ---
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml', '.ico': 'image/x-icon'}

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):
        if '/api/state' not in (self.path or ''):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get('Content-Length', 0) or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def serve_file(self, rel):
        path = os.path.join(WEB, rel)
        if not os.path.isfile(path):
            self.send_error(404, 'not found')
            return
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(os.path.splitext(path)[1], 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def state(self):
        p = sync_solved()
        return {'progress': p, 'submissions': load_submissions(),
                'today': datetime.date.today().isoformat(),
                'timer': active_view(load_active())}

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            return self.serve_file('index.html')
        if path == '/api/content':
            return self.send_json(public_content(load_content()))
        if path == '/api/state':
            return self.send_json(self.state())
        if path.startswith('/api/'):
            return self.send_error(404)
        return self.serve_file(path.lstrip('/').replace('..', ''))

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            data = self.read_json()
        except Exception as e:
            return self.send_json({'error': str(e)}, 400)

        if path == '/api/checkin':
            p = load_progress()
            day = data.get('date') or datetime.date.today().isoformat()
            cur = p['checkins'].get(day, {})
            p['checkins'][day] = {
                'minutes': int(data.get('minutes') or cur.get('minutes') or 0),
                'note': data.get('note', cur.get('note', '')),
                'chapter': data.get('chapter', cur.get('chapter', '')),
                'ts': datetime.datetime.now().isoformat(timespec='seconds'),
            }
            save_progress(p)
            return self.send_json(self.state())

        # ------------------------------------------------ таймер занятия ---
        if path == '/api/timer/start':
            save_active({'startedAt': time.time(), 'topicId': data.get('topicId', ''),
                         'since': time.time(), 'paused': False, 'segments': []})
            return self.send_json(self.state())

        if path == '/api/timer/pause':
            a = load_active()
            if a and not a.get('paused'):
                close_segment(a)
                a['paused'] = True
                save_active(a)
            return self.send_json(self.state())

        if path == '/api/timer/resume':
            a = load_active()
            if a and a.get('paused'):
                a['paused'] = False
                a['since'] = time.time()
                save_active(a)
            return self.send_json(self.state())

        if path == '/api/timer/chapter':
            a = load_active()
            if a:
                close_segment(a)
                a['topicId'] = data.get('topicId', a['topicId'])
                if not a.get('paused'):
                    a['since'] = time.time()
                save_active(a)
            return self.send_json(self.state())

        if path == '/api/timer/finish':
            a = load_active()
            if not a:
                return self.send_json(self.state())
            close_segment(a)
            agg = by_topic(a)
            agg = {k: v for k, v in agg.items() if v > 0}
            if agg:
                record_session(agg, data.get('note', ''), data.get('read', []),
                               a.get('startedAt'))
            save_active(None)
            return self.send_json(self.state())

        if path == '/api/timer/cancel':
            save_active(None)
            return self.send_json(self.state())

        if path == '/api/session/manual':
            mins = max(1, int(data.get('minutes') or 0))
            tid = data.get('topicId', '')
            started = time.time() - mins * 60
            record_session({tid: mins * 60}, data.get('note', ''),
                           data.get('read', []), started)
            return self.send_json(self.state())

        if path == '/api/read/toggle':
            p = load_progress()
            tid = data.get('topicId', '')
            if tid in p['read']:
                p['read'].pop(tid)
            else:
                p['read'][tid] = {'ts': datetime.datetime.now().isoformat(timespec='seconds')}
            save_progress(p)
            return self.send_json(self.state())

        if path == '/api/checkin/delete':
            p = load_progress()
            p['checkins'].pop(data.get('date', ''), None)
            save_progress(p)
            return self.send_json(self.state())

        if path == '/api/quiz/check':
            topics   = load_content()
            topic_id = data['topicId']
            block    = 'exam' if data.get('mode') == 'exam' else 'quiz'
            answers  = data.get('answers', {})
            topic    = next((t for t in topics if t['id'] == topic_id), None)
            questions = (topic.get(block, []) if topic else []) or []
            results, right = {}, 0
            # считаем ВСЕ вопросы блока: без ответа = не засчитан
            for q in questions:
                given = answers.get(q['id'])
                ok = given is not None and check_answer(q, given)
                right += 1 if ok else 0
                results[q['id']] = {'ok': ok, 'answered': given is not None,
                                    'explain': q.get('explain', ''),
                                    'answer': q.get('answer')}
            total = len(questions)
            p = load_progress()
            p['quiz'].append({'topicId': topic_id, 'mode': block, 'score': right,
                              'total': total,
                              'ts': datetime.datetime.now().isoformat(timespec='seconds')})
            save_progress(p)
            return self.send_json({'results': results, 'score': right, 'total': total,
                                   'state': self.state()})

        if path == '/api/run':
            r = run_csharp(data.get('code', ''), data.get('stdin', ''),
                           data.get('taskId', 'scratch'))
            return self.send_json(r)

        if path == '/api/submit':
            topics = load_content()
            topic, task = find_task(topics, data.get('taskId', ''))
            if not task:
                return self.send_json({'error': 'задача не найдена'}, 400)
            code  = data.get('code', '')
            stdin = data.get('stdin', '')
            r = run_csharp(code, stdin, task['id'])
            sid = datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + task['id']
            sub = {'id': sid, 'taskId': task['id'], 'topicId': topic['id'],
                   'chapter': topic.get('chapter'), 'topicTitle': topic.get('title'),
                   'taskTitle': task['title'], 'code': code, 'stdin': stdin,
                   'note': data.get('note', ''), 'run': r,
                   'ts': datetime.datetime.now().isoformat(timespec='seconds')}
            with open(os.path.join(SUBS, sid + '.json'), 'w', encoding='utf-8') as f:
                json.dump(sub, f, ensure_ascii=False, indent=2)
            sys.stderr.write('\n*** НОВАЯ ОТПРАВКА НА ПРОВЕРКУ: %s\n\n' % sid)
            return self.send_json({'id': sid, 'run': r, 'state': self.state()})

        return self.send_error(404)


if __name__ == '__main__':
    n_topics = len(load_content())
    print('=' * 58)
    print('  Тренажёр C#  ->  http://127.0.0.1:%d' % PORT)
    print('  Тем загружено: %d' % n_topics)
    print('  Остановить: Ctrl+C')
    print('=' * 58)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nПока!')
