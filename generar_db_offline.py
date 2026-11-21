# -*- coding: utf-8 -*-
"""
Genera la base de datos offline de MediScan a partir de CIMA (AEMPS).

Uso:
    python generar_db_offline.py

Salida (en la carpeta actual):
    medicamentos.json.gz  -> base de datos comprimida que descarga la app
    version.json          -> fecha de los datos; la app la compara para actualizarse

Funcionamiento INCREMENTAL:
- Si ya existe un medicamentos.json.gz anterior, solo descarga los prospectos
  nuevos o los que CIMA ha actualizado desde entonces (por su fecha).
- La primera ejecucion descarga todo (unos 16.000 prospectos).
- Se puede interrumpir (Ctrl+C) y relanzar: reutiliza lo ya descargado en la
  carpeta cache_cima.
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
SALIDA = 'medicamentos.json.gz'
HILOS = 10
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
                    'User-Agent': 'MediScan-offline-builder/1.1',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as r:
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
        if pagina % 10 == 0 or not res or pagina * tam >= total:
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
# Prospectos: una sola peticion por medicamento (todas las secciones)
# ---------------------------------------------------------------------------
def ruta_cache(nreg, fecha):
    return os.path.join(CACHE, f'{nreg}_{fecha or 0}.json')


def descargar_prospecto(nreg, fecha):
    try:
        d = get_json(f'{BASE}/docSegmentado/contenido/2?nregistro={nreg}')
    except Exception:
        return False  # no se guarda: se reintentara al relanzar
    secciones = {'1': [], '2': []}
    if isinstance(d, list):
        for s in d:
            if not isinstance(s, dict):
                continue
            num = str(s.get('seccion') or '').split('.')[0]
            if num in secciones and s.get('contenido'):
                secciones[num].append(str(s['contenido']))
    datos = {k: '\n'.join(v) for k, v in secciones.items()}
    with open(ruta_cache(nreg, fecha), 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False)
    return True


def leer_cache(nreg, fecha):
    ruta = ruta_cache(nreg, fecha)
    if not os.path.exists(ruta):
        return None
    with open(ruta, 'r', encoding='utf-8') as f:
        return json.load(f)


def cargar_anterior():
    if not os.path.exists(SALIDA):
        return {}
    try:
        with gzip.open(SALIDA, 'rb') as f:
            datos = json.loads(f.read().decode('utf-8'))
        return {str(m.get('r')): m for m in datos if isinstance(m, dict)}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def main():
    os.makedirs(CACHE, exist_ok=True)
    inicio = time.time()
    anterior = cargar_anterior()
    print(f'Base anterior: {len(anterior)} medicamentos', flush=True)

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
        fecha = 0
        secc = True
        for d in m.get('docs') or []:
            if isinstance(d, dict) and str(d.get('tipo')) == '2':
                fecha = d.get('fecha') or 0
                secc = bool(d.get('secc', True))
                break
        info[r] = {
            'n': (m.get('nombre') or '').strip(),
            'p': capitalizar(vtm_nombre or pactivos.get(r, '')),
            'f': fecha,
            'secc': secc,
        }

    registros = sorted(set(info) | set(cns))

    # Que prospectos hay que (re)descargar
    pendientes = []
    for r in registros:
        d = info.get(r, {})
        fecha = d.get('f', 0)
        if not d.get('secc', True):
            continue  # CIMA no tiene el prospecto por secciones
        prev = anterior.get(r)
        al_dia = prev is not None and prev.get('f') == fecha and (prev.get('q') or prev.get('t'))
        if al_dia:
            continue
        if os.path.exists(ruta_cache(r, fecha)):
            continue
        pendientes.append((r, fecha))

    print(f'3/4 Prospectos a descargar: {len(pendientes)} de {len(registros)} '
          f'(Ctrl+C para parar y relanzar luego)...', flush=True)
    hechas = 0
    fallidas = 0
    with ThreadPoolExecutor(HILOS) as ex:
        futuros = [ex.submit(descargar_prospecto, r, f) for r, f in pendientes]
        for fut in as_completed(futuros):
            hechas += 1
            if not fut.result():
                fallidas += 1
            if hechas % 250 == 0 or hechas == len(pendientes):
                print(f'  {hechas}/{len(pendientes)} (fallidas: {fallidas})', flush=True)

    print(f'4/4 Generando {SALIDA}...', flush=True)
    salida = []
    sin_para_que = 0
    sin_no_tome = 0
    for r in registros:
        d = info.get(r, {})
        fecha = d.get('f', 0)
        prev = anterior.get(r) or {}
        cache = leer_cache(r, fecha)
        if cache is not None:
            q = extraer_para_que(html_a_lineas(cache.get('1', '')))
            t = extraer_no_tome(html_a_lineas(cache.get('2', '')))
            f_guardada = fecha
        else:
            q = prev.get('q') or []
            t = prev.get('t') or []
            f_guardada = prev.get('f', 0) if (q or t) else 0
        if not q:
            sin_para_que += 1
        if not t:
            sin_no_tome += 1
        salida.append({
            'r': r,
            'n': d.get('n') or nombre_pres.get(r, '') or prev.get('n', ''),
            'p': d.get('p') or capitalizar(pactivos.get(r, '')) or prev.get('p', ''),
            'c': sorted(cns.get(r, [])),
            'q': q,
            't': t,
            'f': f_guardada,
        })

    datos = json.dumps(salida, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    with gzip.open(SALIDA, 'wb', compresslevel=9) as f:
        f.write(datos)

    version = datetime.date.today().isoformat()
    with open('version.json', 'w', encoding='utf-8') as f:
        json.dump({'version': version, 'total': len(salida)}, f)

    tam = os.path.getsize(SALIDA) / 1024 / 1024
    minutos = (time.time() - inicio) / 60
    print('')
    print(f'OK. Medicamentos: {len(salida)} | sin "para que": {sin_para_que} | '
          f'sin "no tome": {sin_no_tome}')
    print(f'Archivo: {SALIDA} ({tam:.1f} MB) | version: {version} | {minutos:.0f} min')
    if fallidas:
        print(f'AVISO: {fallidas} prospectos fallaron. Vuelve a ejecutar el script para reintentarlos.')


if __name__ == '__main__':
    main()
