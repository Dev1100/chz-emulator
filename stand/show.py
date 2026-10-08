import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8-sig'))
for r in d['результаты']:
    print(r['статус'], r.get('мс')); x=r.get('результат') or {}
    for k in sorted(x, key=lambda s:int(s[1:].split('_')[0])): print(' ',k,':',x[k])
    print(r.get('ошибка','')[:1800])
