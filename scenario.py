"""Автосценарий: полный цикл ЧЗ через прокси и TLS эмулятора, как его проходит 1С.

Используется кнопкой «Автосценарий» в веб-интерфейсе и самопроверкой test_emulator.py.
"""
import base64, http.client, json, ssl, time

import chz_emulator as E

GS = '\x1d'
SELLER, BUYER = '7799000001', '7799000002'
SUZ_HOST, GIS_HOST, CDN_HOST = 'suz.sandbox.crptech.ru', 'markirovka.sandbox.crptech.ru', 'cdn01.sandbox.crptech.ru'


class Client:
    def __init__(self, port, cafile):
        self.port, self.ctx, self.token, self.headers = port, ssl.create_default_context(cafile=cafile), None, {}

    def call(self, method, path, body=None, host=GIS_HOST, headers=None, raw=None):
        c = http.client.HTTPSConnection('127.0.0.1', self.port, context=self.ctx, timeout=15)
        c.set_tunnel(host, 443)
        h = {'Content-Type': 'application/json; charset=utf-8', **self.headers, **(headers or {})}
        if self.token:
            h.setdefault('Authorization', 'Bearer ' + self.token)
        data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        c.request(method, '/' + path, data, h)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text


def run(port, cafile, pg='lp'):
    """Возвращает список шагов {шаг, ok, детали}. Шаги после первого провала не выполняются."""
    cl = Client(port, cafile)
    steps, ctx = [], {}

    def step(name):
        def deco(fn):
            if steps and not steps[-1]['ok']:
                return fn
            t0 = time.time()
            try:
                detail = fn()
                steps.append({'шаг': name, 'ok': True, 'детали': detail, 'мс': int((time.time() - t0) * 1000)})
            except Exception as e:  # шаг упал — показываем почему, остальные пропускаем
                steps.append({'шаг': name, 'ok': False, 'детали': f'{type(e).__name__}: {e}',
                              'мс': int((time.time() - t0) * 1000)})
            return fn
        return deco

    def need(cond, msg):
        if not cond:
            raise AssertionError(msg)

    def doc(t, content, inn):
        cl.token = ctx['tok_' + inn]
        b = base64.b64encode(json.dumps(content, ensure_ascii=False).encode()).decode()
        st, did = cl.call('POST', f'api/v3/true-api/lk/documents/create?pg={pg}',
                          {'document_format': 'MANUAL', 'product_document': b, 'type': t, 'signature': 'c2ln'})
        need(st == 200, f'documents/create → {st} {did}')
        for _ in range(30):                     # ждём, пока документ выйдет из IN_PROGRESS
            st, info = cl.call('GET', f'api/v4/true-api/doc/{did}/info')
            if info[0]['status'] != 'IN_PROGRESS':
                return did, info[0]
            time.sleep(0.2)
        return did, info[0]

    def statuses(codes):
        cl.token = ctx['tok_' + SELLER]
        st, info = cl.call('POST', 'api/v3/true-api/cises/info', codes)
        need(st == 200, f'cises/info → {st}')
        return info

    @step('Авторизация: auth/key → simpleSignIn (продавец и покупатель)')
    def _():
        cl.token = None
        for inn in (SELLER, BUYER):
            st, key = cl.call('GET', 'api/v3/true-api/auth/key')
            need(st == 200 and key.get('uuid'), f'auth/key → {st}')
            st, tok = cl.call('POST', 'api/v3/true-api/auth/simpleSignIn',
                              {'uuid': key['uuid'], 'data': base64.b64encode(f'CMS {inn}'.encode()).decode(), 'inn': inn})
            need(st == 200 and tok.get('token'), f'simpleSignIn → {st} {tok}')
            ctx['tok_' + inn] = tok['token']
        return 'токены получены, ИНН зашит в JWT'

    @step('Участник оборота: participants/{inn}')
    def _():
        cl.token = ctx['tok_' + SELLER]
        st, p = cl.call('GET', f'api/v3/true-api/participants/{SELLER}', host='sandbox.crptech.ru')
        need(st == 200 and p.get('productGroups'), f'participants → {st}')
        return f"{p['status']}, товарных групп: {len(p['productGroups'])}"

    @step('СУЗ: идентификатор соединения и токен СУЗ')
    def _():
        cl.token = None
        st, con = cl.call('POST', 'api/v3/integration/connection?omsId=emu-oms', {}, host=SUZ_HOST)
        need(con.get('status') == 'SUCCESS', f'connection → {con}')
        st, tok = cl.call('POST', f"api/v3/true-api/auth/simpleSignIn/{con['omsConnection']}",
                          {'uuid': 'x', 'data': 'x', 'inn': SELLER})
        need(tok.get('token'), f'simpleSignIn СУЗ → {tok}')
        ctx['suz'] = {'clientToken': tok['token']}
        st, ping = cl.call('GET', 'api/v3/ping?omsId=emu-oms', host=SUZ_HOST, headers=ctx['suz'])
        need(st == 200, f'ping → {st}')
        return f"omsConnection {con['omsConnection'][:8]}…"

    @step('СУЗ: заказ на эмиссию 5 кодов и готовность пула')
    def _():
        ctx['gtin'] = E.make_gtin()
        st, o = cl.call('POST', 'api/v3/order?omsId=emu-oms', {'productGroup': pg, 'products': [
            {'gtin': ctx['gtin'], 'quantity': 5, 'serialNumberType': 'OPERATOR', 'templateId': 10, 'cisType': 'UNIT'}]},
            host=SUZ_HOST, headers=ctx['suz'])
        need(st == 200 and o.get('orderId'), f'order → {st} {o}')
        ctx['order'] = o['orderId']
        for _ in range(50):
            st, s = cl.call('GET', f"api/v3/order/status?omsId=emu-oms&orderId={o['orderId']}&gtin={ctx['gtin']}",
                            host=SUZ_HOST, headers=ctx['suz'])
            if s[0]['bufferStatus'] == 'ACTIVE':
                break
            time.sleep(0.2)
        need(s[0]['bufferStatus'] == 'ACTIVE', f'пул не готов: {s}')
        return f"заказ {o['orderId'][:8]}…, GTIN {ctx['gtin']}, пул ACTIVE"

    @step('СУЗ: получение кодов (пул исчерпан)')
    def _():
        st, c = cl.call('GET', f"api/v3/codes?omsId=emu-oms&orderId={ctx['order']}&gtin={ctx['gtin']}&quantity=5",
                        host=SUZ_HOST, headers=ctx['suz'])
        need(st == 200 and len(c.get('codes', [])) == 5, f'codes → {st} {c}')
        ctx['full'] = c['codes']
        ctx['cis'] = [x.split(GS)[0] for x in c['codes']]
        st, s = cl.call('GET', f"api/v3/order/status?omsId=emu-oms&orderId={ctx['order']}&gtin={ctx['gtin']}",
                        host=SUZ_HOST, headers=ctx['suz'])
        need(s[0]['bufferStatus'] == 'EXHAUSTED', f'ожидали EXHAUSTED: {s}')
        return ctx['full'][0].replace(GS, '<GS>')

    @step('СУЗ: отчёт о нанесении → коды APPLIED')
    def _():
        st, r = cl.call('POST', 'api/v3/utilisation?omsId=emu-oms', {'sntins': ctx['full'], 'usageType': 'VERIFIED'},
                        host=SUZ_HOST, headers=ctx['suz'])
        need(st == 200, f'utilisation → {st} {r}')
        for _ in range(30):
            st, ri = cl.call('GET', f"api/v3/report/info?omsId=emu-oms&reportId={r['reportId']}",
                             host=SUZ_HOST, headers=ctx['suz'])
            if ri['reportStatus'] != 'PENDING':
                break
            time.sleep(0.2)
        need(ri['reportStatus'] == 'SENT', f'отчёт: {ri}')
        info = statuses(ctx['cis'])
        need(all(i['cisInfo']['status'] == 'APPLIED' for i in info), f'статусы: {info}')
        return 'отчёт SENT, 5 кодов APPLIED'

    @step('ГИС МТ: ввод в оборот LP_INTRODUCE_GOODS')
    def _():
        _, d = doc('LP_INTRODUCE_GOODS', {'participant_inn': SELLER, 'producer_inn': SELLER, 'owner_inn': SELLER,
                                          'production_date': '2026-10-01', 'production_type': 'OWN_PRODUCTION',
                                          'products': [{'uit_code': c, 'tnved_code': '6403990000'} for c in ctx['cis']]},
                   SELLER)
        need(d['status'] == 'CHECKED_OK', f'документ: {d}')
        info = statuses(ctx['cis'])
        need(all(i['cisInfo']['status'] == 'INTRODUCED' for i in info), f'статусы: {info}')
        return 'CHECKED_OK, 5 кодов INTRODUCED'

    @step('ГИС МТ: агрегация двух кодов в короб SSCC')
    def _():
        ctx['sscc'] = E.make_sscc()
        _, d = doc('AGGREGATION_DOCUMENT', {'participantId': SELLER, 'aggregationUnits': [
            {'unitSerialNumber': ctx['sscc'], 'aggregationType': 'AGGREGATION', 'sntins': ctx['cis'][:2]}]}, SELLER)
        need(d['status'] == 'CHECKED_OK', f'документ: {d}')
        info = statuses([ctx['sscc']])
        need(sorted(info[0]['cisInfo'].get('child', [])) == sorted(ctx['cis'][:2]), f'короб: {info}')
        return f"короб {ctx['sscc']}, вложено 2"

    @step('ГИС МТ: отгрузка короба покупателю LP_SHIP_GOODS')
    def _():
        _, d = doc('LP_SHIP_GOODS', {'document_num': 'АВТО-1', 'document_date': time.strftime('%Y-%m-%d'),
                                     'trade_participant_inn_sender': SELLER, 'trade_participant_inn_receiver': BUYER,
                                     'products': [{'uitu_code': ctx['sscc']}]}, SELLER)
        need(d['status'] == 'WAIT_ACCEPTANCE', f'документ: {d}')
        cl.token = ctx['tok_' + BUYER]
        st, lst = cl.call('GET', 'api/v4/true-api/doc/list?input=true&documentStatus=WAIT_ACCEPTANCE')
        need(any(x['number'] == 'АВТО-1' for x in lst['results']), f'у покупателя нет входящего: {lst}')
        return 'WAIT_ACCEPTANCE, покупатель видит входящий документ'

    @step('ГИС МТ: приёмка покупателем LP_ACCEPT_GOODS → смена владельца')
    def _():
        _, d = doc('LP_ACCEPT_GOODS', {'trade_participant_inn_sender': SELLER, 'trade_participant_inn_receiver': BUYER,
                                       'document_number': 'АВТО-1', 'acceptance_date': time.strftime('%Y-%m-%d'),
                                       'products': [{'uitu_code': ctx['sscc'], 'accept_type': True}]}, BUYER)
        need(d['status'] == 'CHECKED_OK', f'документ: {d}')
        info = statuses(ctx['cis'][:2] + [ctx['sscc']])
        need(all(i['cisInfo']['ownerInn'] == BUYER for i in info), f'владельцы: {info}')
        return f'владелец короба и вложений — {BUYER}'

    @step('ГИС МТ: вывод из оборота (розница) и повторный вывод отклоняется')
    def _():
        _, d = doc('LK_RECEIPT', {'inn': SELLER, 'action': 'RETAIL', 'products': [{'cis': ctx['cis'][4]}]}, SELLER)
        need(d['status'] == 'CHECKED_OK', f'документ: {d}')
        _, d = doc('LK_RECEIPT', {'inn': SELLER, 'action': 'RETAIL', 'products': [{'cis': ctx['cis'][4]}]}, SELLER)
        need(d['status'] == 'CHECKED_NOT_OK', f'повтор не отклонён: {d}')
        return 'RETIRED; повтор → CHECKED_NOT_OK: ' + d['errors'][0]

    @step('Разрешительный режим: токен, CDN-площадки, codes/check')
    def _():
        cl.token = None
        st, tok = cl.call('POST', 'api/v3/true-api/auth/permissive-access', {'data': base64.b64encode(f'CMS {SELLER}'.encode()).decode()})
        need(tok.get('access_token'), f'permissive-access → {tok}')
        api = {'X-API-KEY': tok['access_token']}
        st, cdn = cl.call('GET', 'api/v4/true-api/cdn/info', headers=api)
        need(st == 200 and cdn['hosts'], f'cdn/info → {st} {cdn}')
        st, chk = cl.call('POST', 'api/v4/true-api/codes/check', {'codes': [ctx['full'][2]]}, host=CDN_HOST, headers=api)
        c = chk['codes'][0]
        need(c['found'] and c['realizable'] and c['isOwner'], f'codes/check: {c}')
        return f"площадок {len(cdn['hosts'])}, код можно продавать"

    return steps
