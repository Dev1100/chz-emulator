# -*- coding: utf-8 -*-
"""Сборка обычных форм (Form.bin) обработок эмулятора ЧЗ из формы-образца.

Форма-образец - обычная форма внешней печатной формы УПП (donor/donor_Form.bin, ее обработка - donor/donor.xml),
перенесена из проекта ut10-perenos-tch, где формат проверен на УПП и УТ 10. Скрипт для каждой обработки:
  * перепривязывает UUID реквизита СсылкаНаОбъект и типа объекта образца к обработке (иначе форма не видит
    модуль объекта: "Ошибка инициализации модуля");
  * меняет заголовок и размер окна (элементы создаются кодом в ПриОткрытии, а элемент за границей макета
    платформа прижимает к краю - размер задается здесь);
  * привязывает событие ПриОткрытии (у обычной формы событие задается в разметке, а не именем процедуры);
  * подменяет модуль формы на forms/<имя>.bsl и упаковывает контейнер без сжатия.

Контейнер 8.2: заголовок FF FF FF 7F, страница 0x200; блоки "\\r\\n<размер док> <размер блока> 7fffffff \\r\\n";
оглавление (адрес атрибутов, адрес данных, 7fffffff); атрибуты: 2 даты по 8 байт, 4 байта, имя UTF-16LE + 4 нуля.

Использование: python -I -X utf8 tools/build_form.py
"""
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONOR_BIN = os.path.join(ROOT, 'donor', 'donor_Form.bin')
DONOR_XML = os.path.join(ROOT, 'donor', 'donor.xml')

# Имя обработки, заголовок окна, размер формы, модуль формы
FORMS = [
    ('ЭЧЗ_НастройкаПодключения_ОФ', 'Эмулятор ЧЗ: настройка подключения', 700, 600, 'НастройкаПодключения.bsl'),
    ('ЭЧЗ_ВыгрузкаВНК_ОФ', 'Эмулятор ЧЗ: выгрузка GTIN в Национальный каталог', 860, 560, 'ВыгрузкаВНК.bsl'),
    ('ЭЧЗ_АвтоПодключение_ОФ', 'Эмулятор ЧЗ: подключение рабочего места', 700, 480, 'АвтоПодключение.bsl'),
]

TITLE_OLD = '"Обработка  Шаблон вешней печатной формы"'
W_OLD, H_OLD = 400, 277

END = 0x7FFFFFFF


def read_container(data):
    """[(attr_bytes, data_bytes)] по оглавлению."""
    assert struct.unpack_from('<i', data, 0)[0] == END, 'не контейнер 8.2'

    def read_doc(addr):
        out = b''
        doc_len = None
        while True:
            hdr = data[addr:addr + 31]
            assert hdr[:2] == b'\r\n', hdr
            d_len, b_len, nxt = (int(x, 16) for x in hdr[2:29].split(b' ')[:3])
            if doc_len is None:
                doc_len = d_len
            out += data[addr + 31:addr + 31 + b_len]
            if nxt == END:
                break
            addr = nxt
        return out[:doc_len]

    toc = read_doc(16)
    files = []
    for i in range(0, len(toc), 12):
        a, d, _ = struct.unpack_from('<iii', toc, i)
        files.append((read_doc(a), read_doc(d)))
    return files


def block(payload, block_size=None):
    size = len(payload) if block_size is None else max(block_size, len(payload))
    hdr = b'\r\n%08x %08x %08x \r\n' % (len(payload), size, END)
    return hdr + payload + b'\x00' * (size - len(payload))


def write_container(files):
    """Раскладка как у платформы: оглавление, атрибуты всех файлов по порядку, затем данные в
    обратном порядке. Один блок на документ; оглавление и данные - блок не меньше страницы (0x200)."""
    out = bytearray(struct.pack('<iiii', END, 0x200, len(files), 0))
    toc_block = max(0x200, 12 * len(files))
    pos = 16 + 31 + toc_block
    body = b''
    attr_addr, data_addr = [], [None] * len(files)
    for attr, _ in files:
        attr_addr.append(pos)
        chunk = block(attr)
        body += chunk
        pos += len(chunk)
    for i in reversed(range(len(files))):
        data_addr[i] = pos
        chunk = block(files[i][1], 0x200)
        body += chunk
        pos += len(chunk)
    toc = b''.join(struct.pack('<iii', a, d, END) for a, d in zip(attr_addr, data_addr))
    out += block(toc, toc_block) + body
    return bytes(out)


def attr_name(attr):
    return attr[20:].decode('utf-16-le').rstrip('\x00')


def ids(xml_path):
    s = open(xml_path, encoding='utf-8-sig').read()
    attr = re.search(r'<Attribute uuid="([^"]+)">\s*<Properties>\s*<Name>СсылкаНаОбъект<', s).group(1)
    obj = re.search(r'category="Object">\s*<xr:TypeId>([^<]+)<', s).group(1)
    return attr, obj


def build(files, name, title, width, height, module_file):
    form = files[0][1].decode('utf-8-sig')

    for old, new in zip(ids(DONOR_XML), ids(os.path.join(ROOT, 'src', name + '.xml'))):
        assert form.count(old) > 0, old
        form = form.replace(old, new)

    assert form.count(TITLE_OLD) == 1
    form = form.replace(TITLE_OLD, '"%s"' % title)

    # Размер формы (заголовок формы: ...,W,H,1,0,1,4,4,62,W,H,96}), командная панель {8,0,0,W,25,
    # направляющие по правому и нижнему краю {2,W-8,...} и {2,H-9,...}
    repl = [
        ('},%d,%d,1,0,1,4,4,62,%d,%d,96}' % (W_OLD, H_OLD, W_OLD, H_OLD),
         '},%d,%d,1,0,1,4,4,62,%d,%d,96}' % (width, height, width, height)),
        ('{8,0,0,%d,25,1,' % W_OLD, '{8,0,0,%d,25,1,' % width),
        ('{2,%d,1,1,3,0,0,8,0}' % (W_OLD - 8), '{2,%d,1,1,3,0,0,8,0}' % (width - 8)),
        ('{2,%d,0,1,4,0,0,9,0}' % (H_OLD - 9), '{2,%d,0,1,4,0,0,9,0}' % (height - 9)),
    ]
    for old, new in repl:
        assert form.count(old) == 1, old
        form = form.replace(old, new)

    # Событие ПриОткрытии (код 70001): у образца список событий пуст ({0} после блока 59d6c227...).
    events_old = '{0},1}\r\n},\r\n{0},1,4,1,0,0,0,'
    caption = '{1,1,\r\n{"ru","При открытии"}\r\n}'
    events_new = ('{0},1}\r\n},\r\n{1,\r\n{70001,e1692cc2-605b-4535-84dd-28440238746c,\r\n{3,"ПриОткрытии",\r\n'
                  '{1,"ПриОткрытии",\r\n' + caption + ',\r\n' + caption + ',\r\n' + caption + ',\r\n'
                  '{3,0,\r\n{0},"",-1,-1,1,0},\r\n{0,0,0}\r\n}\r\n}\r\n}\r\n},1,4,1,0,0,0,')
    assert form.count(events_old) == 1, 'не найден список событий формы'
    form = form.replace(events_old, events_new)

    module = open(os.path.join(ROOT, 'forms', module_file), encoding='utf-8-sig').read()
    module = module.replace('\r\n', '\n').replace('\n', '\r\n')
    result = [
        (files[0][0], b'\xef\xbb\xbf' + form.encode('utf-8')),
        (files[1][0], b'\xef\xbb\xbf' + module.encode('utf-8')),
    ]
    dst = os.path.join(ROOT, 'src', name, 'Forms', 'Форма', 'Ext', 'Form.bin')
    open(dst, 'wb').write(write_container(result))
    print('ok', dst)


def main():
    donor = open(DONOR_BIN, 'rb').read()
    files = read_container(donor)
    # Образец хранит в хвостах блоков мусор, поэтому сверяются раскладка и содержимое, а не все байты.
    again = write_container(files)
    if len(again) != len(donor) or read_container(again) != files or again[:16] != donor[:16]:
        sys.exit('упаковщик не воспроизводит раскладку образца - формат контейнера отличается')
    assert [attr_name(a) for a, _ in files] == ['form', 'module']

    for name, title, width, height, module_file in FORMS:
        build(files, name, title, width, height, module_file)


if __name__ == '__main__':
    main()
