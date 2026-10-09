"""Эмулятор Честного Знака (ГИС МТ True API + СУЗ) для 1С.

1С подключается штатно: в базе включается «Тестовый контур ИС МП» и прокси-сервер (БСП)
127.0.0.1:<порт>. Эмулятор принимает CONNECT к *.crpt.ru / *.crptech.ru, поднимает TLS своим
сертификатом (корневой — certs/ca.crt, его нужно добавить в доверенные) и отвечает как ЧЗ.
Чужие хосты туннелируются как есть. Веб-интерфейс: http://127.0.0.1:<порт>/

Запуск: python chz_emulator.py [--port 3128] [--data data]
Только стандартная библиотека Python 3.10+ и openssl (идёт с Git for Windows) для сертификатов.
"""
import argparse, base64, datetime as dt, email, email.policy, json, os, random, re, select, shutil, socket, sqlite3
import ssl, string, subprocess, sys, threading, time, traceback, uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))   # ресурсы: ui.html, расширение
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else HERE
# данные (база, сертификаты, журнал) — в профиле пользователя, а не рядом с exe: новый exe в другой папке
# видит те же коды и тот же корневой сертификат; CHZ_EMULATOR_DATA — свой каталог
DATA_DIR = os.environ.get('CHZ_EMULATOR_DATA') or (
    os.path.join(os.environ['LOCALAPPDATA'], 'chz-emulator', 'data') if os.environ.get('LOCALAPPDATA')
    else os.path.join(APP_DIR, 'data'))
LEGACY_DATA_DIR = os.path.join(APP_DIR, 'data')   # где данные лежали до версии с DATA_DIR
NK_PROD = 'xn--80aqu.xn----7sbabas4ajkhfocclk9d3cvfsa.xn--p1ai'   # апи.национальный-каталог.рф
EMULATED = re.compile(r'(^|\.)(crpt\.ru|crptech\.ru|crpt\.tech|xn--80aqu\.xn----7sbabas4ajkhfocclk9d3cvfsa\.xn--p1ai'
                      r'|апи\.национальный-каталог\.рф)$', re.I)
SAN = ['*.crpt.ru', '*.sandbox.crptech.ru', '*.crptech.ru', 'crpt.ru', 'crptech.ru',
       '*.mark.crpt.ru', '*.crpt.tech', '*.nk.crptech.ru', '*.integrators.nk.crptech.ru', NK_PROD, 'localhost']
GS = '\x1d'

DEFAULT_SETTINGS = {
    'default_inn': '7700000001',       # ИНН, если из токена его не достать
    'strict': True,                    # проверять статусы и владельца при обработке документов
    'unknown_codes': '404',            # '404' — как ЧЗ; 'auto' — неизвестный код = в обороте у запросившего
    'doc_delay_sec': 1,                # сколько секунд документ «в обработке»
    'order_delay_sec': 1,              # сколько секунд заказ СУЗ готовится
    'reject_documents': False,         # все документы — CHECKED_NOT_OK (проверка ошибок в 1С)
    'reject_reason': 'Документ отклонён эмулятором (тест)',
    'auth_fail': False,                # simpleSignIn отвечает 401
    'token_ttl_hours': 10,
    'nk_placeholder': False,           # GTIN не из Нац. каталога: False — «не найден» как в ЧЗ, True — заглушка
    'suz_require_nk': False,           # заказ СУЗ на GTIN без опубликованной карточки НК отклоняется
    'short_codes': False,              # без templateId в заказе — «Укороченный КМ» (93) вместо стандартного (91+92)
}

# Шаблоны кодов маркировки по товарным группам: длина серийного, хвосты (AI, длина)
TEMPLATES = {   # запасной вариант, если группы нет в SUZ_TEMPLATES (табак, ncp и т. п.)
    'shoes': (13, [('91', 4), ('92', 88)]),
    'milk': (6, [('93', 4)]), 'petfood': (6, [('93', 4)]), 'seafood': (6, [('93', 4)]),
    'beer': (7, [('93', 4)]), 'nabeer': (7, [('93', 4)]), 'otp': (7, [('93', 4)]), 'ncp': (7, [('93', 4)]),
    'water': (13, [('93', 4)]), 'softdrinks': (13, [('93', 4)]), 'bio': (13, [('93', 4)]),
    'antiseptic': (13, [('93', 4)]), 'grocery': (13, [('93', 4)]),
    'electronics': (20, [('93', 4)]),
    # в классификаторе КА шаблонов нет — формат по аналогии с пищевыми группами
    'vegetableoil': (13, [('93', 4)]), 'pharmaraw': (13, [('93', 4)]), 'conserve': (13, [('93', 4)]),
    'sweets': (13, [('93', 4)]), 'tea_coffee': (13, [('93', 4)]),
}
DEFAULT_TEMPLATE = (13, [('91', 4), ('92', 44)])
# Шаблоны кодов СУЗ (единица товара) из классификатора КА 2.5.27 (общий макет
# КлассификаторыВидовПродукцииИС, SUZTemplates.json): формат кода выбирается по templateId заказа,
# без него — шаблон группы «по умолчанию»; настройка short_codes — «Укороченный КМ», если он есть.
# Хвост: S — 93(4), L44 — 91(4) + 92(44), L88 — 91(4) + 92(88). TEMPLATES — для групп, которых здесь нет.
SUZ_TEMPLATES = {   # templateId СУЗ: (товарная группа, длина серийного, хвост, по умолчанию, укороченный)
    25: ('antiseptic', 13, 'S', False, True), 31: ('antiseptic', 13, 'L44', True, False),
    60: ('autofluids', 13, 'S', False, True), 61: ('autofluids', 13, 'L44', True, False),
    18: ('beer', 7, 'S', False, False), 11: ('bicycle', 13, 'L44', False, False),
    23: ('bio', 13, 'S', False, True), 30: ('bio', 13, 'L44', True, False),
    52: ('books', 13, 'L44', False, False), 62: ('cableraw', 13, 'S', False, True),
    63: ('cableraw', 13, 'L44', True, False), 68: ('carparts', 6, 'S', False, True),
    69: ('carparts', 13, 'L44', True, False), 46: ('chemistry', 6, 'L44', True, False),
    47: ('chemistry', 6, 'S', False, True), 48: ('conserve', 6, 'L44', True, False),
    49: ('conserve', 6, 'S', False, True), 53: ('construction', 13, 'S', False, True),
    54: ('construction', 13, 'L44', True, False), 8: ('electronics', 20, 'L44', False, False),
    81: ('fertilizers', 6, 'S', False, True), 82: ('fertilizers', 13, 'L44', True, False),
    55: ('fire', 6, 'S', False, True), 56: ('fire', 13, 'L44', True, False),
    83: ('frozen', 6, 'S', False, True), 84: ('frozen', 13, 'L44', True, False),
    85: ('furslp', 13, 'L44', False, False), 72: ('gadgets', 20, 'S', False, True),
    73: ('gadgets', 20, 'L44', True, False), 42: ('grocery', 13, 'L44', True, False),
    43: ('grocery', 13, 'S', False, True), 57: ('heater', 6, 'S', False, True),
    58: ('heater', 6, 'L44', True, False), 77: ('homeware', 6, 'S', False, True),
    78: ('homeware', 13, 'L44', True, False), 79: ('industrial', 6, 'S', False, True),
    80: ('industrial', 13, 'L44', True, False), 10: ('lp', 13, 'L44', True, False),
    32: ('meat', 6, 'S', False, True), 74: ('meat', 13, 'L44', True, False), 20: ('milk', 6, 'S', False, False),
    28: ('nabeer', 7, 'S', False, False), 70: ('nicotindev', 13, 'S', False, True),
    71: ('nicotindev', 13, 'L44', True, False), 44: ('opticfiber', 13, 'L44', True, False),
    45: ('opticfiber', 13, 'S', False, True), 14: ('otp', 7, 'S', True, False),
    9: ('perfumery', 13, 'L44', False, False), 26: ('petfood', 6, 'L44', True, False),
    41: ('petfood', 6, 'S', False, True), 5: ('pharma', 13, 'L44', False, False),
    64: ('polymer', 6, 'S', False, True), 65: ('polymer', 6, 'L44', True, False),
    86: ('pyrotechnics', 6, 'S', False, True), 87: ('pyrotechnics', 13, 'L44', True, False),
    36: ('radio', 20, 'L44', True, False), 37: ('radio', 20, 'S', False, True),
    27: ('seafood', 6, 'L44', True, False), 38: ('seafood', 6, 'S', False, True),
    1: ('shoes', 13, 'L88', False, False), 29: ('softdrinks', 13, 'S', False, False),
    66: ('sweets', 6, 'S', False, True), 67: ('sweets', 13, 'L44', True, False),
    7: ('tires', 13, 'L44', False, False), 39: ('titan', 13, 'S', False, False),
    34: ('toys', 13, 'L44', True, False), 59: ('toys', 6, 'S', False, True),
    40: ('vegetableoil', 13, 'S', False, True), 51: ('vegetableoil', 13, 'L44', True, False),
    75: ('vetbio', 6, 'S', False, True), 76: ('vetbio', 13, 'L44', True, False),
    50: ('vetpharma', 13, 'L44', False, False), 16: ('water', 13, 'S', False, False),
    12: ('wheelchairs', 13, 'L44', False, False),
}
SUZ_TAILS = {'S': [('93', 4)], 'L44': [('91', 4), ('92', 44)], 'L88': [('91', 4), ('92', 88)]}
PG_ALIASES = {'cosmetics': 'chemistry'}
PG_IDS = {'lp': 1, 'shoes': 2, 'tobacco': 3, 'perfumery': 4, 'tires': 5, 'electronics': 6,
          'pharma': 7, 'milk': 8, 'bicycle': 9, 'wheelchairs': 10, 'otp': 12, 'water': 13,
          'furs': 14, 'beer': 15, 'ncp': 16, 'bio': 17, 'antiseptic': 19, 'petfood': 20,
          'seafood': 21, 'nabeer': 22, 'softdrinks': 23, 'vetpharma': 26, 'grocery': 32}
# Товарные группы для выбора в интерфейсе: код ЧЗ → название
PG_NAMES = {'lp': 'Лёгкая промышленность (одежда, бельё)', 'shoes': 'Обувь', 'perfumery': 'Парфюмерия',
            'tires': 'Шины и покрышки', 'electronics': 'Фототехника и электроника', 'tobacco': 'Табак',
            'otp': 'Альтернативная табачная продукция', 'ncp': 'Никотинсодержащая продукция',
            'milk': 'Молочная продукция', 'water': 'Упакованная вода', 'beer': 'Пиво и пивные напитки',
            'nabeer': 'Безалкогольное пиво', 'softdrinks': 'Безалкогольные напитки', 'bio': 'БАДы',
            'antiseptic': 'Антисептики', 'pharma': 'Лекарства', 'vetpharma': 'Ветеринарные препараты',
            'petfood': 'Корма для животных', 'seafood': 'Морепродукты', 'grocery': 'Бакалея',
            'vegetableoil': 'Растительные масла', 'conserve': 'Консервы', 'sweets': 'Кондитерские изделия',
            'tea_coffee': 'Чай и кофе', 'cosmetics': 'Косметика', 'chemistry': 'Косметика, бытовая химия и парфюмерия (в КА — «Парфюмерия»)',
            'bicycle': 'Велосипеды', 'wheelchairs': 'Кресла-коляски', 'furs': 'Изделия из меха'}

INTRODUCE = ('LP_INTRODUCE_GOODS', 'LP_INTRODUCE_OST', 'LP_INTRODUCE_GOODS_CROSSBORDER',
             'LP_GOODS_IMPORT', 'LP_FTS_INTRODUCE', 'LP_CONTRACT_COMMISSIONING', 'LP_RETURN',
             'LP_INTRODUCE_GOODS_INDIVIDUALS', 'LK_CONTRACT_COMMISSIONING', 'LK_INDI_COMMISSIONING',
             'LP_INTRODUCE_OST_CSV', 'CROSSBORDER', 'FURS_FTS_INTRODUCE',
             'LP_GOODS_IMPORT_AUTO', 'FURS_IMPORT', 'LP_FTS_INTRODUCE_AUTO')
RETIRE = ('LK_RECEIPT', 'LP_SHIP_GOODS_CROSSBORDER', 'EAS_CROSSBORDER_EXPORT', 'EAS_CROSSBORDER',
          'LK_REMARK_RETIRE', 'LP_RECEIPT', 'LK_KM_WITHDRAWAL', 'RECEIPT')
RETIRE_CANCEL = ('LK_RECEIPT_CANCEL', 'RECEIPT_RETURN')
WRITE_OFF = ('KM_CANCELLATION', 'LK_KM_CANCELLATION', 'LK_APPLIED_KM_CANCELLATION',
             'APPLIED_KM_CANCELLATION', 'LP_CANCEL_KM')
SHIP = ('LP_SHIP_GOODS', 'LP_SHIP_RECEIPT', 'LP_SHIP_GOODS_EAES')
ACCEPT = ('LP_ACCEPT_GOODS',)
CANCEL_SHIP = ('LP_CANCEL_SHIPMENT', 'LP_CANCEL_SHIPMENT_CROSSBORDER')
AGGREGATE = ('AGGREGATION_DOCUMENT', 'AGGREGATION', 'LP_AGGREGATION', 'SETS_AGGREGATION')
REMARK = ('LK_REMARK',)                                  # перемаркировка: last_uin → new_uin
CHANGE = ('CIS_INFORMATION_CHANGE',)                     # уточнение сведений (даты производства/годности)
INDIVIDUALIZE = ('LK_INDIVIDUALIZATION',)                # индивидуализация КИЗ → «нанесён»
CONNECT_TAP = ('CONNECT_TAP',)                           # подключение кега к оборудованию розлива
UTD = ('UNIVERSAL_TRANSFER_DOCUMENT',)                   # УПД с кодами → как отгрузка
# Принимаются без изменения кодов: ОСУ по GTIN (LK_GTIN_RECEIPT*, EAS_GTIN_*), OST_DESCRIPTION, отчёты,
# CIRCULATION_INFORMATION*, CIS_NOTICE, FIXATION*, ACCOUNTING, WRITE_OFF (сырьё), REPORT_REWEIGHING, УКД.
DISAGGREGATE = ('DISAGGREGATION_DOCUMENT', 'DISAGGREGATION', 'LP_DISAGGREGATION')
REAGGREGATE = ('REAGGREGATION_DOCUMENT', 'REAGGREGATION')
ATK = ('ATK_AGGREGATION', 'ATK_DISAGGREGATION', 'ATK_TRANSFORMATION')
# Особые состояния выбытия, которые знает 1С (СтатусКодаМаркировкиИСМП); прочие причины — без statusEx
RETIRED_EX = {'RETIRED_CANCELLATION', 'RETIRED_CONFISCATION', 'RETIRED_DAMAGE_LOSS', 'RETIRED_DESTRUCTION',
              'RETIRED_DONATION', 'RETIRED_EEC_EXPORT', 'RETIRED_BEYOND_EEC_EXPORT', 'RETIRED_ENTERPRISE_USE',
              'RETIRED_LIQUIDATION', 'RETIRED_NO_RETAIL_USE', 'RETIRED_RETURN'}
CODE_KEYS = {'uit_code', 'uitu_code', 'cis', 'uit', 'uitu', 'ki', 'kitu', 'uitCode', 'uituCode',
             'kiz', 'cises', 'sntins', 'codes', 'cis_list', 'cisList', 'code', 'new_uin', 'last_uin',
             'КИЗ', 'НомУпак', 'ИдентТрансУпак'}


def xml_to_dict(el):
    """XML → dict: атрибуты и дочерние теги — ключи, повторяющиеся теги — списки, текст листа — значение."""
    d = dict(el.attrib)
    for ch in el:
        tag = ch.tag.split('}')[-1]
        v = xml_to_dict(ch) if (len(ch) or ch.attrib) else (ch.text or '').strip()
        if tag in d:
            d[tag] = d[tag] if isinstance(d[tag], list) else [d[tag]]
            d[tag].append(v)
        else:
            d[tag] = v
    return d


def emission_type(order_body):
    """Способ выпуска из заказа СУЗ (releaseMethodType) → emissionType ответа cises/info."""
    r = (order_body.get('releaseMethodType') or (order_body.get('attributes') or {}).get('releaseMethodType')
         or 'PRODUCTION').upper()
    return {'PRODUCTION': 'LOCAL', 'PRODUCED_IN_RF': 'LOCAL', 'IMPORT': 'FOREIGN',
            'IMPORTED_INTO_RF': 'FOREIGN'}.get(r, r)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def rnd(n, alphabet=string.ascii_letters + string.digits):
    return ''.join(random.choice(alphabet) for _ in range(n))


def gtin_check(g13):
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(g13)))
    return str((10 - s % 10) % 10)


def make_gtin(base='460000000'):
    body = (base + rnd(13 - len(base), string.digits))[:13]
    return '0' + body + gtin_check(body) if len(body) == 12 else body[:13] + gtin_check(body[:13])


def make_sscc():
    body = '146' + rnd(14, string.digits)
    return '00' + body + gtin_check(body)


def code_template(pg, short=False, template_id=None):
    """(длина серийного, хвосты): по templateId СУЗ, иначе шаблон группы по умолчанию (short — укороченный)."""
    try:
        t = SUZ_TEMPLATES.get(int(template_id)) if template_id not in (None, '') else None
    except (TypeError, ValueError):
        t = None
    if not t:
        group = [v for v in SUZ_TEMPLATES.values() if v[0] == PG_ALIASES.get(pg, pg)]
        t = ((short and next((v for v in group if v[4]), None)) or next((v for v in group if v[3]), None)
             or next((v for v in group if not v[4]), None) or (group[0] if group else None))
    if t:
        return t[1], SUZ_TAILS[t[2]]
    return TEMPLATES.get(pg, DEFAULT_TEMPLATE)


def make_code(gtin, pg, short=False, template_id=None):
    """Полный код с криптохвостом (через GS) и его «короткая» форма cis = 01+GTIN+21+серийный."""
    serial_len, tails = code_template(pg, short, template_id)
    cis = '01' + gtin + '21' + rnd(serial_len)
    full = cis + ''.join(GS + ai + rnd(n) for ai, n in tails)
    return cis, full


# ---------------------------------------------------------------- хранилище

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS codes(cis TEXT PRIMARY KEY, full TEXT, gtin TEXT, pg TEXT,
            status TEXT, status_ex TEXT, owner_inn TEXT, producer_inn TEXT, package_type TEXT,
            parent TEXT, emission_date TEXT, applied_date TEXT, introduced_date TEXT,
            production_date TEXT, expiration_date TEXT, last_doc TEXT, order_id TEXT, extra TEXT);
        CREATE INDEX IF NOT EXISTS codes_parent ON codes(parent);
        CREATE INDEX IF NOT EXISTS codes_owner ON codes(owner_inn);
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, type TEXT, pg TEXT, format TEXT,
            status TEXT, sender_inn TEXT, receiver_inn TEXT, number TEXT, doc_date TEXT,
            received TEXT, ready_at REAL, body TEXT, raw TEXT, errors TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, oms_id TEXT, pg TEXT, inn TEXT,
            status TEXT, created TEXT, ready_at REAL, body TEXT, decline TEXT);
        CREATE TABLE IF NOT EXISTS buffers(order_id TEXT, gtin TEXT, total INTEGER, issued INTEGER,
            status TEXT, last_block TEXT, PRIMARY KEY(order_id, gtin));
        CREATE TABLE IF NOT EXISTS blocks(id TEXT PRIMARY KEY, order_id TEXT, gtin TEXT,
            created TEXT, codes TEXT);
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, kind TEXT, oms_id TEXT, inn TEXT,
            status TEXT, created TEXT, ready_at REAL, body TEXT, errors TEXT, doc_id TEXT);
        CREATE TABLE IF NOT EXISTS nk(good_id INTEGER PRIMARY KEY AUTOINCREMENT, gtin TEXT UNIQUE, name TEXT,
            brand TEXT, tnved TEXT, pg TEXT, inn TEXT, status TEXT, created TEXT, attrs TEXT);
        CREATE TABLE IF NOT EXISTS participants(inn TEXT PRIMARY KEY, name TEXT, status TEXT, pgs TEXT);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, inn TEXT, kind TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS mods(id TEXT PRIMARY KEY, inn TEXT, fias TEXT, kpp TEXT,
            address TEXT, pgs TEXT);
        CREATE TABLE IF NOT EXISTS dispenser(id TEXT PRIMARY KEY, inn TEXT, name TEXT, pg TEXT, params TEXT,
            created TEXT, result_id TEXT);
        CREATE TABLE IF NOT EXISTS agreements(id TEXT PRIMARY KEY, inn TEXT, status TEXT, body TEXT, created TEXT);
        ''')
        if self.one("SELECT name FROM sqlite_master WHERE name='products'"):   # старая база — в Нац. каталог
            self.db.execute("INSERT OR IGNORE INTO nk(gtin, name, brand, tnved, pg, inn, status, created) "
                            "SELECT gtin, name, brand, tnved, pg, inn, 'published', '' FROM products")
            self.db.execute('DROP TABLE products')

    def q(self, sql, *a):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, a).fetchall()]

    def one(self, sql, *a):
        r = self.q(sql, *a)
        return r[0] if r else None

    def x(self, sql, *a):
        with self.lock:
            self.db.execute(sql, a)

    def many(self, sql, rows):
        with self.lock:
            self.db.execute('BEGIN')
            try:
                self.db.executemany(sql, rows)
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise

    def setting(self, k):
        r = self.one('SELECT v FROM settings WHERE k=?', k)
        return json.loads(r['v']) if r else DEFAULT_SETTINGS.get(k)

    def settings(self):
        s = dict(DEFAULT_SETTINGS)
        for r in self.q('SELECT k, v FROM settings'):
            s[r['k']] = json.loads(r['v'])
        return s

    def set_setting(self, k, v):
        self.x('INSERT OR REPLACE INTO settings VALUES(?,?)', k, json.dumps(v, ensure_ascii=False))

    def upsert_code(self, **c):
        cols = ','.join(c)
        self.x(f'INSERT OR REPLACE INTO codes({cols}) VALUES({",".join("?" * len(c))})', *c.values())

    def update_code(self, cis, **c):
        sets = ','.join(f'{k}=?' for k in c)
        self.x(f'UPDATE codes SET {sets} WHERE cis=?', *c.values(), cis)

    def find_code(self, value):
        """Код в любом виде: со скобками, с GS и криптохвостом, без них, SSCC."""
        v = value.strip()
        if v.startswith('(01)'):
            v = re.sub(r'\((\d{2,4})\)', lambda m: (GS if m.start() else '') + m.group(1), v)
            v = v.replace(GS + '21', '21', 1)
        v = v.split(GS)[0]
        c = self.one('SELECT * FROM codes WHERE cis=?', v)
        if c or not v.startswith('01') or len(v) < 19:
            return c
        for n in (6, 7, 8, 11, 12, 13, 20):          # код с криптохвостом без разделителей
            c = self.one('SELECT * FROM codes WHERE cis=?', v[:18 + n])
            if c:
                return c
        return None

    @staticmethod
    def norm_gtin(gtin):
        g = str(gtin or '').strip()
        return g.zfill(14) if g.isdigit() and len(g) < 14 else g

    def nk_card(self, gtin):
        return self.one('SELECT * FROM nk WHERE gtin=?', self.norm_gtin(gtin))

    def nk_put(self, gtin, name=None, pg=None, inn=None, tnved='', brand='', status='published'):
        gtin = self.norm_gtin(gtin)
        old = self.nk_card(gtin)
        if old:
            self.x('UPDATE nk SET name=?, brand=?, tnved=?, pg=?, inn=?, status=? WHERE gtin=?',
                   name or old['name'], brand or old['brand'], tnved or old['tnved'], pg or old['pg'],
                   inn or old['inn'], status or old['status'], gtin)
        else:
            self.x('INSERT INTO nk(gtin, name, brand, tnved, pg, inn, status, created) VALUES(?,?,?,?,?,?,?,?)',
                   gtin, name or f'Товар {gtin}', brand or '', tnved or '', pg, inn, status or 'published', iso())
        return self.nk_card(gtin)

    def product(self, gtin):
        """Карточка из Национального каталога; None — товара в каталоге нет."""
        p = self.nk_card(gtin)
        if p:
            return p
        if self.setting('nk_placeholder'):
            return {'gtin': gtin, 'name': f'Товар {gtin} (нет в НК)', 'pg': None, 'inn': None,
                    'tnved': '', 'brand': '', 'status': None, 'good_id': None}
        return None

    def participant(self, inn):
        p = self.one('SELECT * FROM participants WHERE inn=?', inn)
        if p:
            p['pgs'] = json.loads(p['pgs'] or '[]')
            return p
        return {'inn': inn, 'name': f'Участник {inn}', 'status': 'REGISTERED', 'pgs': list(PG_IDS)}


# ---------------------------------------------------------------- логика ЧЗ

class Chz:
    def __init__(self, store):
        self.s = store
        self.log = []                 # кольцевой журнал запросов для веб-интерфейса
        self.log_lock = threading.Lock()

    # --- токены
    def jwt(self, inn, kind='ismp'):
        exp = int(time.time() + self.s.setting('token_ttl_hours') * 3600)
        payload = {'exp': exp, 'iat': int(time.time()), 'inn': inn, 'pid': random.randint(1, 10 ** 6),
                   'user_status': 'ACTIVE', 'full_name': 'Тестовый пользователь эмулятора',
                   'scope': ['trusted', 'read', 'write'], 'id': random.randint(1, 10 ** 6),
                   'organisation_status': 'REGISTERED', 'client_id': 'crpt_service',
                   'product_group_info': [{'name': pg, 'status': '5'} for pg in PG_IDS],
                   'emulator': True, 'kind': kind}
        return '.'.join([b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()),
                         b64url(json.dumps(payload).encode()), b64url(b'emulator')])

    def token_inn(self, token):
        if not token:
            return None
        parts = token.split('.')
        if len(parts) == 3:
            try:
                p = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
                if p.get('exp', 0) < time.time():
                    return None
                return p.get('inn') or self.s.setting('default_inn')
            except Exception:
                pass
        t = self.s.one('SELECT * FROM tokens WHERE token=?', token)
        if t and t['expires'] > time.time():
            return t['inn']
        return None

    @staticmethod
    def inn_from_signature(data):
        """Из «подписи» достаём ИНН: в CMS-подписи данных он лежит открытым текстом."""
        try:
            raw = base64.b64decode(data + '=' * (-len(data) % 4))
        except Exception:
            raw = (data or '').encode()
        m = re.search(rb'(?<!\d)(\d{12}|\d{10})(?!\d)', raw)
        return m.group(1).decode() if m else None

    # --- коды
    def cis_info(self, c, requested=None, with_children=True):
        p = (self.s.product(c['gtin']) if c['gtin'] else None) or {}
        owner = self.s.participant(c['owner_inn']) if c['owner_inn'] else None
        producer = self.s.participant(c['producer_inn']) if c['producer_inn'] else None
        info = {
            'requestedCis': requested or c['cis'], 'cis': c['cis'], 'gtin': c['gtin'],
            'tnVedEaes': (p.get('tnved') or '')[:4], 'tnVedEaesGroup': (p.get('tnved') or '')[:2],
            'productName': p.get('name'), 'productGroupId': PG_IDS.get(c['pg']),
            'productGroup': c['pg'], 'brand': p.get('brand'),
            'emissionDate': c['emission_date'], 'emissionType': json.loads(c['extra'] or '{}').get('emissionType') or 'LOCAL',
            'applicationDate': c['applied_date'], 'introducedDate': c['introduced_date'],
            'productionDate': c['production_date'], 'producedDate': c['production_date'],
            'expirationDate': c['expiration_date'],
            'status': c['status'], 'statusEx': c['status_ex'] or None,
            'packageType': c['package_type'] or 'UNIT',
            'generalPackageType': {'UNIT': 'UNIT', 'LEVEL1': 'GROUP', 'LEVEL2': 'BOX', 'BOX': 'BOX',
                                   'ATK': 'ATK', 'SET': 'SET', 'BUNDLE': 'BUNDLE', 'GROUP': 'GROUP'}.get(c['package_type'] or 'UNIT', 'UNIT'),
            'ownerInn': c['owner_inn'], 'ownerName': owner and owner['name'],
            'producerInn': c['producer_inn'], 'producerName': producer and producer['name'],
            'lastDocId': c['last_doc'], 'parent': c['parent'], 'markWithdraw': False,
        }
        children = self.s.q('SELECT * FROM codes WHERE parent=?', c['cis'])
        if children:
            info['child'] = [ch['cis'] for ch in children]
            info['countChildren'] = len(children)
        return {k: v for k, v in info.items() if v is not None}

    def auto_code(self, value, inn):
        """Режим unknown_codes='auto': неизвестный код считаем введённым в оборот у inn."""
        v = value.split(GS)[0].replace('(', '').replace(')', '')
        gtin = v[2:16] if v.startswith('01') and len(v) >= 18 else None
        c = dict(cis=v, full=value, gtin=gtin, pg=None, status='INTRODUCED', status_ex=None,
                 owner_inn=inn, producer_inn=inn, package_type='UNIT' if gtin else 'BOX',
                 parent=None, emission_date=iso(), applied_date=iso(), introduced_date=iso(),
                 production_date=iso(), expiration_date=None, last_doc=None, order_id=None, extra=None)
        self.s.upsert_code(**c)
        return c

    def lookup(self, value, inn):
        c = self.s.find_code(value)
        if not c and self.s.setting('unknown_codes') == 'auto':
            c = self.auto_code(value, inn)
        return c

    def cises_info(self, codes, inn):
        out = []
        for v in codes:
            c = self.lookup(v, inn)
            if c:
                out.append({'cisInfo': self.cis_info(c, v)})
            else:
                out.append({'cisInfo': {'requestedCis': v},
                            'errorMessage': 'Код идентификации не найден', 'errorCode': '404'})
        return out

    # --- документы
    @staticmethod
    def collect_codes(node, parent_key=None, out=None):
        out = [] if out is None else out
        if isinstance(node, dict):
            for k, v in node.items():
                Chz.collect_codes(v, k, out)
        elif isinstance(node, list):
            for v in node:
                Chz.collect_codes(v, parent_key, out)
        elif isinstance(node, str) and parent_key in CODE_KEYS and node:
            out.append(node)
        return out

    @staticmethod
    def first(body, *keys):
        if isinstance(body, dict):
            for k in keys:
                if body.get(k):
                    return body[k]
            for v in body.values():
                if isinstance(v, dict):
                    r = Chz.first(v, *keys)
                    if r:
                        return r
        return None

    def decode_document(self, product_document, fmt):
        raw = base64.b64decode(product_document) if product_document else b''
        text = None
        for enc in ('utf-8-sig', 'cp1251'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        text = text or ''
        if (fmt or 'MANUAL').upper() in ('MANUAL', 'JSON') or text.lstrip().startswith(('{', '[')):
            try:
                return json.loads(text), text
            except ValueError:
                pass
        if text.lstrip().startswith('<'):
            try:
                return xml_to_dict(ET.fromstring(re.sub(r'^<\?xml[^>]*\?>', '', text.strip()))), text
            except ET.ParseError:
                return {'xml': text}, text
        return {'csv': text.splitlines()}, text

    def create_document(self, req_inn, body, pg, url_kind):
        doc_type = (body.get('type') or body.get('documentType') or '').upper()
        fmt = body.get('document_format') or body.get('documentFormat') or 'MANUAL'
        prod = body.get('product_document') or body.get('productDocument')
        try:
            content, raw = self.decode_document(prod, fmt)
        except Exception as e:
            return None, f'Не удалось прочитать product_document: {e}'
        if not doc_type and isinstance(content, dict):
            doc_type = (content.get('doc_type') or content.get('document_type') or '').upper()
        doc_id = str(uuid.uuid4())
        sender = (self.first(content, 'participant_inn', 'participantInn', 'trade_participant_inn_sender',
                             'inn', 'sender_inn', 'participantId', 'owner_inn', 'exporter_taxpayer_id')
                  or req_inn)
        receiver = self.first(content, 'trade_participant_inn_receiver', 'receiver_inn', 'buyer_inn')
        number = self.first(content, 'document_number', 'document_num', 'doc_num', 'documentNumber',
                            'reg_number') or doc_id[:8]
        doc_date = self.first(content, 'document_date', 'doc_date', 'documentDate', 'action_date',
                              'transfer_date', 'production_date') or iso()
        if doc_type in UTD and isinstance(content, dict):
            def inn_under(node, key):
                part = self.first(content, key) if isinstance(content, dict) else None
                return self.first(part, 'ИННЮЛ', 'ИННФЛ') if isinstance(part, dict) else None
            sender = inn_under(content, 'СвПрод') or sender
            receiver = inn_under(content, 'СвПокуп') or receiver
            number = self.first(content, 'НомерДок', 'НомерСчФ') or number
        if doc_type in ACCEPT:   # приёмку подаёт получатель
            sender, receiver = (self.first(content, 'trade_participant_inn_sender') or sender), req_inn
        ready = time.time() + float(self.s.setting('doc_delay_sec') or 0)
        self.s.x('INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', doc_id, doc_type, pg, fmt,
                 'IN_PROGRESS', str(sender), receiver, str(number), str(doc_date), iso(), ready,
                 json.dumps(content, ensure_ascii=False), raw, '[]', None)
        errors = []
        try:
            if self.s.setting('reject_documents'):
                errors = [self.s.setting('reject_reason')]
            else:
                errors = self.apply_document(doc_id, doc_type, content, str(sender), receiver, req_inn, pg)
        except Exception as e:
            errors = [f'Ошибка обработки в эмуляторе: {e}']
            traceback.print_exc()
        status = 'CHECKED_NOT_OK' if errors else ('WAIT_ACCEPTANCE' if doc_type in SHIP
                                                  or doc_type in UTD and receiver else 'CHECKED_OK')
        self.s.x('UPDATE docs SET status=?, errors=? WHERE id=?', status,
                 json.dumps(errors, ensure_ascii=False), doc_id)
        return doc_id, None

    def apply_document(self, doc_id, t, content, sender, receiver, req_inn, pg):
        strict = self.s.setting('strict')
        values = self.collect_codes(content)
        if t in AGGREGATE or t in REAGGREGATE or t in ATK:
            return self.apply_aggregation(doc_id, t, content, req_inn, pg)
        if t in DISAGGREGATE:
            return self.apply_disaggregation(doc_id, content, req_inn)
        if t in REMARK:
            return self.apply_remark(doc_id, content, sender)
        codes, errors = [], []
        for v in values:
            c = self.lookup(v, sender)
            if not c:
                errors.append(f'{v}: код идентификации не найден в ГИС МТ')
                continue
            codes.append(c)
        if errors and strict:
            return errors

        def expand(cs):              # упаковки — вместе с вложениями
            res = []
            for c in cs:
                res.append(c)
                res.extend(expand(self.s.q('SELECT * FROM codes WHERE parent=?', c['cis'])))
            return res

        upd = {}
        if t in INTRODUCE:
            for c in codes:
                # True API: во всех видах ввода в оборот КИ в статусе «APPLIED» («Нанесён», после отчёта
                # о нанесении); «EMITTED» — отказ, как в ГИС МТ. Возврат в оборот — из «RETIRED»
                if strict and t == 'LP_RETURN' and c['status'] != 'RETIRED':
                    errors.append(f"{c['cis']}: недопустимый статус {c['status']} для {t}")
                elif strict and t != 'LP_RETURN' and c['status'] != 'APPLIED':
                    errors.append(f"{c['cis']}: недопустимый статус {c['status']} для {t}: "
                                  + ('нужен отчёт о нанесении (статус «APPLIED»)' if c['status'] == 'EMITTED'
                                     else 'требуется статус «APPLIED»'))
            upd = dict(status='INTRODUCED', status_ex=None, owner_inn=sender, introduced_date=iso())
            pd = self.first(content, 'production_date', 'productionDate')
            if pd:
                upd['production_date'] = pd
        elif t in RETIRE or t.startswith('LK_RECEIPT') and t not in RETIRE_CANCEL:
            action = (self.first(content, 'action', 'withdrawal_type', 'withdrawalReason') or 'RETAIL')
            for c in codes:
                if strict and c['status'] != 'INTRODUCED':
                    errors.append(f"{c['cis']}: код не в обороте ({c['status']})")
                if strict and c['owner_inn'] and c['owner_inn'] != sender:
                    errors.append(f"{c['cis']}: владелец {c['owner_inn']}, а не {sender}")
            ex = str(action).upper()
            ex = ex if ex.startswith('RETIRED') else 'RETIRED_' + ex
            upd = dict(status='RETIRED', status_ex=ex if ex in RETIRED_EX else None)  # розница — без особого состояния
            codes = expand(codes)
        elif t in RETIRE_CANCEL:
            upd = dict(status='INTRODUCED', status_ex=None)
        elif t in WRITE_OFF:
            upd = dict(status='WRITTEN_OFF', status_ex=None)
        elif t in CHANGE:
            pd = self.first(content, 'production_date', 'productionDate')
            ed = self.first(content, 'expiration_date', 'expirationDate')
            upd = {k: v for k, v in (('production_date', pd), ('expiration_date', ed)) if v}
        elif t in INDIVIDUALIZE:
            upd = dict(status='APPLIED', applied_date=iso())
        elif t in CONNECT_TAP:
            for c in codes:
                if strict and c['status'] != 'INTRODUCED':
                    errors.append(f"{c['cis']}: кег не в обороте ({c['status']})")
                extra = json.loads(c['extra'] or '{}')
                extra['connectDate'] = iso()
                self.s.update_code(c['cis'], extra=json.dumps(extra))
        elif t in SHIP or t in UTD and receiver:
            for c in codes:
                if strict and c['status'] != 'INTRODUCED':
                    errors.append(f"{c['cis']}: код не в обороте ({c['status']})")
                if strict and c['owner_inn'] and c['owner_inn'] != sender:
                    errors.append(f"{c['cis']}: владелец {c['owner_inn']}, а не {sender}")
            upd = dict(status_ex='WAIT_SHIPMENT')
            codes = expand(codes)
        elif t in ACCEPT:
            ship = self.s.one("SELECT * FROM docs WHERE type IN ('LP_SHIP_GOODS','LP_SHIP_RECEIPT','UNIVERSAL_TRANSFER_DOCUMENT') AND "
                              "receiver_inn=? AND status='WAIT_ACCEPTANCE' AND number=? ORDER BY received DESC",
                              req_inn, self.first(content, 'document_number', 'document_num') or '')
            if not codes and ship:
                codes = [self.s.find_code(v) for v in self.collect_codes(json.loads(ship['body']))]
                codes = [c for c in codes if c]
            accepted = self.first(content, 'accept_all') is not False
            upd = dict(status_ex=None, owner_inn=req_inn) if accepted else dict(status_ex=None)
            codes = expand(codes)
            if ship:
                self.s.x('UPDATE docs SET status=? WHERE id=?', 'ACCEPTED' if accepted else 'CANCELLED', ship['id'])
        elif t in CANCEL_SHIP:
            upd = dict(status_ex=None)
            codes = expand(codes)
            self.s.x("UPDATE docs SET status='CANCELLED' WHERE type IN ('LP_SHIP_GOODS','LP_SHIP_RECEIPT') "
                     "AND sender_inn=? AND status='WAIT_ACCEPTANCE' AND number=?", sender,
                     self.first(content, 'shipment_number', 'document_number') or '')
        if errors and strict:
            return errors
        for c in codes:
            self.s.update_code(c['cis'], last_doc=doc_id, **upd)
        if t in SHIP and receiver:
            # входящий документ получателю виден в doc/list как WAIT_ACCEPTANCE (он же — исходный)
            pass
        return errors if strict else []

    def apply_remark(self, doc_id, content, inn):
        """Перемаркировка: старый код выбывает (REMARK_RETIRED), новый вводится в оборот у владельца."""
        strict, errors, pairs = self.s.setting('strict'), [], []

        def walk(n):
            if isinstance(n, dict):
                if n.get('new_uin'):
                    pairs.append((n.get('last_uin'), n['new_uin']))
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(content)
        for old, new in pairs:
            c_new = self.lookup(new, inn)
            if not c_new:
                errors.append(f'{new}: новый код не найден (закажите коды в СУЗ со способом «Перемаркировка»)')
            elif strict and c_new['status'] not in ('EMITTED', 'APPLIED'):
                errors.append(f"{new}: новый код в статусе {c_new['status']}")
            c_old = self.s.find_code(old) if old else None
            if errors and strict:
                continue
            if c_old:
                self.s.update_code(c_old['cis'], status='RETIRED', status_ex='REMARK_RETIRED', last_doc=doc_id)
            if c_new:
                self.s.update_code(c_new['cis'], status='INTRODUCED', status_ex=None, owner_inn=inn,
                                   introduced_date=iso(), last_doc=doc_id,
                                   gtin=c_new['gtin'] or (c_old or {}).get('gtin'))
        if not pairs:
            errors.append('В документе перемаркировки нет пар last_uin/new_uin')
        return errors if strict else []

    def apply_aggregation(self, doc_id, t, content, inn, pg):
        errors = []
        units = (content.get('aggregationUnits') or content.get('aggregation_units') or
                 content.get('atkUnits') or [])
        if not units:                # XML МОТП: упаковки — узлы с kitu, вложения — ki/kitu внутри
            def walk(n):
                if isinstance(n, dict):
                    if n.get('kitu') and isinstance(n['kitu'], str) and len(n) > 1:
                        units.append({'unitSerialNumber': n['kitu'],
                                      'sntins': self.collect_codes({k: v for k, v in n.items() if k != 'kitu'})})
                    else:
                        for v in n.values():
                            walk(v)
                elif isinstance(n, list):
                    for v in n:
                        walk(v)
            walk(content)
        if t in ATK and not units:
            units = [{'unitSerialNumber': None, 'sntins': self.collect_codes(content)}]
        for u in units:
            parent = u.get('unitSerialNumber') or u.get('unit_serial_number') or u.get('atk')
            kids = u.get('sntins') or u.get('cises') or self.collect_codes(u)
            kids = [k for k in kids if k != parent]
            found = []
            for k in kids:
                c = self.lookup(k, inn)
                if not c:
                    errors.append(f'{k}: код идентификации не найден')
                elif c['parent'] and c['parent'] != parent and t not in REAGGREGATE:
                    errors.append(f"{k}: уже агрегирован в {c['parent']}")
                else:
                    found.append(c)
            if errors and self.s.setting('strict'):
                continue
            if t in ATK:
                parent = parent or ('ATK' + rnd(17, string.digits))
                ptype = 'ATK'
            else:
                ptype = 'SET' if t == 'SETS_AGGREGATION' else ('LEVEL2' if parent and parent.startswith('00') else 'LEVEL1')
            st = found[0]['status'] if found else 'APPLIED'
            gt = found[0]['gtin'] if found and ptype != 'LEVEL2' else None
            if not self.s.find_code(parent):
                self.s.upsert_code(cis=parent, full=parent, gtin=gt, pg=pg or (found[0]['pg'] if found else None),
                                   status=st, status_ex=None, owner_inn=inn, producer_inn=inn,
                                   package_type=ptype, parent=None, emission_date=iso(), applied_date=iso(),
                                   introduced_date=iso() if st == 'INTRODUCED' else None, production_date=None,
                                   expiration_date=None, last_doc=doc_id, order_id=None, extra=None)
            for c in found:
                self.s.update_code(c['cis'], parent=parent, last_doc=doc_id)
            u['_parent'] = parent
        if t in ATK:
            content['atk'] = [u.get('_parent') for u in units]
            self.s.x('UPDATE docs SET body=? WHERE id=?', json.dumps(content, ensure_ascii=False), doc_id)
        return errors if self.s.setting('strict') else []

    def apply_disaggregation(self, doc_id, content, inn):
        parents = self.collect_codes(content) + [x.get('uitu') for x in content.get('products_list', [])
                                                 if isinstance(x, dict) and x.get('uitu')]
        errors = []
        for p in set(parents):
            c = self.s.find_code(p)
            if not c:
                errors.append(f'{p}: упаковка не найдена')
                continue
            self.s.x('UPDATE codes SET parent=NULL, last_doc=? WHERE parent=?', doc_id, c['cis'])
            self.s.update_code(c['cis'], status='DISAGGREGATED', last_doc=doc_id)
        return errors if self.s.setting('strict') else []

    def doc_view(self, d, body=False, for_inn=None):
        status = d['status']
        if d['ready_at'] and time.time() < d['ready_at']:
            status = 'IN_PROGRESS'
        sender, receiver = self.s.participant(d['sender_inn']), d['receiver_inn'] and self.s.participant(d['receiver_inn'])
        v = {'number': d['number'], 'docDate': d['doc_date'], 'receivedAt': d['received'],
             'type': d['type'], 'status': status, 'senderInn': d['sender_inn'],
             'senderName': sender['name'], 'receiverInn': d['receiver_inn'],
             'receiverName': receiver and receiver['name'], 'documentFormat': d['format'],
             'productGroup': d['pg'], 'productGroupId': PG_IDS.get(d['pg']), 'id': d['id'],
             'docId': d['id'], 'input': bool(for_inn and d['receiver_inn'] == for_inn and d['sender_inn'] != for_inn),
             'downloadStatus': 'SUCCESS', 'downloadDesc': d['type']}
        errors = json.loads(d['errors'] or '[]')
        if status == 'CHECKED_NOT_OK':
            v['errors'] = errors
        if body:
            v['body'] = json.loads(d['body'] or '{}')
            v['content'] = d['raw']
        return {k: val for k, val in v.items() if val is not None}

    # --- СУЗ
    def create_order(self, oms_id, inn, body, pg_url):
        pg = body.get('productGroup') or pg_url or 'lp'
        oid = str(uuid.uuid4())
        delay = float(self.s.setting('order_delay_sec') or 0)
        self.s.x('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)', oid, oms_id, pg, inn, 'CREATED', iso(),
                 time.time() + delay, json.dumps(body, ensure_ascii=False), None)
        missing = []
        for p in body.get('products') or []:
            qty = int(p.get('quantity') or 0)
            gtin = p.get('gtin')
            card = self.s.nk_card(gtin) if gtin else None
            if gtin and (not card or card['status'] != 'published'):
                if self.s.setting('suz_require_nk'):
                    missing.append(gtin)
                elif not card:
                    self.s.nk_put(gtin, pg=pg, inn=inn, tnved=p.get('tnved') or p.get('tnvedCode') or '')
            # SELF_MADE: коды пришли от клиента (serialNumbers)
            serials = p.get('serialNumbers') or []
            self.s.x('INSERT OR REPLACE INTO buffers VALUES(?,?,?,?,?,?)', oid, gtin, qty or len(serials), 0, 'PENDING', None)
            if serials:
                p['_serials'] = serials
        self.s.x('UPDATE orders SET body=? WHERE id=?', json.dumps(body, ensure_ascii=False), oid)
        if missing:
            reason = 'Нет опубликованной карточки в Национальном каталоге: ' + ', '.join(missing)
            self.s.x("UPDATE orders SET status='DECLINED', decline=? WHERE id=?", reason, oid)
            self.s.x("UPDATE buffers SET status='REJECTED' WHERE order_id=?", oid)
        return oid, int(delay * 1000) + 1000

    def refresh_order(self, o):
        if o['status'] == 'CREATED' and time.time() >= (o['ready_at'] or 0):
            self.s.x("UPDATE orders SET status='READY' WHERE id=?", o['id'])
            self.s.x("UPDATE buffers SET status='ACTIVE' WHERE order_id=? AND status='PENDING'", o['id'])
            o['status'] = 'READY'
        return o

    def buffer_view(self, o, b):
        status = b['status']
        if status == 'ACTIVE' and b['issued'] >= b['total']:
            status = 'EXHAUSTED'
        left = 0 if status in ('PENDING', 'REJECTED') else b['total'] - b['issued']
        return {'orderId': o['id'], 'gtin': b['gtin'], 'leftInBuffer': left, 'totalCodes': b['total'],
                'unavailableCodes': 0, 'availableCodes': left, 'bufferStatus': status,
                'poolsExhausted': status == 'EXHAUSTED', 'totalPassed': b['issued'], 'omsId': o['oms_id'],
                'expiredDate': int((time.time() + 30 * 86400) * 1000), 'productionOrderId': o['id'],
                'rejectionReason': o.get('decline') if status == 'REJECTED' else None,
                'poolInfos': [{'status': 'READY' if status != 'PENDING' else 'PENDING',
                               'quantity': b['total'], 'leftInRegistrar': left, 'registrarId': 'emu-registrar',
                               'isRegistrarReady': True, 'registrarErrorCount': 0, 'lastRegistrarErrorTimestamp': 0}]}

    def issue_codes(self, o, gtin, qty):
        b = self.s.one('SELECT * FROM buffers WHERE order_id=? AND gtin=?', o['id'], gtin)
        if not b:
            return None, 'Подзаказ по GTIN не найден'
        if b['status'] not in ('ACTIVE',):
            return None, f"Буфер в статусе {b['status']}"
        qty = min(int(qty), b['total'] - b['issued'])
        if qty <= 0:
            return None, 'Коды в буфере закончились (EXHAUSTED)'
        body = json.loads(o['body'])
        prod = next((p for p in body.get('products', []) if p.get('gtin') == gtin), {})
        serials = prod.get('_serials') or []
        tpl = prod.get('templateId')
        # тип КИ из заказа (cisType) → тип упаковки кода: набор и комплект выпускаются своими КИ
        ptype = {'GROUP': 'LEVEL1', 'SET': 'SET', 'BUNDLE': 'BUNDLE'}.get(prod.get('cisType'), 'UNIT')
        rows, fulls = [], []
        short = self.s.setting('short_codes')
        for i in range(qty):
            if serials and b['issued'] + i < len(serials):
                cis = '01' + gtin + '21' + serials[b['issued'] + i]
                full = cis + ''.join(GS + ai + rnd(n) for ai, n in code_template(o['pg'], short, tpl)[1])
            else:
                cis, full = make_code(gtin, o['pg'], short, tpl)
            fulls.append(full)
            rows.append((cis, full, gtin, o['pg'], 'EMITTED', None, o['inn'], o['inn'], ptype, None, iso(),
                         None, None, None, None, None, o['id'], json.dumps({'emissionType': emission_type(body)})))
        self.s.many('INSERT OR REPLACE INTO codes VALUES(' + ','.join('?' * 18) + ')', rows)
        block = str(uuid.uuid4())
        self.s.x('INSERT INTO blocks VALUES(?,?,?,?,?)', block, o['id'], gtin, iso(), json.dumps(fulls))
        self.s.x('UPDATE buffers SET issued=issued+?, last_block=? WHERE order_id=? AND gtin=?', qty, block, o['id'], gtin)
        return {'omsId': o['oms_id'], 'codes': fulls, 'blockId': block}, None

    def create_report(self, kind, oms_id, inn, body):
        rid = str(uuid.uuid4())
        errors = []
        codes = body.get('sntins') or body.get('codes') or self.collect_codes(body)
        if kind == 'utilisation':
            for v in codes:
                c = self.s.find_code(v)
                if not c:
                    errors.append(f'{v}: код не найден в СУЗ')
                elif c['status'] not in ('EMITTED', 'APPLIED') and self.s.setting('strict'):
                    errors.append(f"{v}: статус {c['status']}, нанесение невозможно")
            if not errors:
                pd = body.get('productionDate') or (body.get('attributes') or {}).get('productionDate')
                ed = body.get('expirationDate') or (body.get('attributes') or {}).get('expirationDate')
                for v in codes:
                    c = self.s.find_code(v)
                    self.s.update_code(c['cis'], status='APPLIED', applied_date=iso(), last_doc=rid,
                                       production_date=pd or c['production_date'],
                                       expiration_date=ed or c['expiration_date'])
        elif kind == 'dropout':
            for v in codes:
                c = self.s.find_code(v)
                if c:
                    self.s.update_code(c['cis'], status='WRITTEN_OFF', last_doc=rid)
        elif kind == 'aggregation':
            doc_body = {'participantId': inn, 'aggregationUnits': body.get('aggregationUnits', [])}
            errors = self.apply_aggregation(rid, 'AGGREGATION_DOCUMENT', doc_body, inn, body.get('productGroup'))
        if self.s.setting('reject_documents'):
            errors = [self.s.setting('reject_reason')]
        ready = time.time() + float(self.s.setting('doc_delay_sec') or 0)
        self.s.x('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?)', rid, kind, oms_id, inn,
                 'REJECTED' if errors else 'SENT', iso(), ready, json.dumps(body, ensure_ascii=False),
                 json.dumps(errors, ensure_ascii=False), str(uuid.uuid4()))
        return rid


# ---------------------------------------------------------------- HTTP

class Resp(Exception):
    def __init__(self, code, body=None, ctype='application/json; charset=utf-8'):
        self.code, self.body, self.ctype = code, body, ctype


def err(code, msg):
    return Resp(code, {'error_message': msg, 'code': code, 'description': msg,
                       'globalErrors': [{'error': msg, 'errorCode': code}]})


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CHZ-Emulator/1.0'
    tunnel_host = None

    def log_message(self, fmt, *a):
        if self.server.verbose:
            sys.stderr.write('%s %s\n' % (self.tunnel_host or '-', fmt % a))

    # --- прокси
    def do_CONNECT(self):
        host, _, port = self.path.partition(':')
        port = int(port or 443)
        if not EMULATED.search(host):
            return self.tunnel(host, port)
        self.send_response(200, 'Connection established')
        self.send_header('Content-Length', '0')
        self.end_headers()
        self.wfile.flush()
        try:
            tls = self.server.tls.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as e:
            self.server.chz.add_log({'t': iso(), 'm': 'TLS', 'host': host, 'code': 0,
                                     'err': f'TLS не установлен: {e} (сертификат certs/ca.crt не в доверенных?)'})
            self.close_connection = True
            return
        h = Handler.__new__(Handler)
        h.tunnel_host = host
        try:
            Handler.__init__(h, tls, self.client_address, self.server)
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        self.close_connection = True

    def tunnel(self, host, port):
        try:
            remote = socket.create_connection((host, port), timeout=15)
        except OSError as e:
            self.send_error(502, f'Нет соединения с {host}:{port}: {e}')
            return
        self.send_response(200, 'Connection established')
        self.end_headers()
        conns = [self.connection, remote]
        try:
            while True:
                r, _, x = select.select(conns, [], conns, 60)
                if x or not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (remote if s is self.connection else self.connection).sendall(data)
        except OSError:
            pass
        finally:
            remote.close()
            self.close_connection = True

    def do_GET(self): self.dispatch('GET')
    def do_POST(self): self.dispatch('POST')
    def do_PUT(self): self.dispatch('PUT')
    def do_DELETE(self): self.dispatch('DELETE')
    def do_PATCH(self): self.dispatch('PATCH')

    def dispatch(self, method):
        chz = self.server.chz
        u = urlsplit(self.path)
        host = self.tunnel_host or u.hostname or (self.headers.get('Host') or '').split(':')[0]
        path = unquote(u.path).lstrip('/')
        query = {k: v[-1] for k, v in parse_qs(u.query).items()}
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        if u.scheme == 'http' and u.hostname and not EMULATED.search(u.hostname) \
                and u.hostname not in ('127.0.0.1', 'localhost'):
            return self.send_json(502, {'error': f'Эмулятор не проксирует http://{u.hostname}'})
        entry = {'t': iso(), 'm': method, 'host': host, 'path': path + ('?' + u.query if u.query else ''),
                 'req': raw[:4000].decode('utf-8', 'replace')}
        t0 = time.time()
        try:
            data = None
            if raw:
                ct = self.headers.get('Content-Type') or ''
                if 'json' in ct or raw.lstrip()[:1] in (b'{', b'['):
                    try:
                        data = json.loads(raw.decode('utf-8-sig'))
                    except ValueError as e:
                        raise err(400, f'Некорректный JSON в теле запроса: {e}')
                if data is None and 'x-www-form-urlencoded' in ct:
                    data = {k: v[-1] for k, v in parse_qs(raw.decode()).items()}
                if data is None and 'multipart/form-data' in ct:
                    msg = email.message_from_bytes(b'Content-Type: ' + ct.encode() + b'\r\n\r\n' + raw,
                                                   policy=email.policy.HTTP)
                    data = {'_multipart': {p.get_param('name', header='content-disposition'):
                                           p.get_payload(decode=True).decode('utf-8', 'replace')
                                           for p in msg.iter_parts()}}
            routes = ADMIN_ROUTES if not self.tunnel_host and (path == '' or path.startswith('_emu')) else ROUTES
            for m, rx, fn in routes:
                if m == method:
                    mt = rx.fullmatch(path)
                    if mt:
                        fn(self, chz, query=query, data=data, raw=raw, args=mt.groups(), host=host)
                        raise Resp(200, {})
            raise err(404, f'Метод не реализован в эмуляторе: {method} /{path}')
        except Resp as r:
            body = r.body
            if isinstance(body, (dict, list)):
                payload = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, bytes):
                payload = body
            else:
                payload = ('' if body is None else str(body)).encode()
            entry.update(code=r.code, resp=payload[:4000].decode('utf-8', 'replace'))
            self.send_raw(r.code, payload, r.ctype)
        except Exception as e:
            traceback.print_exc()
            entry.update(code=500, resp=str(e))
            self.send_json(500, {'error_message': f'Внутренняя ошибка эмулятора: {e}'})
        entry['ms'] = int((time.time() - t0) * 1000)
        if not path.startswith('_emu') and path:
            chz.add_log(entry)

    def send_raw(self, code, payload, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, code, obj):
        self.send_raw(code, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    # --- авторизация
    def inn(self, required=True):
        chz = self.server.chz
        tok = None
        a = self.headers.get('Authorization') or ''
        if a.lower().startswith('bearer '):
            tok = a[7:].strip()
        tok = tok or self.headers.get('clientToken') or self.headers.get('X-API-KEY') or self.headers.get('X-Client-Token')
        inn = chz.token_inn(tok)
        if not inn and required:
            raise Resp(401, {'error_message': 'Токен отсутствует или истёк', 'code': 401,
                             'globalErrors': [{'error': 'Unauthorized', 'errorCode': 401}]})
        return inn


def add_log(self, entry):
    with self.log_lock:
        self.log.append(entry)
        del self.log[:-500]
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with open(os.path.join(self.logdir, 'requests.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


Chz.add_log = add_log

ROUTES, ADMIN_ROUTES = [], []


def route(method, pattern, admin=False):
    def deco(fn):
        (ADMIN_ROUTES if admin else ROUTES).append((method, re.compile(pattern), fn))
        return fn
    return deco


def ok(body=None, code=200):
    raise Resp(code, body if body is not None else {})


def text(s, code=200):
    raise Resp(code, s, 'text/plain; charset=utf-8')


def as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get('cises') or data.get('codes') or data.get('cis') or []
    return []


# ---- ГИС МТ: авторизация
@route('GET', r'api/v3/(?:true-api/)?auth/(?:key|cert/key)')
def auth_key(h, chz, **k):
    u = str(uuid.uuid4())
    ok({'uuid': u, 'data': rnd(30, string.ascii_uppercase)})


@route('POST', r'api/v3/(?:true-api/)?auth/(?:simpleSignIn|cert/?)(?:/([^/]+))?')
def auth_sign_in(h, chz, data=None, args=(), **k):
    if chz.s.setting('auth_fail'):
        raise err(401, 'Ошибка проверки подписи (эмулятор: auth_fail)')
    data = data or {}
    inn = data.get('inn') or chz.inn_from_signature(data.get('data') or '') or chz.s.setting('default_inn')
    if args and args[0]:            # токен СУЗ: simpleSignIn/{omsConnection}
        tok = str(uuid.uuid4())
        chz.s.x('INSERT OR REPLACE INTO tokens VALUES(?,?,?,?)', tok, inn, 'suz', time.time() + 10 * 3600)
        ok({'token': tok})
    if data.get('unitedToken') or chz.s.setting('uuid_token'):
        tok = str(uuid.uuid4())
        chz.s.x('INSERT OR REPLACE INTO tokens VALUES(?,?,?,?)', tok, inn, 'ismp', time.time() + 10 * 3600)
        ok({'uuidToken': tok, 'token': chz.jwt(inn)})
    ok({'token': chz.jwt(inn)})


@route('POST', r'api/v3/true-api/auth/permissive-access')
def auth_permissive(h, chz, data=None, **k):
    inn = chz.inn_from_signature((data or {}).get('data') or '') or chz.s.setting('default_inn')
    ok({'access_token': chz.jwt(inn, 'retail'), 'expires_in': 3600 * 24, 'token_type': 'Bearer'})


# ---- участники, МОД, продукция
@route('GET', r'api/v3/(?:true-api/)?participants/(\d+)|api/v3/true-api/participants|api/v4/true-api/participants')
def participants(h, chz, args=(), query=None, **k):
    h.inn()
    inn = (args and args[0]) or query.get('inns') or query.get('inn')
    p = chz.s.participant(inn)
    if p['status'] == 'NOT_REGISTERED':
        raise Resp(404, {'error_message': f'Участник с ИНН {inn} не зарегистрирован', 'code': 404})
    pgs = p['pgs']
    body = {'inn': inn, 'status': 'Зарегистрирован' if p['status'] == 'REGISTERED' else p['status'],
            'name': p['name'], 'productGroups': pgs, 'is_registered': True, 'isRegistered': True,
            'productGroupInfo': [{'productGroup': g, 'name': g, 'status': '5', 'types': ['TRADE_PARTICIPANT'],
                                  'isControllability': False} for g in pgs]}
    ok([body] if not args or not args[0] else body)


@route('POST', r'api/v3/true-api/mods/info')
def mods_info(h, chz, **k):
    inn = h.inn()
    mods = chz.s.q('SELECT * FROM mods WHERE inn=?', inn)
    ok({'isBlocked': False, 'mods': [{'id': m['id'], 'fiasId': m['fias'], 'kpp': m['kpp'], 'address': m['address'],
                                      'productGroups': json.loads(m['pgs'] or '[]'), 'status': 'ACTIVE'} for m in mods]})


@route('GET', r'api/v3/true-api/mods/list')
def mods_list(h, chz, **k):
    inn = h.inn()
    ok([{'id': m['id'], 'fiasId': m['fias'], 'kpp': m['kpp'], 'address': m['address'] or 'Адрес МОД (эмулятор)',
         'productGroups': json.loads(m['pgs'] or '[]')} for m in chz.s.q('SELECT * FROM mods WHERE inn=?', inn)])


@route('POST', r'api/v3/true-api/mods/add')
def mods_add(h, chz, data=None, **k):
    inn = h.inn()
    mods = (data or {}).get('mods') or []
    for m in mods:
        chz.s.x('INSERT OR REPLACE INTO mods VALUES(?,?,?,?,?,?)', m.get('fiasId'), inn, m.get('fiasId'),
                m.get('kpp'), f"Адрес по ФИАС {m.get('fiasId')}", json.dumps(m.get('productGroup') or []))
    ok({'results': [{'groupDescription': 'Успешно зарегистрированы', 'code': 200, 'count': len(mods), 'modList': []}]})


@route('DELETE', r'api/v3/true-api/mod')
def mod_delete(h, chz, data=None, query=None, **k):
    h.inn()
    fias = (data or {}).get('fiasId') or query.get('fiasId')
    chz.s.x('DELETE FROM mods WHERE fias=?', fias)
    ok({})


@route('GET', r'api/v3/true-api/sim/mods/search')
def sim_mods(h, chz, **k):
    h.inn()
    ok({'items': [], 'nextPage': None})


def product_view(chz, gtin):
    p = chz.s.product(gtin)
    if not p:
        return None
    return {'gtin': gtin, 'name': p['name'], 'inn': p['inn'], 'brand': p['brand'],
            'productGroupId': PG_IDS.get(p['pg']), 'productGroup': p['pg'], 'tnVedCode': p['tnved'],
            'tnVedCode10': p['tnved'], 'packageType': 'UNIT', 'isKit': False, 'isSet': False,
            'goodSignedFlag': 'true', 'goodMarkFlag': 'true', 'goodTurnFlag': 'true'}


@route('POST', r'api/v4/true-api/product/info|api/v3/true-api/product/info')
def product_info(h, chz, data=None, query=None, **k):
    h.inn()
    gtins = (data or {}).get('gtins') if isinstance(data, dict) else data
    gtins = gtins or [g for g in (query.get('gtins') or '').split(',') if g]
    found = [v for v in (product_view(chz, g) for g in gtins) if v]
    ok({'results': found, 'total': len(found), 'errorCode': None})


@route('GET', r'api/v4/true-api/product/info|api/v3/true-api/product/info')
def product_info_get(h, chz, query=None, **k):
    product_info(h, chz, data=None, query=query)


@route('GET', r'api/v4/true-api/product/gtin')
def product_gtin(h, chz, query=None, **k):
    inn = h.inn()
    rows = chz.s.q("SELECT gtin FROM nk WHERE inn=? AND status='published'", query.get('inn') or inn)
    gt = [r['gtin'] for r in rows]
    ok({'results': gt, 'gtins': gt, 'total': len(gt)})


@route('POST', r'api/v3/true-api/product/gtin-weight/cis-count')
def gtin_weight(h, chz, data=None, **k):
    h.inn()
    ok({'products': [{'gtin': p.get('gtin'), 'cisCount': 1} for p in (data or {}).get('products', [])]})


@route('GET', r'api/v3/true-api/products/listV2')
def products_list(h, chz, **k):
    h.inn()
    ok({'results': []})


# ---- коды маркировки
@route('POST', r'api/v3/true-api/cises/info|api/v4/true-api/cises/info')
def cises_info(h, chz, data=None, **k):
    inn = h.inn()
    ok(chz.cises_info(as_list(data), inn))


@route('POST', r'api/v3/true-api/cises/eaes/info')
def cises_eaes(h, chz, data=None, **k):
    h.inn()
    ok([{'cisInfo': {'requestedCis': c}, 'errorMessage': 'Код не найден', 'errorCode': '404'} for c in as_list(data)])


@route('POST', r'api/v3/true-api/cises/gz/info')
def cises_gz(h, chz, data=None, **k):
    h.inn()
    ok([{'cis': c, 'cisWithoutBrackets': c, 'inGrayZone': False, 'containsGrayCodes': False} for c in as_list(data)])


@route('POST', r'api/v3/true-api/cises/aggregated/list')
def cises_aggregated(h, chz, data=None, **k):
    h.inn()
    out = {}
    for v in as_list(data):
        c = chz.s.find_code(v)
        out[v] = [ch['cis'] for ch in chz.s.q('SELECT cis FROM codes WHERE parent=?', c['cis'])] if c else []
    ok(out)


@route('POST', r'api/v4/true-api/cises/search')
def cises_search(h, chz, data=None, **k):
    inn = h.inn()
    f = (data or {}).get('filter') or {}
    pg_ = (data or {}).get('pagination') or {}
    per = int(pg_.get('perPage') or 100)
    sql, a = 'SELECT * FROM codes WHERE owner_inn=?', [inn]
    if f.get('states'):
        st = [s.get('status') if isinstance(s, dict) else s for s in f['states']]
        sql += ' AND status IN (%s)' % ','.join('?' * len(st))
        a += st
    if f.get('productGroups'):
        sql += ' AND pg IN (%s)' % ','.join('?' * len(f['productGroups']))
        a += f['productGroups']
    if pg_.get('lastEmissionDate'):
        sql += ' AND emission_date > ?'
        a.append(pg_['lastEmissionDate'])
    rows = chz.s.q(sql + ' ORDER BY emission_date LIMIT ?', *a, per + 1)
    ok({'result': [chz.cis_info(c) | {'sgtin': c['cis']} for c in rows[:per]], 'isLastPage': len(rows) <= per})


@route('POST', r'api/v3/true-api/warehouse/balance')
def warehouse(h, chz, data=None, **k):
    inn = h.inn()
    gtins = (data or {}).get('gtins') or []
    rows = chz.s.q("SELECT gtin, COUNT(*) n FROM codes WHERE owner_inn=? AND status='INTRODUCED' GROUP BY gtin", inn)
    ok({'result': [{'gtin': r['gtin'], 'quantity': r['n']} for r in rows if not gtins or r['gtin'] in gtins],
        'pagination': {'page': 1, 'perPage': 1000}})


@route('GET', r'api/v3/true-api/sim/warehouse/balance/actual')
def sim_balance(h, chz, **k):
    h.inn()
    ok({'items': [], 'nextPage': None})


# ---- документы
@route('POST', r'api/v3/true-api/(lk|sim)/documents/create|api/v3/true-api/documents/(aggregation|dropped-out)/create|api/v3/lk/documents/create')
def doc_create(h, chz, data=None, query=None, args=(), **k):
    inn = h.inn()
    if not isinstance(data, dict):
        raise err(400, 'Тело запроса должно быть JSON-объектом')
    pg = query.get('pg')
    if '_multipart' in data:          # МОТП: XML файлом в форме, подпись в X-Signature
        xml = next(iter(data['_multipart'].values()), '')
        data = {'document_format': 'XML', 'product_document': base64.b64encode(xml.encode()).decode(),
                'type': 'AGGREGATION_DOCUMENT' if args[1] == 'aggregation' else 'KM_CANCELLATION'}
    if args and args[1] == 'aggregation' and not data.get('type'):
        data['type'] = 'AGGREGATION_DOCUMENT'
    doc_id, e = chz.create_document(inn, data, pg, args and args[0])
    if e:
        raise err(400, e)
    text(doc_id)


@route('GET', r'api/v4/true-api/doc/([^/]+)/info|api/v3/true-api/doc/([^/]+)/info')
def doc_info(h, chz, args=(), query=None, **k):
    inn = h.inn()
    did = args[0] or args[1]
    d = chz.s.one('SELECT * FROM docs WHERE id=?', did)
    if not d:
        raise err(404, f'Документ {did} не найден')
    ok([chz.doc_view(d, body=query.get('body') == 'true', for_inn=inn)])


@route('GET', r'api/v3/true-api/sim/documents/([^/]+)')
def sim_doc_info(h, chz, args=(), **k):
    inn = h.inn()
    d = chz.s.one('SELECT * FROM docs WHERE id=?', args[0])
    if not d:
        raise err(404, 'Документ не найден')
    v = chz.doc_view(d, for_inn=inn)
    v['errors'] = [{'errorCode': 'EMU', 'localizedMessage': e} for e in json.loads(d['errors'] or '[]')]
    ok(v)


@route('GET', r'api/v4/true-api/receipt/list')
def receipt_list(h, chz, **k):
    h.inn()
    ok({'results': [], 'nextPage': False})


@route('GET', r'api/v4/true-api/doc/list|api/v3/true-api/doc/list')
def doc_list(h, chz, query=None, **k):
    inn = h.inn()
    sql, a = 'SELECT * FROM docs WHERE (sender_inn=? OR receiver_inn=?)', [inn, inn]
    for qk, col in (('documentType', 'type'), ('documentStatus', 'status'), ('number', 'number'), ('pg', 'pg'),
                    ('senderInn', 'sender_inn'), ('receiverInn', 'receiver_inn')):
        if query.get(qk):
            sql += f' AND {col}=?'
            a.append(query[qk])
    if query.get('dateFrom'):
        sql += ' AND received >= ?'
        a.append(query['dateFrom'])
    if query.get('dateTo'):
        sql += ' AND received <= ?'
        a.append(query['dateTo'])
    inp = query.get('input')
    if inp in ('true', 'false'):
        sql += ' AND receiver_inn=? AND sender_inn<>?' if inp == 'true' else ' AND sender_inn=?'
        a += [inn, inn] if inp == 'true' else [inn]
    limit = int(query.get('limit') or 1000)
    rows = chz.s.q(sql + ' ORDER BY received DESC LIMIT ?', *a, limit + 1)
    ok({'results': [chz.doc_view(d, for_inn=inn) for d in rows[:limit]], 'nextPage': len(rows) > limit})


@route('POST', r'api/v3/true-api/doc/validator/create')
def validator_create(h, chz, data=None, **k):
    h.inn()
    ok({'id': (data or {}).get('id') or str(uuid.uuid4()), 'message': 'Принято'})


@route('POST', r'api/v3/true-api/doc/validator/status')
def validator_status(h, chz, data=None, **k):
    h.inn()
    ok({'id': (data or {}).get('id'), 'status': 'SUCCESS', 'errors': []})


@route('POST', r'api/v3/true-api/document/reprocess')
def reprocess(h, chz, data=None, **k):
    h.inn()
    ok({'code': 0, 'description': 'ok'})


@route('GET', r'api/v3/true-api/documents/edo/tpr/ud')
def edo_ud(h, chz, **k):
    h.inn()
    ok({'status': 'CHECKED_OK'})


# ---- CDN / разрешительный режим
@route('GET', r'api/v4/true-api/cdn/info')
def cdn_info(h, chz, **k):
    h.inn()
    ok({'code': 0, 'description': 'ok', 'hosts': [{'host': 'https://cdn01.sandbox.crptech.ru'},
                                                   {'host': 'https://cdn02.sandbox.crptech.ru'}]})


@route('GET', r'api/v4/true-api/cdn/health/check')
def cdn_health(h, chz, **k):
    ok({'code': 0, 'description': 'ok', 'avgTimeMs': 5})


@route('POST', r'api/v4/true-api/codes/check')
def codes_check(h, chz, data=None, **k):
    inn = h.inn(required=False) or chz.s.setting('default_inn')
    out = []
    for v in (data or {}).get('codes', []):
        c = chz.lookup(v, inn)
        if not c:
            out.append({'cis': v, 'valid': False, 'found': False, 'verified': False, 'errorCode': 1,
                        'realizable': False, 'utilised': False, 'isBlocked': False, 'sold': False})
            continue
        p = (chz.s.product(c['gtin']) if c['gtin'] else None) or {}
        out.append({'cis': c['cis'], 'printView': c['cis'], 'gtin': c['gtin'], 'valid': True, 'found': True,
                    'verified': True, 'errorCode': 0, 'groupIds': [PG_IDS.get(c['pg'], 0)],
                    'realizable': c['status'] == 'INTRODUCED' and not c['status_ex'],
                    'utilised': c['status'] in ('APPLIED', 'INTRODUCED', 'RETIRED'), 'isBlocked': False,
                    'sold': c['status'] == 'RETIRED', 'isOwner': c['owner_inn'] == inn, 'isTracking': True,
                    'packageType': c['package_type'] or 'UNIT', 'producerInn': c['producer_inn'],
                    'expireDate': c['expiration_date'], 'productionDate': c['production_date'],
                    'grayZone': False, 'productName': p.get('name')})
    ok({'code': 0, 'description': 'ok', 'codes': out, 'reqId': str(uuid.uuid4()),
        'reqTimestamp': int(time.time() * 1000)})


# ---- СУЗ (V3 и V2/extension)
def oms(query):
    return query.get('omsId') or 'emu-oms'


def suz_inn(h):
    return h.inn()


@route('POST', r'api/v[23]/integration/connection')
def suz_connection(h, chz, query=None, **k):
    ok({'status': 'SUCCESS', 'omsConnection': str(uuid.uuid4())})


@route('GET', r'api/v[23]/(?:[a-z_]+/)?token')
def suz_token(h, chz, query=None, **k):
    tok = str(uuid.uuid4())
    chz.s.x('INSERT OR REPLACE INTO tokens VALUES(?,?,?,?)', tok, chz.s.setting('default_inn'), 'suz', time.time() + 36000)
    ok({'token': tok})


@route('GET', r'api/v3/ping|api/v[23]/[a-z_]+/ping')
def suz_ping(h, chz, query=None, **k):
    suz_inn(h)
    ok({'omsId': oms(query), 'apiVersion': '3.0', 'omsVersion': 'emulator'})


@route('POST', r'api/v3/order|api/v2/([a-z_]+)/orders')
def suz_order(h, chz, query=None, data=None, args=(), **k):
    inn = suz_inn(h)
    if not isinstance(data, dict) or not data.get('products'):
        raise err(400, 'В заказе нет products')
    oid, ms = chz.create_order(oms(query), inn, data, args and args[0])
    ok({'omsId': oms(query), 'orderId': oid, 'expectedCompleteTimestamp': ms, 'expectedCompletionTime': ms})


@route('GET', r'api/v3/order/status|api/v2/([a-z_]+)/buffer/status')
def suz_status(h, chz, query=None, args=(), **k):
    suz_inn(h)
    o = chz.s.one('SELECT * FROM orders WHERE id=?', query.get('orderId'))
    if not o:
        raise err(404, 'Заказ не найден')
    chz.refresh_order(o)
    bs = chz.s.q('SELECT * FROM buffers WHERE order_id=?' + (' AND gtin=?' if query.get('gtin') else ''),
                 o['id'], *([query['gtin']] if query.get('gtin') else []))
    views = [chz.buffer_view(o, b) for b in bs]
    if not views:
        raise err(404, 'Подзаказ не найден')
    ok(views if args == () or not args or args[0] is None else views[0])


@route('GET', r'api/v3/order/list|api/v2/([a-z_]+)/orders')
def suz_list(h, chz, query=None, **k):
    inn = suz_inn(h)
    sql, a = 'SELECT * FROM orders WHERE inn=?', [inn]
    if query.get('orderId'):
        sql, a = 'SELECT * FROM orders WHERE id=?', [query['orderId']]
    infos = []
    for o in chz.s.q(sql + ' ORDER BY created DESC LIMIT 200', *a):
        chz.refresh_order(o)
        bs = chz.s.q('SELECT * FROM buffers WHERE order_id=?', o['id'])
        st = o['status']
        if st == 'READY' and bs and all(b['issued'] >= b['total'] for b in bs):
            st = 'READY'
        infos.append({'orderId': o['id'], 'orderStatus': st, 'createdTimestamp': o['created'],
                      'productGroupType': o['pg'], 'declineReason': o['decline'],
                      'buffers': [chz.buffer_view(o, b) for b in bs]})
    ok({'omsId': oms(query), 'orderInfos': infos})


@route('GET', r'api/v3/codes|api/v2/([a-z_]+)/codes')
def suz_codes(h, chz, query=None, **k):
    suz_inn(h)
    o = chz.s.one('SELECT * FROM orders WHERE id=?', query.get('orderId'))
    if not o:
        raise err(404, 'Заказ не найден')
    chz.refresh_order(o)
    res, e = chz.issue_codes(o, query.get('gtin'), query.get('quantity') or 1000)
    if e:
        raise Resp(400, {'success': False, 'globalErrors': [{'error': e, 'errorCode': 400}]})
    ok(res)


@route('GET', r'api/v3/order/codes/retry|api/v2/([a-z_]+)/codes/retry')
def suz_retry(h, chz, query=None, **k):
    suz_inn(h)
    b = chz.s.one('SELECT * FROM blocks WHERE id=?', query.get('blockId'))
    if not b:
        raise err(404, 'Блок кодов не найден')
    o = chz.s.one('SELECT * FROM orders WHERE id=?', b['order_id'])
    ok({'omsId': o['oms_id'], 'codes': json.loads(b['codes']), 'blockId': b['id']})


@route('GET', r'api/v3/order/codes/blocks|api/v2/([a-z_]+)/codes/blocks')
def suz_blocks(h, chz, query=None, **k):
    suz_inn(h)
    bl = chz.s.q('SELECT * FROM blocks WHERE order_id=? AND gtin=? ORDER BY created',
                 query.get('orderId'), query.get('gtin'))
    ok({'omsId': oms(query), 'orderId': query.get('orderId'), 'gtin': query.get('gtin'),
        'blocks': [{'blockId': b['id'], 'blockDateTime': b['created'], 'quantity': len(json.loads(b['codes']))}
                   for b in bl]})


@route('POST', r'api/v3/order/close|api/v2/([a-z_]+)/(?:buffer|orders)/close')
def suz_close(h, chz, query=None, data=None, **k):
    suz_inn(h)
    oid = query.get('orderId') or (data or {}).get('orderId')
    gtin = query.get('gtin') or (data or {}).get('gtin')
    chz.s.x('UPDATE buffers SET status=? WHERE order_id=?' + (' AND gtin=?' if gtin else ''),
            'CLOSED', oid, *([gtin] if gtin else []))
    if not gtin:
        chz.s.x("UPDATE orders SET status='CLOSED' WHERE id=?", oid)
    ok({'omsId': oms(query), 'reportId': str(uuid.uuid4())})


@route('POST', r'api/v3/(utilisation|dropout|aggregation)|api/v2/[a-z_]+/(utilisation|dropout|aggregation|acceptance)')
def suz_report(h, chz, query=None, data=None, args=(), **k):
    inn = suz_inn(h)
    kind = args[0] or args[1]
    rid = chz.create_report(kind, oms(query), inn, data or {})
    ok({'omsId': oms(query), 'reportId': rid})


@route('GET', r'api/v3/report/info|api/v2/[a-z_]+/reports?(?:/info)?')
def suz_report_info(h, chz, query=None, **k):
    suz_inn(h)
    r = chz.s.one('SELECT * FROM reports WHERE id=?', query.get('reportId'))
    if not r:
        raise err(404, 'Отчёт не найден')
    st = 'PENDING' if time.time() < (r['ready_at'] or 0) else r['status']
    errs = json.loads(r['errors'] or '[]')
    body = {'omsId': r['oms_id'], 'reportId': r['id'], 'reportStatus': st, 'status': st,
            'resultDocId': r['doc_id']}
    if errs:
        body['errorReason'] = '; '.join(errs)
    ok(body)


@route('GET', r'api/v[23]/(?:[a-z_]+/)?receipts/receipt')
def suz_receipt(h, chz, query=None, **k):
    suz_inn(h)
    r = chz.s.one('SELECT * FROM reports WHERE doc_id=? OR id=?', query.get('resultDocId'), query.get('resultDocId'))
    errs = json.loads(r['errors'] or '[]') if r else []
    ok({'results': [{'resultDocId': query.get('resultDocId'), 'state': 'FAILED' if errs else 'SUCCESS',
                     'description': '; '.join(errs) or None,
                     'operations': [{'operationType': 'RNMS_GIS_PROCESSED', 'docId': query.get('resultDocId')}]}]})


@route('GET', r'api/v[23]/(?:[a-z_]+/)?receipts/document')
def suz_receipt_doc(h, chz, query=None, **k):
    suz_inn(h)
    ok({'content': json.dumps({'cisList': []})})


@route('GET', r'api/v3/providers|api/v2/[a-z_]+/providers')
def suz_providers(h, chz, query=None, **k):
    suz_inn(h)
    ok({'providers': []})


@route('GET', r'api/v3/mod|api/v2/[a-z_]+/mod')
def suz_mod(h, chz, query=None, **k):
    inn = suz_inn(h)
    ok([{'fiasId': m['fias'], 'address': m['address'], 'kpp': m['kpp']} for m in chz.s.q('SELECT * FROM mods WHERE inn=?', inn)])


@route('POST', r'api/v2/[a-z_]+/product/info')
def suz_product_info(h, chz, data=None, **k):
    suz_inn(h)
    gt = (data or {}).get('gtins') or []
    ok({'products': [{'gtin': g, 'name': (chz.s.product(g) or {}).get('name', ''), 'productAttributes': {}} for g in gt]})


@route('GET', r'api/v3/true-api/documents/([^/]+)/info')
def documents_info_v3(h, chz, args=(), **k):
    inn = h.inn()
    d = chz.s.one('SELECT * FROM docs WHERE id=?', args[0])
    if not d:
        raise err(404, 'Документ не найден')
    ok(chz.doc_view(d, for_inn=inn))


@route('GET', r'api/v4/true-api/edo/inn/(\d+)')
def edo_inn(h, chz, args=(), **k):
    h.inn()
    ok({'id': f'2BM-{args[0]}-EMU', 'inn': args[0], 'operator': 'ЭДО-лайт (эмулятор)'})


# ---- Выгрузки ГИС МТ (dispenser): «Список КИ на балансе», «Сведения об отклонениях»
@route('POST', r'api/v3/true-api/dispenser/tasks')
def dispenser_create(h, chz, data=None, **k):
    inn = h.inn()
    d = data or {}
    tid, created = str(uuid.uuid4()), iso()
    chz.s.x('INSERT INTO dispenser VALUES(?,?,?,?,?,?,?)', tid, inn, d.get('name') or '',
            str(d.get('productGroupCode') or ''), json.dumps(d, ensure_ascii=False), created, str(uuid.uuid4()))
    ok({'id': tid, 'name': d.get('name'), 'currentStatus': 'PREPARATION', 'createDate': created})


@route('GET', r'api/v3/true-api/dispenser/tasks/([^/]+)')
def dispenser_task(h, chz, args=(), **k):
    h.inn()
    t = chz.s.one('SELECT * FROM dispenser WHERE id=?', args[0])
    if not t:
        raise err(404, 'Задание не найдено')
    ok({'id': t['id'], 'name': t['name'], 'currentStatus': 'COMPLETED', 'createDate': t['created']})


@route('GET', r'api/v3/true-api/dispenser/results')
def dispenser_results(h, chz, query=None, **k):
    inn = h.inn()
    ids = [i for i in (query.get('task_ids') or query.get('taskIds') or '').split(',') if i]
    rows = [chz.s.one('SELECT * FROM dispenser WHERE id=?', i) for i in ids] if ids else \
        chz.s.q('SELECT * FROM dispenser WHERE inn=? ORDER BY created DESC LIMIT 50', inn)
    ok({'list': [{'id': r['result_id'], 'taskId': r['id'], 'available': 'AVAILABLE', 'name': r['name']}
                 for r in rows if r]})


@route('GET', r'api/v3/true-api/dispenser/results/([^/]+)/file')
def dispenser_file(h, chz, args=(), **k):
    import io as _io
    import zipfile
    t = chz.s.one('SELECT * FROM dispenser WHERE result_id=?', args[0])
    if not t:
        raise err(404, 'Результат выгрузки не найден')
    # пустое поле — без кавычек: 1С трактует "" как экранированную кавычку
    q = lambda v: '' if v in (None, '') else '"' + str(v).replace('"', '""') + '"'
    if 'DEVIATION' in (t['name'] or '').upper() or 'ОТКЛОН' in (t['name'] or '').upper():
        lines = ['Сведения об отклонениях', ','.join(q(c) for c in ('Вид отклонения', 'Результат проверки', 'Субъект',
                 'Адрес места фиксации отклонения', 'Регистрационный номер ККТ (из чека)', 'Нивелировано'))]
    else:   # список КИ на балансе: первая строка — заголовок отчёта, вторая — колонки
        lines = ['Список КИ на балансе', ','.join(q(c) for c in ('gtin', 'parent', 'status', 'emissionType',
                                                                    'packageType', 'requestedCis'))]
        for c in chz.s.q("SELECT * FROM codes WHERE owner_inn=? AND status='INTRODUCED'", t['inn']):
            et = json.loads(c['extra'] or '{}').get('emissionType') or 'LOCAL'
            lines.append(','.join(q(v) for v in (c['gtin'], c['parent'], c['status'], et,
                                                 c['package_type'] or 'UNIT', c['cis'])))
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('report.csv', '\n'.join(lines).encode('utf-8'))
    raise Resp(200, buf.getvalue(), 'application/zip')


# ---- Реестр согласий о предоставлении информации
@route('POST', r'api/v3/true-api/agreement-registry/agreement')
def agreement_create(h, chz, data=None, **k):
    inn = h.inn()
    aid = str(uuid.uuid4())
    chz.s.x('INSERT INTO agreements VALUES(?,?,?,?,?)', aid, inn, 'DRAFT', json.dumps(data or {}, ensure_ascii=False), iso())
    ok({'id': aid})


@route('GET', r'api/v3/true-api/agreement-registry/agreement/list')
def agreement_list(h, chz, **k):
    inn = h.inn()
    ok({'results': [{'id': a['id'], 'status': a['status'], 'createdAt': a['created'], **json.loads(a['body'])}
                    for a in chz.s.q('SELECT * FROM agreements WHERE inn=? ORDER BY created DESC', inn)]})


@route('GET', r'api/v3/true-api/agreement-registry/agreement/([^/]+)/trusted-inns')
def agreement_trusted(h, chz, args=(), **k):
    h.inn()
    a = chz.s.one('SELECT * FROM agreements WHERE id=?', args[0])
    b = json.loads(a['body']) if a else {}
    ok({'everyonePermitted': b.get('everyonePermitted', False), 'expirationDate': b.get('expirationDate'),
        'trustedInns': b.get('trustedInns', [])})


@route('GET', r'api/v3/true-api/agreement-registry/([^/]+)/print-form')
def agreement_print(h, chz, args=(), **k):
    h.inn()
    text(f'<?xml version="1.0" encoding="UTF-8"?><agreement id="{args[0]}">Согласие (эмулятор ЧЗ)</agreement>')


@route('POST', r'api/v3/true-api/agreement-registry/publish')
def agreement_publish(h, chz, data=None, **k):
    h.inn()
    did = (data or {}).get('documentId')
    chz.s.x("UPDATE agreements SET status='PUBLISHED' WHERE id=?", did)
    ok({'documentId': did, 'status': 'PUBLISHED'})


@route('POST', r'api/v3/true-api/agreement-registry/cancellation')
def agreement_cancel(h, chz, data=None, **k):
    h.inn()
    cid = str(uuid.uuid4())
    did = (data or {}).get('agreementId') or (data or {}).get('documentId') or (data or {}).get('id')
    if did:
        chz.s.x("UPDATE agreements SET status='CANCELLED' WHERE id=?", did)
    ok({'id': cid})


@route('POST', r'api/v3/facade/agreement-registry/agreement/decline')
def agreement_decline(h, chz, data=None, **k):
    h.inn()
    ok({})


# ---- Локальный модуль ЧЗ (ЛМ ЧЗ): касса, разрешительный режим офлайн. В 1С указывается адрес эмулятора.
@route('GET', r'api/v[12]/status')
def lm_status(h, chz, **k):
    ok({'status': 'ready', 'operationMode': 'active', 'version': 'chz-emulator', 'lastSync': int(time.time() * 1000),
        'inst': 'emu-lm', 'requiresDownload': False})


@route('POST', r'api/v[12]/(init|changePassword)')
def lm_init(h, chz, **k):
    ok({'code': 0, 'description': 'ok'})


@route('GET', r'api/v1/config')
def lm_config(h, chz, **k):
    ok({'code': 0, 'productGroups': list(PG_IDS), 'mode': 'online'})


@route('POST', r'api/v1/groups')
def lm_groups(h, chz, **k):
    ok({'code': 0, 'description': 'ok'})


@route('POST', r'api/v[12]/cis/outCheck')
def lm_out_check(h, chz, data=None, **k):
    d = data or {}
    codes = d.get('cis_list') or ([d['cis']] if d.get('cis') else [])
    try:
        codes_check(h, chz, data={'codes': codes})
    except Resp as r:
        res = r.body
    ok({'code': 0, 'description': 'ok', 'results': [{'codes': res['codes'], 'reqId': res['reqId'],
                                                       'reqTimestamp': res['reqTimestamp']}]})


@route('POST', r'api/v[12]/cis/(sell|return)')
def lm_sell(h, chz, data=None, args=(), **k):
    for v in (data or {}).get('cis_list') or []:
        c = chz.s.find_code(v if isinstance(v, str) else v.get('cis', ''))
        if c:
            chz.s.update_code(c['cis'], status='RETIRED' if args[0] == 'sell' else 'INTRODUCED', status_ex=None)
    ok({'code': 0, 'description': 'ok'})


# ---- Национальный каталог (api.integrators.nk.crptech.ru / апи.национальный-каталог.рф), API v3
def nk_key(query):
    if not query.get('apikey'):
        raise Resp(401, {'apiversion': 3, 'error': {'code': 401, 'message': 'Не передан apikey'}})


@route('GET', r'_emu/epf/([A-Za-z_А-Яа-я]+)\.epf', admin=True)
def ui_epf(h, chz, args=(), **k):
    """Обработки 1С: ЭЧЗ_НастройкаПодключения, ЭЧЗ_ВыгрузкаВНК."""
    from urllib.parse import unquote
    path = os.path.join(HERE, 'epf', unquote(args[0]) + '.epf')
    if not os.path.isfile(path):
        raise err(404, 'Нет такой обработки')
    with open(path, 'rb') as f:
        raise Resp(200, f.read(), 'application/octet-stream')


@route('GET', r'_emu/extension\.cfe', admin=True)
def ui_extension(h, chz, **k):
    """Расширение 1С ЧЗ_БезПодписи (подпись-заглушка, только в тестовом контуре ИС МП)."""
    with open(os.path.join(HERE, 'extension', 'ЧЗ_БезПодписи.cfe'), 'rb') as f:
        raise Resp(200, f.read(), 'application/octet-stream')


def nk_view(c):
    return {'good_id': c['good_id'], 'good_name': c['name'], 'brand_name': c['brand'] or None,
            'identified_by': [{'value': c['gtin'], 'type': 'gtin', 'multiplier': 1, 'level': 'trade-unit'}],
            'good_status': c['status'], 'good_detailed_status': [c['status']], 'tnved': c['tnved'] or None,
            'producer_inn': c['inn'], 'categories': [{'cat_id': PG_IDS.get(c['pg'], 0), 'cat_name': c['pg']}],
            'good_signed': c['status'] == 'published', 'updated_date': c['created']}


def nk_select(chz, gtins=(), ids=()):
    rows = []
    for g in gtins:
        c = chz.s.nk_card(g)
        rows += [c] if c else []
    for i in ids:
        c = chz.s.one('SELECT * FROM nk WHERE good_id=?', int(i))
        rows += [c] if c else []
    return rows


@route('GET', r'v3/feed-product|v3/product')
def nk_feed_product(h, chz, query=None, **k):
    nk_key(query)
    gtins = [g for g in (query.get('gtins') or query.get('gtin') or '').split(';') if g]
    ids = [i for i in (query.get('good_ids') or query.get('good_id') or '').split(';') if i]
    rows = nk_select(chz, gtins, ids) if gtins or ids else chz.s.q('SELECT * FROM nk ORDER BY good_id LIMIT 1000')
    ok({'apiversion': 3, 'result': [nk_view(c) for c in rows]})


@route('POST', r'v3/feed-product-document')
def nk_feed_document(h, chz, query=None, data=None, **k):
    nk_key(query)
    d = data or {}
    xmls, errors = [], []
    asked = [(g, None) for g in d.get('gtins') or []] + [(None, i) for i in d.get('goodIds') or []]
    for g, i in asked:
        found = nk_select(chz, [g]) if g else nk_select(chz, ids=[i])
        c = found[0] if found else None
        if not c:
            errors.append({'gtin': g, 'goodId': i, 'message': 'Товар не найден в Национальном каталоге'})
        elif c['status'] == 'published':
            errors.append({'gtin': c['gtin'], 'goodId': c['good_id'], 'message': 'Карточка уже подписана'})
        else:
            xml = (f'<?xml version="1.0" encoding="UTF-8"?><good id="{c["good_id"]}"><gtin>{c["gtin"]}</gtin>'
                   f'<name>{c["name"]}</name><tnved>{c["tnved"] or ""}</tnved></good>')
            xmls.append({'goodId': c['good_id'], 'gtin': c['gtin'], 'xml': base64.b64encode(xml.encode()).decode()})
    ok({'apiversion': 3, 'result': {'xmls': xmls, 'errors': errors}})


@route('POST', r'v3/feed-product-sign-pkcs')
def nk_sign(h, chz, query=None, data=None, **k):
    nk_key(query)
    signed, errors = [], []
    for it in data if isinstance(data, list) else []:
        c = chz.s.one('SELECT * FROM nk WHERE good_id=?', int(it.get('goodId') or 0))
        if not c:
            errors.append({'goodId': it.get('goodId'), 'message': 'Товар не найден'})
            continue
        chz.s.x("UPDATE nk SET status='published' WHERE good_id=?", c['good_id'])   # подпись не проверяется
        signed.append(c['good_id'])
    ok({'apiversion': 3, 'result': {'signed': signed, 'errors': errors}})


# ---- веб-интерфейс и админ-API
@route('GET', r'_emu/nk', admin=True)
def ui_nk(h, chz, query=None, **k):
    q = query.get('q')
    rows = chz.s.q('SELECT * FROM nk WHERE gtin LIKE ? OR name LIKE ? ORDER BY good_id DESC LIMIT 1000',
                   f'%{q}%', f'%{q}%') if q else chz.s.q('SELECT * FROM nk ORDER BY good_id DESC LIMIT 1000')
    ok(rows)


@route('POST', r'_emu/nk', admin=True)
def ui_nk_put(h, chz, data=None, **k):
    d = data or {}
    gtin = d.get('gtin') or make_gtin()
    if not re.fullmatch(r'\d{8,14}', chz.s.norm_gtin(gtin)):
        raise err(400, f'GTIN должен быть из 8–14 цифр: {gtin}')
    ok(chz.s.nk_put(gtin, d.get('name'), d.get('pg'), d.get('inn'), d.get('tnved') or '', d.get('brand') or '',
                    d.get('status') or 'published'))


@route('POST', r'_emu/nk/import', admin=True)
def ui_nk_import(h, chz, data=None, **k):
    """CSV/TSV: GTIN;Наименование;ТГ;ТН ВЭД;Бренд;ИНН;Статус. Строки без GTIN в первой колонке пропускаются."""
    n, bad = 0, []
    for line in (data or {}).get('csv', '').splitlines():
        cols = [c.strip() for c in re.split(r'[;\t]', line)]
        if not line.strip():
            continue
        if not re.fullmatch(r'\d{8,14}', cols[0]):
            bad.append(line)
            continue
        cols += [''] * (7 - len(cols))
        chz.s.nk_put(cols[0], cols[1] or None, cols[2] or None, cols[5] or None, cols[3], cols[4], cols[6] or 'published')
        n += 1
    ok({'imported': n, 'skipped': bad})


@route('POST', r'_emu/nk/delete', admin=True)
def ui_nk_delete(h, chz, data=None, **k):
    chz.s.x('DELETE FROM nk WHERE gtin=?', chz.s.norm_gtin((data or {}).get('gtin')))
    ok({})


@route('GET', r'_emu/code/(.+)', admin=True)
def ui_code_info(h, chz, args=(), **k):
    from urllib.parse import unquote
    c = chz.s.find_code(unquote(args[0]))
    if not c:
        raise err(404, 'Код не найден')
    docs = chz.s.q('SELECT id, type, status, number, received FROM docs WHERE body LIKE ? ORDER BY received',
                   '%' + c['cis'] + '%')
    ok({'code': c, 'cisInfo': chz.cis_info(c), 'nk': chz.s.nk_card(c['gtin']) if c['gtin'] else None, 'docs': docs})
@route('GET', r'', admin=True)
def ui(h, chz, **k):
    with open(os.path.join(HERE, 'ui.html'), 'rb') as f:
        raise Resp(200, f.read(), 'text/html; charset=utf-8')


@route('GET', r'_emu/ca\.crt', admin=True)
def ui_ca(h, chz, **k):
    with open(os.path.join(h.server.certdir, 'ca.crt'), 'rb') as f:
        raise Resp(200, f.read(), 'application/x-x509-ca-cert')


@route('GET', r'_emu/state', admin=True)
def ui_state(h, chz, query=None, **k):
    lim = int(query.get('limit') or 200)
    codes_q, a = 'SELECT * FROM codes', []
    if query.get('q'):
        codes_q += ' WHERE cis LIKE ? OR gtin LIKE ? OR owner_inn LIKE ? OR status LIKE ?'
        a = ['%' + query['q'] + '%'] * 4
    ok({'settings': chz.s.settings(), 'pgs': PG_NAMES,
        'stats': chz.s.q('SELECT status, COUNT(*) n FROM codes GROUP BY status'),
        'codes': chz.s.q(codes_q + ' ORDER BY rowid DESC LIMIT ?', *a, lim),
        'docs': [d | {'body': None, 'raw': None} for d in chz.s.q('SELECT * FROM docs ORDER BY received DESC LIMIT ?', lim)],
        'orders': chz.s.q('SELECT o.*, (SELECT SUM(total) FROM buffers b WHERE b.order_id=o.id) total, '
                          '(SELECT SUM(issued) FROM buffers b WHERE b.order_id=o.id) issued FROM orders o '
                          'ORDER BY created DESC LIMIT ?', lim),
        'reports': chz.s.q('SELECT id, kind, status, created, errors FROM reports ORDER BY created DESC LIMIT ?', lim),
        'participants': chz.s.q('SELECT * FROM participants'),
        'nk': chz.s.q('SELECT * FROM nk ORDER BY good_id DESC LIMIT ?', lim),
        'log': list(reversed(chz.log[-lim:]))})


@route('GET', r'_emu/doc/([^/]+)', admin=True)
def ui_doc(h, chz, args=(), **k):
    d = chz.s.one('SELECT * FROM docs WHERE id=?', args[0])
    ok(d or {})


@route('POST', r'_emu/settings', admin=True)
def ui_settings(h, chz, data=None, **k):
    for key, v in (data or {}).items():
        if key in DEFAULT_SETTINGS:
            chz.s.set_setting(key, v)
    ok(chz.s.settings())


@route('POST', r'_emu/codes', admin=True)
def ui_codes(h, chz, data=None, **k):
    """Сгенерировать коды: {gtin, pg, count, status, owner_inn, aggregate: true}"""
    d = data or {}
    pg = d.get('pg') or 'lp'
    gtin = d.get('gtin') or make_gtin()
    owner = d.get('owner_inn') or chz.s.setting('default_inn')
    status = d.get('status') or 'INTRODUCED'
    extra = json.dumps({'emissionType': emission_type({'releaseMethodType': d.get('release') or 'PRODUCTION'})})
    if not chz.s.nk_card(gtin):
        chz.s.nk_put(gtin, d.get('name'), pg, d.get('producer_inn') or owner, d.get('tnved') or '', d.get('brand') or '')
    rows, out = [], []
    short = d['short'] if 'short' in d else chz.s.setting('short_codes')
    for _ in range(int(d.get('count') or 1)):
        cis, full = make_code(gtin, pg, short, d.get('template_id'))
        out.append(full)
        rows.append((cis, full, gtin, pg, status, None, owner, d.get('producer_inn') or owner, 'UNIT', None,
                     iso(), iso() if status != 'EMITTED' else None, iso() if status in ('INTRODUCED', 'RETIRED') else None,
                     d.get('production_date') or iso(), d.get('expiration_date'), None, None, extra))
    chz.s.many('INSERT OR REPLACE INTO codes VALUES(' + ','.join('?' * 18) + ')', rows)
    box = None
    if d.get('aggregate'):
        box = make_sscc()
        chz.s.upsert_code(cis=box, full=box, gtin=None, pg=pg, status=status, status_ex=None, owner_inn=owner,
                          producer_inn=owner, package_type='LEVEL2', parent=None, emission_date=iso(),
                          applied_date=iso(), introduced_date=iso(), production_date=None, expiration_date=None,
                          last_doc=None, order_id=None, extra=None)
        for r in rows:
            chz.s.update_code(r[0], parent=box)
    ok({'gtin': gtin, 'codes': out, 'box': box})


@route('POST', r'_emu/code', admin=True)
def ui_code_edit(h, chz, data=None, **k):
    d = dict(data or {})
    cis = d.pop('cis')
    allowed = {'status', 'status_ex', 'owner_inn', 'parent', 'expiration_date', 'production_date', 'pg', 'gtin',
               'package_type'}
    d = {k2: (v or None) for k2, v in d.items() if k2 in allowed}
    if d:
        chz.s.update_code(cis, **d)
    ok(chz.s.one('SELECT * FROM codes WHERE cis=?', cis))


@route('POST', r'_emu/codes/status', admin=True)
def ui_codes_status(h, chz, data=None, **k):
    """Массовая смена статуса: {cises: [...], status}. Даты нанесения и ввода заполняются, если пусты."""
    d = data or {}
    status = d.get('status')
    if status not in ('EMITTED', 'APPLIED', 'INTRODUCED', 'RETIRED', 'WRITTEN_OFF', 'DISAGGREGATED'):
        raise err(400, f'Недопустимый статус: {status}')
    n = 0
    for cis in d.get('cises') or []:
        c = chz.s.one('SELECT * FROM codes WHERE cis=?', cis)
        if not c:
            continue
        upd = {'status': status, 'status_ex': None}
        if status in ('APPLIED', 'INTRODUCED', 'RETIRED') and not c['applied_date']:
            upd['applied_date'] = iso()
        if status in ('INTRODUCED', 'RETIRED') and not c['introduced_date']:
            upd['introduced_date'] = iso()
        chz.s.update_code(cis, **upd)
        n += 1
    ok({'updated': n, 'status': status})


@route('POST', r'_emu/incoming', admin=True)
def ui_incoming(h, chz, data=None, **k):
    """Входящая отгрузка от поставщика: {sender_inn, receiver_inn, codes:[...] | gtin+count, pg}"""
    d = data or {}
    sender = d.get('sender_inn') or '7700000002'
    receiver = d.get('receiver_inn') or chz.s.setting('default_inn')
    codes = d.get('codes') or []
    if not codes:
        gen = {'gtin': d.get('gtin'), 'pg': d.get('pg') or 'lp', 'count': d.get('count') or 2,
               'owner_inn': sender, 'status': 'INTRODUCED'}
        try:
            ui_codes(h, chz, data=gen)
        except Resp as r:
            codes = r.body['codes']
    num = d.get('number') or ('ЭМ-' + rnd(6, string.digits))
    body = {'document_num': num, 'document_date': dt.date.today().isoformat(), 'turnover_type': 'SELLING',
            'transfer_date': dt.date.today().isoformat(), 'trade_participant_inn_sender': sender,
            'trade_participant_inn_receiver': receiver,
            'products': [{'uit_code': c.split(GS)[0], 'product_cost': 10000, 'product_tax': 2000} for c in codes]}
    doc_id = str(uuid.uuid4())
    for c in codes:
        x = chz.s.find_code(c)
        if x:
            chz.s.update_code(x['cis'], owner_inn=sender, status='INTRODUCED', status_ex='WAIT_SHIPMENT', last_doc=doc_id)
    chz.s.x('INSERT INTO docs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', doc_id, 'LP_SHIP_GOODS', d.get('pg') or 'lp',
            'MANUAL', 'WAIT_ACCEPTANCE', sender, receiver, num, body['document_date'], iso(), 0,
            json.dumps(body, ensure_ascii=False), json.dumps(body, ensure_ascii=False), '[]', None)
    ok({'id': doc_id, 'number': num, 'codes': codes})


@route('POST', r'_emu/participant', admin=True)
def ui_participant(h, chz, data=None, **k):
    d = data or {}
    chz.s.x('INSERT OR REPLACE INTO participants VALUES(?,?,?,?)', d['inn'], d.get('name') or f"Участник {d['inn']}",
            d.get('status') or 'REGISTERED', json.dumps(d.get('pgs') or list(PG_IDS)))
    ok(chz.s.participant(d['inn']))


@route('POST', r'_emu/autotest', admin=True)
def ui_autotest(h, chz, data=None, **k):
    import scenario
    t0 = time.time()
    steps = scenario.run(h.server.server_address[1], os.path.join(h.server.certdir, 'ca.crt'),
                         pg=(data or {}).get('pg') or 'lp')
    ok({'ok': all(s['ok'] for s in steps) and len(steps) == SCENARIO_STEPS, 'steps': steps,
        'всего': SCENARIO_STEPS, 'мс': int((time.time() - t0) * 1000)})


SCENARIO_STEPS = 13


@route('POST', r'_emu/reset', admin=True)
def ui_reset(h, chz, **k):
    for t in ('codes', 'docs', 'orders', 'buffers', 'blocks', 'reports', 'tokens', 'mods'):
        chz.s.x(f'DELETE FROM {t}')
    with chz.log_lock:
        chz.log.clear()
        path = os.path.join(chz.logdir, 'requests.log')
        if os.path.exists(path):   # старый журнал — в архив, иначе он вернётся после перезапуска
            os.replace(path, os.path.join(chz.logdir, time.strftime('requests-%Y%m%d-%H%M%S.log')))
    ok({'reset': True})


# ---------------------------------------------------------------- сертификаты и запуск

def find_openssl():
    for p in (shutil.which('openssl'), r'C:\Program Files\Git\usr\bin\openssl.exe',
              r'C:\Program Files\Git\mingw64\bin\openssl.exe'):
        if p and os.path.exists(p):
            return p
    raise SystemExit('Не найден openssl (поставьте Git for Windows или OpenSSL)')


def ensure_certs(certdir):
    os.makedirs(certdir, exist_ok=True)
    ca_key, ca_crt = os.path.join(certdir, 'ca.key'), os.path.join(certdir, 'ca.crt')
    key, crt = os.path.join(certdir, 'server.key'), os.path.join(certdir, 'server.crt')
    san_file = os.path.join(certdir, 'san.txt')
    san_now = ','.join(SAN)
    same_san = os.path.exists(san_file) and open(san_file).read() == san_now
    if os.path.exists(crt) and os.path.exists(ca_crt) and same_san:
        return crt, key
    try:
        _certs_cryptography(ca_key, ca_crt, key, crt)
    except ImportError:
        _certs_openssl(certdir, ca_key, ca_crt, key, crt)
    with open(san_file, 'w') as f:
        f.write(san_now)
    return crt, key


def _certs_cryptography(ca_key, ca_crt, key, crt):
    """Корневой CA создаётся один раз (его доверяют в Windows), серверный перевыпускается при смене SAN."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    import ipaddress
    pem = serialization.Encoding.PEM
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    if os.path.exists(ca_crt) and os.path.exists(ca_key):
        ca_k = serialization.load_pem_private_key(open(ca_key, 'rb').read(), None)
        ca_c = x509.load_pem_x509_certificate(open(ca_crt, 'rb').read())
    else:
        ca_k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CHZ Emulator Root CA'),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'chz-emulator')])
        ca_c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_k.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(t0)
                .not_valid_after(t0 + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_k.public_key()), critical=False)
                .sign(ca_k, hashes.SHA256()))
        open(ca_key, 'wb').write(ca_k.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL,
                                                    serialization.NoEncryption()))
        open(ca_crt, 'wb').write(ca_c.public_bytes(pem))
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '*.crpt.ru')]))
         .issuer_name(ca_c.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(t0).not_valid_after(t0 + dt.timedelta(days=825))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_k.public_key()), critical=False)
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in SAN] +
                                                    [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
         .sign(ca_k, hashes.SHA256()))
    open(key, 'wb').write(k.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    open(crt, 'wb').write(c.public_bytes(pem) + ca_c.public_bytes(pem))   # цепочка: сервер + корень


def _certs_openssl(certdir, ca_key, ca_crt, key, crt):
    ossl = find_openssl()
    env = dict(os.environ, MSYS_NO_PATHCONV='1', MSYS2_ARG_CONV_EXCL='*')
    run = lambda *a: subprocess.run([ossl, *a], check=True, env=env, capture_output=True)
    if not os.path.exists(ca_crt):
        run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', ca_key, '-out', ca_crt, '-days', '3650',
            '-subj', '/CN=CHZ Emulator Root CA/O=chz-emulator', '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    ext = os.path.join(certdir, 'server.ext')
    with open(ext, 'w') as f:
        f.write('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n'
                'extendedKeyUsage=serverAuth\nsubjectAltName=' + ','.join('DNS:' + s for s in SAN) +
                ',IP:127.0.0.1\n')
    csr = os.path.join(certdir, 'server.csr')
    run('req', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', csr, '-subj', '/CN=*.crpt.ru')
    run('x509', '-req', '-in', csr, '-CA', ca_crt, '-CAkey', ca_key, '-CAcreateserial', '-out', crt,
        '-days', '825', '-sha256', '-extfile', ext)
    with open(crt, 'a') as f, open(ca_crt) as c:      # цепочка: сервер + корень
        f.write(c.read())
    return crt, key


def migrate_legacy_data(data_dir):
    """Первый запуск с DATA_DIR: копируем data рядом с exe (база, сертификаты, журнал), чтобы не потерять
    коды и не получить новый, недоверенный корневой сертификат. Возвращает откуда скопировано или None."""
    if os.path.exists(os.path.join(data_dir, 'chz.db')) or os.path.exists(os.path.join(data_dir, 'certs', 'ca.crt')):
        return None
    src = LEGACY_DATA_DIR
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(data_dir))             or not os.path.exists(os.path.join(src, 'certs', 'ca.crt')):
        return None
    shutil.copytree(src, data_dir, dirs_exist_ok=True)
    return src


def load_log(chz, keep=500):
    """Журнал запросов веб-интерфейса — из файла, чтобы он переживал перезапуск."""
    try:
        with open(os.path.join(chz.logdir, 'requests.log'), encoding='utf-8') as f:
            lines = f.readlines()[-keep:]
    except OSError:
        return
    for line in lines:
        try:
            chz.log.append(json.loads(line))
        except ValueError:
            pass


def make_server(port, data_dir, verbose=False, bind='127.0.0.1'):
    certdir = os.path.join(data_dir, 'certs')
    crt, key = ensure_certs(certdir)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(crt, key)
    chz = Chz(Store(os.path.join(data_dir, 'chz.db')))
    chz.logdir = os.path.join(data_dir, 'logs')
    os.makedirs(chz.logdir, exist_ok=True)
    load_log(chz)
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    srv.tls, srv.chz, srv.verbose, srv.certdir = tls, chz, verbose, certdir
    return srv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=3128)
    ap.add_argument('--bind', default='127.0.0.1', help='0.0.0.0 — пустить 1С с других машин')
    ap.add_argument('--data', default=DATA_DIR)
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--no-browser', action='store_true', help='не открывать веб-интерфейс')
    a = ap.parse_args()
    migrated = migrate_legacy_data(a.data)
    try:
        srv = make_server(a.port, a.data, a.verbose, a.bind)
    except OSError as e:
        print(f'Порт {a.port} занят — эмулятор уже запущен? ({e})')
        if getattr(sys, 'frozen', False):
            input('Enter — закрыть')
        return
    url = f'http://127.0.0.1:{a.port}/'
    print(f'Эмулятор ЧЗ: прокси для 1С {a.bind}:{a.port}, веб-интерфейс {url}  (данные: {a.data})')
    if migrated:
        print(f'Данные перенесены из {migrated} (старая папка не тронута)')
    print(f'Корневой сертификат: {os.path.join(srv.certdir, "ca.crt")}')
    print('Окно не закрывайте, пока работаете с 1С. Остановка — Ctrl+C.')
    if not a.no_browser:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
