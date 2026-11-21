# -*- coding: utf-8 -*-
"""
Genera la base de datos offline de MediScan a partir de CIMA (AEMPS).

Uso:
    python generar_db_offline.py

Salida (en la carpeta actual):
    medicamentos.json.gz  -> base de datos comprimida que descarga la app
    version.json          -> fecha de los datos; la app la compara para actualizarse

Se puede interrumpir (Ctrl+C) y relanzar: reutiliza lo ya descargado en la
carpeta cache_cima. Para regenerar desde cero, borra esa carpeta.
Solo usa la libreria estandar de Python (no hace falta pip install).
"""
import datetime
import gzip
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'https://cima.aemps.es/cima/rest'
CACHE = 'cache_cima'
HILOS = 6
MAX_PARA_QUE = 15
MAX_NO_TOME = 20

RE_INICIO_NO_TOME = re.compile(
    r'^no (tome|tomar|use|usar|utilice|utilizar|debe|deben|administre|aplique|'
    r'reciba|ponga|beba|se (le )?(debe|deben|administr\w*|aplique|use|utilice)|'
    r'le (administren|pongan|apliquen|den))\b'
)
RE_FIN_NO_TOME = re.compile(
    r'^(advertencias|precauciones|niños|otros medicamentos|toma de|uso de|'
    r'embarazo|conducción|información importante|\d+\.\s)'
)


# ---------------------------------------------------------------------------
# Red
# ---------------------------------------------------------------------------
def get_json(url, intentos=5):
    for i in range(intentos):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'MediScan-offline-builder/1.0',
                },
            )
            with urllib.request.urlopen(req, timeout=40) as r:
                datos = r.read()
                if not datos.strip():
                    return None
                return json.loads(datos.decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (204, 400, 404):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError('Fallo repetido: ' + url)


def paginar(ruta):
    pagina = 1
    todos = []
    while True:
        d = get_json(f'{BASE}/{ruta}&pagina={pagina}') or {}
        res = d.get('resultados') or []
        todos.extend(res)
        total = d.get('totalFilas', 0) or 0
        tam = d.get('tamanioPagina', 200) or 200
        print(f'  {ruta.split("?")[0]}: pagina {pagina} ({len(todos)}/{total})', flush=True)
        if not res or pagina * tam >= total:
            break
        pagina += 1
    return todos


# ---------------------------------------------------------------------------
# Limpieza de texto (misma logica que el widget de la app)
# ---------------------------------------------------------------------------
def capitalizar(s):
    t = (s or '').strip().lower()
    return t[:1].upper() + t[1:] if t else ''


def html_a_lineas(h):
    t = re.sub(r'<\s*br\s*/?>', '\n', h or '', flags=re.I)
    t = re.sub(r'<\s*li[^>]*>', '\n• ', t, flags=re.I)
    t = re.sub(r'<\s*/\s*(p|li|h\d|div|tr|ul|ol)\s*>', '\n', t, flags=re.I)
    t = re.sub(r'<\s*(p|div|h\d|tr)(\s[^>]*)?>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]*>', '', t)
    t = html.unescape(t)
    lineas = []
    bala = False
    for l in t.split('\n'):
        l = re.sub(r'\s+', ' ', l).strip()
        if not l:
            continue
        if l == '•':
            bala = True
            continue
        if bala and not l.startswith('•'):
            l = '• ' + l
        bala = False
        lineas.append(l)
    return lineas


def extraer_para_que(lineas):
    res = []
    for l in lineas:
        b = l.lower()
        if len(b) < 140 and ('para qué se utiliza' in b or re.match(r'^1\.\s', b)):
            continue
        if 'si empeora' in b or 'si no mejora' in b:
            continue
        res.append(l)
    return res[:MAX_PARA_QUE]


def extraer_no_tome(lineas):
    res = []
    dentro = False
    for l in lineas:
        b = l.lower()
        if not dentro:
            if RE_INICIO_NO_TOME.match(b):
                dentro = True
                if not (l.endswith(':') or len(l) < 70):
                    res.append(l)
            continue
        if RE_FIN_NO_TOME.match(b):
            break
        res.append(l)
        if len(res) >= MAX_NO_TOME:
            break
    return res


# ---------------------------------------------------------------------------
# Cache de secciones del prospecto
# ---------------------------------------------------------------------------
def ruta_cache(nreg, sec):
    return os.path.join(CACHE, f'{nreg}_{sec}.html')


def leer_cache(nreg, sec):
    ruta = ruta_cache(nreg, sec)
    if not os.path.exists(ruta):
        return ''
    with open(ruta, 'r', encoding='utf-8') as f:
        return f.read()


def descargar_seccion(nreg, sec):
    try:
        d = get_json(f'{BASE}/docSegmentado/contenido/2?nregistro={nreg}&seccion={sec}')
    except Exception:
        return False  # no se guarda: se reintentara al relanzar
    partes = []
    if isinstance(d, list):
        for s in d:
            if isinstance(s, dict) and s.get('contenido'):
                partes.append(str(s['contenido']))
    elif isinstance(d, dict) and d.get('contenido'):
        partes.append(str(d['contenido']))
    with open(ruta_cache(nreg, sec), 'w', encoding='utf-8') as f:
        f.write('\n'.join(partes))
    return True


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main():
    os.makedirs(CACHE, exist_ok=True)
    inicio = time.time()

    print('1/4 Descargando presentaciones comercializadas...', flush=True)
    presentaciones = paginar('presentaciones?comerc=1')
    cns = {}
    pactivos = {}
    nombre_pres = {}
    for p in presentaciones:
        r = str(p.get('nregistro') or '')
        cn = str(p.get('cn') or '')
        if not r or not cn:
            continue
        cns.setdefault(r, set()).add(cn)
        if p.get('pactivos'):
            pactivos.setdefault(r, p['pactivos'])
        if p.get('nombre'):
            nombre_pres.setdefault(r, str(p['nombre']).rsplit(',', 1)[0].strip())

    print('2/4 Descargando medicamentos comercializados...', flush=True)
    medicamentos = paginar('medicamentos?comerc=1')
    info = {}
    for m in medicamentos:
        r = str(m.get('nregistro') or '')
        if not r:
            continue
        vtm = m.get('vtm')
        vtm_nombre = vtm.get('nombre') if isinstance(vtm, dict) else None
        info[r] = {
            'n': (m.get('nombre') or '').strip(),
            'p': capitalizar(vtm_nombre or pactivos.get(r, '')),
        }

    registros = sorted(set(info) | set(cns))
    tareas = [(r, s) for r in registros for s in (1, 2) if not os.path.exists(ruta_cache(r, s))]
    print(f'3/4 Descargando prospectos: {len(registros)} medicamentos, '
          f'{len(tareas)} secciones pendientes (Ctrl+C para parar y relanzar luego)...', flush=True)
    hechas = 0
    fallidas = 0
    with ThreadPoolExecutor(HILOS) as ex:
        futuros = [ex.submit(descargar_seccion, r, s) for r, s in tareas]
        for f in as_completed(futuros):
            hechas += 1
            if not f.result():
                fallidas += 1
            if hechas % 250 == 0 or hechas == len(tareas):
                print(f'  {hechas}/{len(tareas)} (fallidas: {fallidas})', flush=True)

    print('4/4 Generando medicamentos.json.gz...', flush=True)
    salida = []
    sin_para_que = 0
    sin_no_tome = 0
    for r in registros:
        q = extraer_para_que(html_a_lineas(leer_cache(r, 1)))
        t = extraer_no_tome(html_a_lineas(leer_cache(r, 2)))
        if not q:
            sin_para_que += 1
        if not t:
            sin_no_tome += 1
        d = info.get(r, {})
        salida.append({
            'r': r,
            'n': d.get('n') or nombre_pres.get(r, ''),
            'p': d.get('p') or capitalizar(pactivos.get(r, '')),
            'c': sorted(cns.get(r, [])),
            'q': q,
            't': t,
        })

    datos = json.dumps(salida, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    with gzip.open('medicamentos.json.gz', 'wb', compresslevel=9) as f:
        f.write(datos)

    version = datetime.date.today().isoformat()
    with open('version.json', 'w', encoding='utf-8') as f:
        json.dump({'version': version, 'total': len(salida)}, f)

    tam = os.path.getsize('medicamentos.json.gz') / 1024 / 1024
    minutos = (time.time() - inicio) / 60
    print('')
    print(f'OK. Medicamentos: {len(salida)} | sin "para que": {sin_para_que} | '
          f'sin "no tome": {sin_no_tome}')
    print(f'Archivo: medicamentos.json.gz ({tam:.1f} MB) | version: {version} | {minutos:.0f} min')
    if fallidas:
        print(f'AVISO: {fallidas} secciones fallaron. Vuelve a ejecutar el script para reintentarlas.')


if __name__ == '__main__':
    main()
