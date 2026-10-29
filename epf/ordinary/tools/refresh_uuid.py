"""Новые UUID в XML-исходниках внешней обработки (кроме ClassId): платформа кеширует обработку по UUID.
Использование: refresh_uuid.py <каталог исходников>"""
import os, re, sys, uuid
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        if f.endswith(".xml") and "Ext" not in root.split(os.sep):
            p = os.path.join(root, f)
            s = open(p, encoding="utf-8-sig").read()
            s = re.sub(r"(?<!<xr:ClassId>)(?<=[>\"])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=[<\"])", lambda m: str(uuid.uuid4()), s)
            open(p, "w", encoding="utf-8-sig").write(s)
print("ok")
