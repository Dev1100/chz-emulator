"""Самопроверка эмулятора на временной базе. Запуск: python test_emulator.py"""
import http.client, json, os, shutil, socket, tempfile, threading

import chz_emulator as E
import scenario

GS = '\x1d'


def main():
    tmp = tempfile.mkdtemp()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    srv = E.make_server(port, tmp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ca = os.path.join(tmp, 'certs', 'ca.crt')

    # автосценарий целиком — через админ-API, как кнопка в интерфейсе
    adm = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    adm.request('POST', '/_emu/autotest', json.dumps({'pg': 'shoes'}), {'Content-Type': 'application/json'})
    res = json.loads(adm.getresponse().read())
    for st in res['steps']:
        print('OK ' if st['ok'] else 'ERR', st['шаг'], '—', st['детали'])
    assert res['ok'], 'автосценарий не прошёл'
    assert len(res['steps']) == E.SCENARIO_STEPS

    cl = scenario.Client(port, ca)
    INN = scenario.SELLER
    cl.token = srv.chz.jwt(INN)

    # код со скобками и с криптохвостом находится так же, как короткий
    adm.request('POST', '/_emu/codes', json.dumps({'pg': 'tobacco', 'count': 2, 'status': 'APPLIED', 'owner_inn': INN}),
                {'Content-Type': 'application/json'})
    full = json.loads(adm.getresponse().read())['codes']
    packs = [c.split(GS)[0] for c in full]
    br = '(01)' + packs[0][2:16] + '(21)' + packs[0][18:]
    st, info = cl.call('POST', 'api/v3/true-api/cises/info', [br, full[1].replace(GS, '')])
    assert [i['cisInfo']['cis'] for i in info] == packs, info

    # МОТП: агрегация XML в multipart-форме
    block = E.make_sscc()
    xml = ('<?xml version="1.0" encoding="UTF-8"?><aggregation_document><participant_id>%s</participant_id>'
           '<packings_list><packing><kitu>%s</kitu><products_list>%s</products_list></packing></packings_list>'
           '</aggregation_document>') % (INN, block, ''.join(f'<product><ki>{p}</ki></product>' for p in packs))
    bnd = 'emuBoundary'
    form = (f'--{bnd}\r\nContent-Disposition: form-data; name="xmlFile"; filename="data.xml"\r\n'
            f'Content-Type: application/xml\r\n\r\n{xml}\r\n--{bnd}--\r\n').encode()
    st, did = cl.call('POST', 'api/v3/true-api/documents/aggregation/create', raw=form,
                      headers={'Content-Type': f'multipart/form-data; boundary={bnd}', 'X-Signature': 'c2ln'})
    assert st == 200, (st, did)
    srv.chz.s.x('UPDATE docs SET ready_at=0')
    st, info = cl.call('GET', f'api/v4/true-api/doc/{did}/info')
    assert info[0]['status'] == 'CHECKED_OK', info
    st, info = cl.call('POST', 'api/v3/true-api/cises/info', [block])
    assert sorted(info[0]['cisInfo']['child']) == sorted(packs), info

    # СУЗ при suz_require_nk: заказ на GTIN без карточки НК отклоняется
    srv.chz.s.set_setting('suz_require_nk', True)
    cl.token = None
    st, con = cl.call('POST', 'api/v3/integration/connection?omsId=o', {}, host=scenario.SUZ_HOST)
    st, stok = cl.call('POST', f"api/v3/true-api/auth/simpleSignIn/{con['omsConnection']}", {'data': 'x', 'inn': INN})
    suz = {'clientToken': stok['token']}
    st, o = cl.call('POST', 'api/v3/order?omsId=o', {'productGroup': 'lp', 'products': [
        {'gtin': '04699999999990', 'quantity': 1}]}, host=scenario.SUZ_HOST, headers=suz)
    st, lst = cl.call('GET', f"api/v3/order/list?omsId=o&orderId={o['orderId']}", host=scenario.SUZ_HOST, headers=suz)
    info = lst['orderInfos'][0]
    assert info['orderStatus'] == 'DECLINED' and 'Национальном каталоге' in info['declineReason'], info
    srv.chz.s.set_setting('suz_require_nk', False)
    cl.token = srv.chz.jwt(INN)

    # без токена — 401, неизвестный метод — 404, битый JSON — 400
    cl.token = None
    assert cl.call('POST', 'api/v3/true-api/cises/info', [packs[0]])[0] == 401
    cl.token = srv.chz.jwt(INN)
    assert cl.call('GET', 'api/v3/true-api/no/such/method')[0] == 404
    assert cl.call('POST', 'api/v3/true-api/cises/info', raw=b'{oops')[0] == 400

    srv.shutdown()
    srv.chz.s.db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print('OK: все проверки эмулятора прошли')


if __name__ == '__main__':
    main()
