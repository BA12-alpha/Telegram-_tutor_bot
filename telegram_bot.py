#!/usr/bin/env python3
import os
import tempfile
import logging
import hashlib
import time
import json
from collections import deque, defaultdict
from pathlib import Path
from typing import Optional, Set, Dict, Deque, Tuple, Any

from telegram import Update, File, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.error import RetryAfter, NetworkError

from common import analyze_code, ocr_image_path

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    cfg_path = Path("config.json")
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
            BOT_TOKEN = data.get("BOT_TOKEN") or data.get("bot_token")
        except Exception as e:
            logging.error("No pude leer config.json: %s", e)

MAX_DOC_SIZE_MB = float(os.environ.get("MAX_DOC_SIZE_MB", 256))
MAX_PHOTO_SIZE_MB = float(os.environ.get("MAX_PHOTO_SIZE_MB", 256))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", 50000))
DOWNLOAD_TIMEOUT = float(os.environ.get("DOWNLOAD_TIMEOUT", 60))
DOWNLOAD_MAX_RETRIES = int(os.environ.get("DOWNLOAD_MAX_RETRIES", 3))
RATE_LIMIT_WINDOW_S = int(os.environ.get("RATE_LIMIT_WINDOW_S", 30))
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", 5))
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", 5))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

DEFAULT_ALLOWED_MIME: Set[str] = {
    "text/plain", "text/markdown", "text/x-python", "text/x-c", "text/x-c++",
    "text/x-java", "text/x-go", "text/x-php", "text/x-ruby", "text/x-shellscript",
    "text/x-sql", "text/x-typescript", "text/css", "text/html",
    "application/json", "application/javascript", "application/xml",
    "application/x-yaml", "application/yaml",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
}
ENV_MIME = os.environ.get("ALLOWED_DOC_MIME_LIST")
ALLOWED_DOC_MIME: Set[str] = (
    {m.strip().lower() for m in ENV_MIME.split(",") if m.strip()} if ENV_MIME else DEFAULT_ALLOWED_MIME
)

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("telegram-bot")

# ============================================================
# STATE IN MEMORY + PERSISTENCE
# ============================================================
user_history: Dict[int, Deque[Tuple[str, str]]] = defaultdict(lambda: deque(maxlen=HISTORY_MAX))
rate_limit: Dict[int, Deque[float]] = defaultdict(deque)
ocr_cache: Dict[str, str] = {}
tutor_state: Dict[int, Dict[str, Any]] = defaultdict(dict)  # persisted


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            tutor_state.update({int(k): v for k, v in data.get("tutor_state", {}).items()})
        except Exception as e:
            logger.warning("No pude cargar state: %s", e)


def save_state():
    try:
        STATE_FILE.write_text(json.dumps({"tutor_state": tutor_state}, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("No pude guardar state: %s", e)


# ============================================================
# TUTOR CONTENT (AMPLIADO HASTA NIVEL 20 CON PLACEHOLDERS)
# ============================================================
TUTOR_CONTENT = {
    # ============================================
    # PROGRAMACIÓN GENERAL (Python)
    # ============================================
    "python": {
        0: {  # Fundamentos
            "modules": [
                {"title": "Variables y tipos", "lesson": "int/float/str/bool, print, f-strings",
                 "code": "name='Ada'; age=30; print(f'Hola {name}, {age}')",
                 "exercises": ["Declara 3 vars y muéstralas", "Concatena str + número convirtiendo tipo"],
                 "tasks": ["Script que pida nombre/edad y diga edad+5"]},
                {"title": "Control de flujo", "lesson": "if/elif/else, comparaciones, bool",
                 "code": "x=7\nif x>10: print('grande')\nelif x>=5: print('mediano')\nelse: print('pequeño')",
                 "exercises": ["Par/impar", "Signo de un número"], "tasks": ["Calculadora +,-,*,/ con manejo de /0"]},
                {"title": "Bucles y colecciones", "lesson": "for/while, listas, dict, range, len, append",
                 "code": "nums=[1,2,3]\nfor n in nums: print(n)\nfor k,v in {'a':1}.items(): print(k,v)",
                 "exercises": ["Suma lista", "Contar letras en string"], "tasks": ["Aplicar 10% descuento a precios y totalizar"]},
                {"title": "Funciones", "lesson": "def, params, return, scope básico",
                 "code": "def area(r): return 3.1416*r*r\nprint(area(3))",
                 "exercises": ["Max de 3 números", "Filtrar pares de lista"],
                 "tasks": ["Validador de contraseña: >=8, 1 número, 1 letra -> True/False"]},
            ],
            "quiz": [
                {"q": "len('hola') =", "options": ["3", "4", "5"], "answer": 1},
                {"q": "if 5>3: print('ok') imprime", "options": ["nada", "ok", "error"], "answer": 1},
                {"q": "[n for n in [1,2,3] if n%2]==?", "options": ["[1,3]", "[2]", "[]"], "answer": 0},
            ],
            "errors": [
                {"name": "SyntaxError", "why": "Falta : comillas o paréntesis", "how": "Revisar línea previa e identación", "example": "if x>3 print(x)"},
                {"name": "NameError", "why": "Variable no definida", "how": "Definir antes, revisar typos", "example": "pritn(x)"},
                {"name": "TypeError", "why": "Operación con tipos incompatibles", "how": "Convertir tipos o ajustar operación", "example": "'a'+1"},
            ],
        },
        1: {  # Junior
            "modules": [
                {"title": "Comprehensions y slicing", "lesson": "List/dict comps, slices",
                 "code": "nums=[1,2,3,4]; ev=[n for n in nums if n%2==0]",
                 "exercises": ["Cuadrados 1..10", "Filtra palabras >3 letras"],
                 "tasks": ["Dict palabra->longitud por comprensión"]},
                {"title": "Errores (try/except)", "lesson": "try/except/else/finally",
                 "code": "try: 1/0\nexcept ZeroDivisionError: print('No')\nfinally: print('fin')",
                 "exercises": ["Captura ValueError al castear int", "FileNotFoundError en open"],
                 "tasks": ["Bucle de input numérico con reintento y 'q' para salir"]},
                {"title": "Módulos y paquetes", "lesson": "import, from x import, pathlib/json",
                 "code": "import math; from math import sqrt",
                 "exercises": ["Listar archivos con pathlib", "Cargar json y contar claves"],
                 "tasks": ["Crear helpers.py con sumar(a,b) e importarlo"]},
                {"title": "Tests básicos", "lesson": "unittest, assertions",
                 "code": "import unittest\nclass T(unittest.TestCase): ...",
                 "exercises": ["Tests para max3", "Test que espera ValueError"],
                 "tasks": ["Tests para validador de contraseña"]},
            ],
            "quiz": [
                {"q": "finally se ejecuta", "options": ["solo sin error", "siempre", "nunca"], "answer": 1},
                {"q": "math.pi es", "options": ["función", "constante", "clase"], "answer": 1},
            ],
            "errors": [
                {"name": "IndexError", "why": "Índice fuera de rango", "how": "Verifica len o usa slicing", "example": "nums[10] con len 5"},
                {"name": "KeyError", "why": "Clave no existe", "how": "dict.get o verificar clave", "example": "d['x'] sin clave"},
            ],
        },
        2: {  # Mid
            "modules": [
                {"title": "OOP básica", "lesson": "clases, __init__, métodos",
                 "code": "class Persona:\n    def __init__(self,n): self.n=n\n    def saluda(self): return f'Hola {self.n}'",
                 "exercises": ["Clase Coche", "Método encender"], "tasks": ["CuentaBancaria con depositar/retirar"]},
                {"title": "Archivos y contexto", "lesson": "with open",
                 "code": "with open('data.txt') as f: print(f.read())",
                 "exercises": ["Escribir y leer lista de números", "Contar líneas"],
                 "tasks": ["Logger simple con timestamp a archivo"]},
                {"title": "HTTP cliente", "lesson": "requests GET/POST, JSON",
                 "code": "import requests; r=requests.get('https://httpbin.org/get')",
                 "exercises": ["GET API pública", "POST json a httpbin"],
                 "tasks": ["Wrapper fetch_json(url) con timeout y manejo de errores"]},
                {"title": "Logging/config", "lesson": "logging.basicConfig, niveles",
                 "code": "import logging; logging.basicConfig(level=logging.INFO)",
                 "exercises": ["Logger con formato personalizado", "Loguear excepciones"],
                 "tasks": ["App que loguee a archivo y stdout"]},
            ],
            "quiz": [
                {"q": "with open hace", "options": ["nada", "cierra automático", "compila"], "answer": 1},
                {"q": "requests.get(...).json() devuelve", "options": ["bytes", "str", "dict/list"], "answer": 2},
            ],
            "errors": [
                {"name": "requests.Timeout", "why": "Demora excesiva", "how": "timeout y retry/backoff", "example": "requests.get(url, timeout=2)"},
                {"name": "IOError", "why": "Archivo/permisos", "how": "Verifica ruta/permiso", "example": "open('/root/x')"},
            ],
        },
        3: {  # Avanzado
            "modules": [
                {"title": "Asyncio", "lesson": "async/await, gather",
                 "code": "import asyncio\nasync def main(): ...\nasyncio.run(main())",
                 "exercises": ["3 tareas con sleeps", "aiohttp paralelo"],
                 "tasks": ["Scraper concurrente de títulos"]},
                {"title": "Patrones/arquitectura", "lesson": "capas, DI, adapters",
                 "code": "# servicio + repo (conceptual)",
                 "exercises": ["Separar lógica de datos/presentación", "Repo interface + fake repo"],
                 "tasks": ["Mini API con dominio + infra simulada"]},
                {"title": "Calidad", "lesson": "linters/formatters/tests",
                 "code": "# pytest example",
                 "exercises": ["3 tests pytest", "Aplicar black y ver diff"],
                 "tasks": ["Pipeline local: black+pytest+coverage"]},
                {"title": "Debug/perf", "lesson": "logging contextual, profiling",
                 "code": "log.info('msg', extra={'user':'u1'})",
                 "exercises": ["Capturar TypeError con stacktrace", "cProfile en loop grande"],
                 "tasks": ["Guía de diagnóstico para tu proyecto"]},
            ],
            "quiz": [
                {"q": "asyncio.gather", "options": ["Paraleliza IO async", "Compila", "Bloquea hilo"], "answer": 0},
                {"q": "black es", "options": ["Linter", "Formatter", "Debugger"], "answer": 1},
            ],
            "errors": [
                {"name": "Race condition", "why": "Acceso concurrente sin control", "how": "Locks/colas/await correcto", "example": "Compartir mutable sin lock"},
                {"name": "Memory leak", "why": "Referencias retenidas", "how": "Limitar caches, cerrar recursos", "example": "Acumular en lista global"},
            ],
        },
        4: {  # Especialización (Data/ML o servicios)
            "modules": [
                {"title": "NumPy/Pandas básico", "lesson": "ndarray, df, select/transform",
                 "code": "import pandas as pd; df=pd.DataFrame(...)",
                 "exercises": ["Filtro y agregación simple", "Merge de dos tablas"],
                 "tasks": ["EDA breve sobre dataset pequeño"]},
                {"title": "APIs y servicios", "lesson": "FastAPI/flask básico",
                 "code": "from fastapi import FastAPI\napp=FastAPI()",
                 "exercises": ["Endpoint GET simple", "POST que valida input"],
                 "tasks": ["API CRUD mínima con validación"]},
            ],
            "quiz": [{"q": "Pandas DataFrame es", "options": ["array 1D", "tabla 2D", "dict"], "answer": 1}],
            "errors": [{"name": "ValueError (pandas)", "why": "Dimensiones/columnas", "how": "Revisar shapes/columns", "example": "merge con columnas distintas"}],
        },
        5: {  # Senior/Arquitectura
            "modules": [
                {"title": "Dominios y límites", "lesson": "DDD ligero, agregados",
                 "code": "# conceptual",
                 "exercises": ["Identifica entidades/valores en un problema"],
                 "tasks": ["Diseña modelo de dominio para órdenes/pagos/envíos"]},
                {"title": "Escalabilidad", "lesson": "caching, colas, partición",
                 "code": "# conceptual",
                 "exercises": ["Diseña un cache layer"],
                 "tasks": ["Plan de escalado para API concurrida"]},
            ],
            "quiz": [{"q": "Cache se usa para", "options": ["Persistencia fuerte", "Reducir latencia", "Seguridad"], "answer": 1}],
            "errors": [{"name": "Hotspot", "why": "Clave popular en cache/DB", "how": "Sharding/colas/backoff", "example": "Misma clave muy concurrida"}],
        },
        6: {
            "modules": [
                {"title": "Servicios HTTP robustos", "lesson": "FastAPI avanzado: validación, dependencias, middlewares, errores",
                 "code": "from fastapi import FastAPI, Depends, HTTPException\napp=FastAPI()\n@app.get('/items/{id}')\ndef get_item(id:int):\n    if id<0: raise HTTPException(400,'id invalido')\n    return {'id':id}",
                 "exercises": ["Agregar dependencias (auth básica)", "Middleware de logging"],
                 "tasks": ["API con CRUD + validación Pydantic + manejo de errores global"]},
                {"title": "SQL/ORM", "lesson": "SQLModel/SQLAlchemy: sesiones, modelos, relaciones simples",
                 "code": "# modelo SQLModel y sessionmaker",
                 "exercises": ["Crear tabla y hacer CRUD"], "tasks": ["API CRUD con FastAPI + SQLModel y migraciones alembic"]},
            ],
            "quiz": [
                {"q": "FastAPI lanza HTTPException para", "options": ["Errores HTTP controlados", "Errores de sintaxis", "Logs"], "answer": 0},
                {"q": "SQLAlchemy session sirve para", "options": ["Formatear JSON", "Gestionar transacciones", "Hacer hashing"], "answer": 1},
            ],
            "errors": [
                {"name": "Session leak", "why": "No cerrar/commit/rollback", "how": "Usar contextmanager/dependencias", "example": "Session global sin close()"},
                {"name": "N+1 queries", "why": "Cargar relaciones perezosas en bucle", "how": "selectinload/joinedload", "example": "for user in users: user.posts"},
            ],
        },
        7: {
            "modules": [
                {"title": "Caching y colas", "lesson": "Redis para cache y rate limit; Celery/RQ para tareas",
                 "code": "# pseudo: redis.set/get y job queue",
                 "exercises": ["Implementar cache con TTL", "Job asincrónico que procese una lista"],
                 "tasks": ["Endpoint que use cache y dispare tarea en background"]},
                {"title": "Test y CI", "lesson": "pytest + coverage + pipeline básico",
                 "code": "# pytest + coverage run -m pytest",
                 "exercises": ["Tests con fixtures", "Medir cobertura"],
                 "tasks": ["Workflow local/CI: lint+test+coverage threshold"]},
            ],
            "quiz": [
                {"q": "TTL en cache es para", "options": ["Persistir", "Expirar entradas", "Encriptar"], "answer": 1},
                {"q": "coverage mide", "options": ["Latencia", "Líneas ejecutadas", "Uso de RAM"], "answer": 1},
            ],
            "errors": [
                {"name": "Stale cache", "why": "No invalidar", "how": "TTL, versionado de claves", "example": "Cache de producto sin borrar tras update"},
            ],
        },
        8: {
            "modules": [
                {"title": "Seguridad aplicación", "lesson": "AuthZ/AuthN, JWT, hashing, mitigaciones OWASP Top 10",
                 "code": "# generar/validar JWT con pyjwt",
                 "exercises": ["Endpoint protegido por rol", "Hash de password con bcrypt"],
                 "tasks": ["Agregar auth JWT + roles a API con buenas prácticas (no guardar secretos en código)"]},
                {"title": "Observabilidad", "lesson": "logging estructurado, métricas (Prometheus), trazas (OTel)",
                 "code": "# logger con json + counter de peticiones",
                 "exercises": ["Log JSON con request_id", "Exponer /metrics dummy"],
                 "tasks": ["Añadir request_id y métricas a tu API; log de errores con contexto"]},
            ],
            "quiz": [
                {"q": "Password seguro se almacena", "options": ["Plano", "Hash + salt", "Base64"], "answer": 1},
                {"q": "Observabilidad combina", "options": ["Logs + métricas + trazas", "Solo logs", "Solo traces"], "answer": 0},
            ],
            "errors": [
                {"name": "JWT inseguro", "why": "Clave débil o hardcode", "how": "Rotar/almacenar en secrets, HS256/RS256 adecuados", "example": "Secret '123' en código"},
            ],
        },
        9: {
            "modules": [
                {"title": "Arquitectura de servicios", "lesson": "Monolito modular vs microservicios ligeros; contratos; versionado",
                 "code": "# esquema de capas y DTOs",
                 "exercises": ["Diseñar contratos estables"],
                 "tasks": ["Dividir una API en servicios con un BFF o gateway simple"]},
                {"title": "Performance", "lesson": "Perf de IO vs CPU, profiling, uvloop, gunicorn/uvicorn tuning",
                 "code": "# gunicorn -k uvicorn.workers.UvicornWorker -w 4",
                 "exercises": ["Medir latencia antes/después de cache"],
                 "tasks": ["Profiling de un endpoint y propuesta de mejora (cache/batch)"]},
            ],
            "quiz": [
                {"q": "Microservicios añaden", "options": ["Menos latencia seguro", "Coste de red/coord", "Menos contratos"], "answer": 1},
                {"q": "IO-bound mejora con", "options": ["Más threads/async", "Optimizar CPU", "Cambiar a int"], "answer": 0},
            ],
            "errors": [
                {"name": "Chattiness", "why": "Demasiadas llamadas entre servicios", "how": "Batching, agregación, caché", "example": "20 llamadas para armar una página"},
            ],
        },
        10: {
            "modules": [
                {"title": "Distribución y despliegue", "lesson": "Contenedores, imágenes slim, multi-stage; envs y secrets",
                 "code": "# Dockerfile multistage ejemplo",
                 "exercises": ["Construir imagen slim"],
                 "tasks": ["Containerizar API con multi-stage y variables de entorno seguras"]},
                {"title": "Fiabilidad", "lesson": "Retries/backoff, circuit breaker, timeouts, healthchecks",
                 "code": "# patrón retry y timeout en cliente",
                 "exercises": ["Agregar timeout+retry a fetch_json"],
                 "tasks": ["Añadir /health y política de retry/backoff en cliente"]},
            ],
            "quiz": [
                {"q": "Multi-stage build sirve para", "options": ["Imágenes más pequeñas", "Más logs", "Más capas innecesarias"], "answer": 0},
                {"q": "Circuit breaker corta", "options": ["El CPU", "Llamadas a servicio inestable", "El disco"], "answer": 1},
            ],
            "errors": [
                {"name": "Retry storm", "why": "Demasiados retries sin backoff", "how": "Backoff exponencial + jitter", "example": "10 clientes reintentando en bucle"},
            ],
        },
        **{lvl: {"modules": [{"title": f"Python nivel {lvl} (placeholder)", "lesson": "Añade lección avanzada (IA, dist-sys, seguridad profunda, etc.)", "code": "# ejemplo", "exercises": ["Ej1"], "tasks": ["Proyecto del nivel"]}], "quiz": [], "errors": []} for lvl in range(11, 21)}
    },

    # ============================================
    # OTROS LENGUAJES (base + placeholders)
    # ============================================
    "javascript": {
        0: {"modules": [{"title": "Hola mundo y let/const", "lesson": "console.log, variables", "code": "const n='Ada'; let a=30;", "exercises": ["Declara 3 vars"], "tasks": ["Pedir nombre y saludar"]}], "quiz": [], "errors": []},
        1: {"modules": [{"title": "Arrays map/filter", "lesson": "transformar y filtrar", "code": "const ev=nums.filter(n=>n%2===0);", "exercises": ["Suma array", "Filtra mayores"], "tasks": ["Procesar lista y sumar"]}], "quiz": [], "errors": []},
        2: {"modules": [{"title": "Clases y this", "lesson": "class, constructor", "code": "class Persona { constructor(n){this.n=n;} }", "exercises": ["Clase Coche"], "tasks": ["Modelo simple con métodos"]}], "quiz": [], "errors": []},
        3: {"modules": [{"title": "Async/await y fetch", "lesson": "promesas, fetch", "code": "const r=await fetch(url);", "exercises": ["GET API"], "tasks": ["Múltiples fetch en paralelo"]}], "quiz": [], "errors": []},
        **{lvl: {"modules": [{"title": f"JS nivel {lvl} (placeholder)", "lesson": "Añade lección", "code": "// ejemplo", "exercises": ["Ej1"], "tasks": ["Tarea"]}], "quiz": [], "errors": []} for lvl in range(4, 21)}
    },
    "java": {
        0: {"modules": [{"title": "Tipos y main", "lesson": "clase, main, tipos primitivos", "code": "public class Main{public static void main(String[] a){System.out.println(\"Hi\");}}", "exercises": ["Hola mundo", "Variables y print"], "tasks": ["Leer nombre por Scanner"]}], "quiz": [], "errors": []},
        1: {"modules": [{"title": "POO básica", "lesson": "clases, getters/setters", "code": "class Persona{...}", "exercises": ["Clase Libro"], "tasks": ["Cuenta bancaria simple"]}], "quiz": [], "errors": []},
        2: {"modules": [{"title": "Colecciones y streams", "lesson": "List/Map, stream/filter/map", "code": "nums.stream().filter(n->n>0)...", "exercises": ["Filtrar lista"], "tasks": ["Agrupar por categoría"]}], "quiz": [], "errors": []},
        3: {"modules": [{"title": "Excepciones y IO", "lesson": "try-with-resources", "code": "try(FileReader fr=...){...}", "exercises": ["Leer archivo"], "tasks": ["Copiar archivo con buffer"]}], "quiz": [], "errors": []},
        **{lvl: {"modules": [{"title": f"Java nivel {lvl} (placeholder)", "lesson": "Añade lección", "code": "// ejemplo", "exercises": ["Ej1"], "tasks": ["Tarea"]}], "quiz": [], "errors": []} for lvl in range(4, 21)}
    },
    "csharp": {
        0: {"modules": [{"title": "Tipos y consola", "lesson": "Console.WriteLine, tipos", "code": "Console.WriteLine(\"Hola\");", "exercises": ["Hola mundo", "Variables"], "tasks": ["Pedir nombre y saludar"]}], "quiz": [], "errors": []},
        1: {"modules": [{"title": "POO básica", "lesson": "class, propiedades", "code": "class Persona { public string Nombre {get;set;} }", "exercises": ["Clase Producto"], "tasks": ["Inventario simple"]}], "quiz": [], "errors": []},
        2: {"modules": [{"title": "LINQ", "lesson": "Select/Where", "code": "nums.Where(n=>n>0).Select(n=>n*2)", "exercises": ["Filtrar y mapear"], "tasks": ["Agrupar datos con LINQ"]}], "quiz": [], "errors": []},
        3: {"modules": [{"title": "Async/await", "lesson": "Task, await", "code": "var r=await client.GetStringAsync(url);", "exercises": ["GET HTTP"], "tasks": ["Varias peticiones en paralelo"]}], "quiz": [], "errors": []},
        **{lvl: {"modules": [{"title": f"C# nivel {lvl} (placeholder)", "lesson": "Añade lección", "code": "// ejemplo", "exercises": ["Ej1"], "tasks": ["Tarea"]}], "quiz": [], "errors": []} for lvl in range(4, 21)}
    },
    "go": {
        0: {"modules": [{"title": "Fundamentos", "lesson": "package main, func main, tipos", "code": "package main\nimport \"fmt\"\nfunc main(){fmt.Println(\"hi\")}", "exercises": ["Print y vars"], "tasks": ["Leer input y saludar"]}], "quiz": [], "errors": []},
        1: {"modules": [{"title": "Slices y mapas", "lesson": "make, append, map", "code": "m:=map[string]int{\"a\":1}", "exercises": ["Sumar slice"], "tasks": ["Contar palabras en texto"]}], "quiz": [], "errors": []},
        2: {"modules": [{"title": "Goroutines y canales", "lesson": "go f(), chan", "code": "go work(); <-ch", "exercises": ["Dos goroutines"], "tasks": ["Workers con canal"]}], "quiz": [], "errors": []},
        3: {"modules": [{"title": "HTTP y context", "lesson": "net/http, context", "code": "http.Get(url)", "exercises": ["GET HTTP"], "tasks": ["Cliente con timeout/context"]}], "quiz": [], "errors": []},
        **{lvl: {"modules": [{"title": f"Go nivel {lvl} (placeholder)", "lesson": "Añade lección", "code": "// ejemplo", "exercises": ["Ej1"], "tasks": ["Tarea"]}], "quiz": [], "errors": []} for lvl in range(4, 21)}
    },

    # ============================================
    # SEGURIDAD / INFRA
    # ============================================
    "sec_pentesting": {
        0: {"modules": [
                {"title": "Modelos y ética", "lesson": "Tipos de pentest, legalidad, alcance", "code": "# conceptual",
                 "exercises": ["Identifica alcance seguro"], "tasks": ["Redacta reglas de engagement"]},
                {"title": "Recon y OSINT", "lesson": "whois, subdominios, puertos", "code": "# nmap/whois ejemplos",
                 "exercises": ["Enumerar puertos (simulado)"], "tasks": ["Plan de recon para un dominio de prueba"]},
            ],
            "quiz": [{"q": "Regla #1 del pentest", "options": ["Actuar sin permiso", "Definir alcance y permiso", "Explotar primero"], "answer": 1}],
            "errors": [{"name": "Alcance mal definido", "why": "Falta acuerdo/permiso", "how": "ROE firmado", "example": "Probar IP fuera de alcance"}],
        },
        1: {"modules": [
                {"title": "Scans y enumeración", "lesson": "nmap, banners, servicios", "code": "# nmap -sV -p- target",
                 "exercises": ["Interpretar resultado de scan"], "tasks": ["Mapa de servicios de un host de lab"]},
                {"title": "Vuln básico", "lesson": "CVEs comunes web/servicios", "code": "# conceptual",
                 "exercises": ["Identificar versión vulnerable"], "tasks": ["Reporte breve de CVE sobre servicio X"]},
            ],
            "quiz": [{"q": "-sV en nmap sirve para", "options": ["Enumerar versión", "Escanear UDP", "Hacer ping"], "answer": 0}],
            "errors": [{"name": "Falsos positivos", "why": "Banner engañoso", "how": "Validar manualmente", "example": "Version spoofed"}],
        },
        2: {"modules": [
                {"title": "Explotación web básica", "lesson": "XSS/SQLi/LFI simples", "code": "# payloads mínimos",
                 "exercises": ["Encontrar input reflejado"], "tasks": ["POC SQLi en lab de prueba"]},
                {"title": "Contramedidas", "lesson": "WAF, saneo, parametrización", "code": "# conceptual",
                 "exercises": ["Mitigación para XSS"], "tasks": ["Checklist de mitigación para app de ejemplo"]},
            ],
            "quiz": [{"q": "Mejor mitigación SQLi", "options": ["Escapar manual", "Consultas parametrizadas", "Filtrar comillas"], "answer": 1}],
            "errors": [{"name": "Dañar datos reales", "why": "Explotar en prod", "how": "POC no destructiva, entornos de prueba", "example": "DROP en BD productiva"}],
        },
        3: {"modules": [
                {"title": "Post-explotación", "lesson": "shells, enum interno, credenciales", "code": "# ejemplos de enum",
                 "exercises": ["Listar usuarios/servicios"], "tasks": ["Plan de movimiento lateral en lab"]},
                {"title": "Pruebas de contraseñas", "lesson": "hashes, cracking básico", "code": "# hashcat/john (conceptual)",
                 "exercises": ["Identificar hash"], "tasks": ["Política de contraseñas recomendada"]},
            ],
            "quiz": [{"q": "Enumeración post-explotación busca", "options": ["Apagar host", "Expandir acceso", "Cambiar puertos"], "answer": 1}],
            "errors": [{"name": "Persistencia no removida", "why": "Dejar backdoor", "how": "Eliminar y reportar accesos", "example": "No limpiar cuentas de prueba"}],
        },
        4: {"modules": [
                {"title": "AD y movimiento lateral", "lesson": "Kerberoast/pass-the-hash (conceptual)", "code": "# conceptual",
                 "exercises": ["Identificar SPN vulnerable"], "tasks": ["Simular plan lateral en AD de lab"]},
                {"title": "Reporte profesional", "lesson": "Evidencias, riesgo, impacto", "code": "# estructura de reporte",
                 "exercises": ["Redactar hallazgo con riesgo/mitigación"], "tasks": ["Informe ejecutivo + técnico de un hallazgo"]},
            ],
            "quiz": [{"q": "Reporte debe incluir", "options": ["Solo PoC", "Riesgo e impacto", "Código fuente"], "answer": 1}],
            "errors": [{"name": "Hallazgos sin prioridad", "why": "No evaluar impacto", "how": "Usar rating (CVSS/propio)", "example": "Listar hallazgos sin severidad"}],
        },
        5: {"modules": [
                {"title": "Evasión básica", "lesson": "User-agents, timing, rutas, WAF bypass simple", "code": "# payloads alterados",
                 "exercises": ["Payload con doble URL-encode (lab)"], "tasks": ["Plan de evasión para WAF básico en lab"]},
                {"title": "Privilege escalation (Linux)", "lesson": "SUID, sudoers, cron, PATH", "code": "# checklist enum",
                 "exercises": ["Identificar binario SUID"], "tasks": ["Escalada en VM de práctica (guía)"]},
            ],
            "quiz": [{"q": "SUID peligroso porque", "options": ["Da root al binario", "Borra logs", "Crea usuarios"], "answer": 0}],
            "errors": [{"name": "Evasión ruidosa", "why": "Excesivo ruido en logs", "how": "Variar tiempos/agentes, menos firmas", "example": "Flood de requests idénticas"}],
        },
        6: {"modules": [
                {"title": "AD enum", "lesson": "SPN, kerberoasting (conceptual), bloodhound básico", "code": "# conceptual",
                 "exercises": ["Identificar SPN expuesto (lab)"], "tasks": ["Diagrama de ataque lateral en AD de prueba"]},
                {"title": "Persistencia y limpieza", "lesson": "Cuentas backdoor, schtasks/cron, logs", "code": "# conceptual",
                 "exercises": ["Detectar persistencia común"], "tasks": ["Procedimiento de limpieza post-test"]},
            ],
            "quiz": [{"q": "Kerberoasting busca", "options": ["Hashes de servicio", "Open ports", "Nmap rápido"], "answer": 0}],
            "errors": [{"name": "No retirar persistencia", "why": "Olvido tras prueba", "how": "Checklist de rollback", "example": "Cuenta de servicio dejada activa"}],
        },
        7: {"modules": [
                {"title": "Exploits a medida", "lesson": "Ajustar payloads, ROP básico (conceptual), fuzzing ligero", "code": "# conceptual",
                 "exercises": ["Adaptar exploit a offset distinto (lab)"], "tasks": ["Fuzzing simple sobre binario de prueba"]},
                {"title": "Infra cloud (intro)", "lesson": "Buckets públicos, claves expuestas, metadata service", "code": "# conceptual",
                 "exercises": ["Detectar bucket público (lab)"], "tasks": ["Checklist cloud básico (S3/Blob)"]},
            ],
            "quiz": [{"q": "Metadata service en cloud expone", "options": ["Clima", "Credenciales temporales", "DNS"], "answer": 1}],
            "errors": [{"name": "Fuzzing destructivo", "why": "No limitar scope", "how": "Ambiente de lab y límites de rate", "example": "Crash de servicio en prod"}],
        },
        8: {"modules": [
                {"title": "Reporte y riesgo", "lesson": "CVSS/propio, impacto, probabilidad, priorización", "code": "# plantilla de hallazgo",
                 "exercises": ["Redactar hallazgo con riesgo"], "tasks": ["Informe completo (ejecutivo+técnico) para 3 hallazgos de lab"]},
                {"title": "Red teaming (intro)", "lesson": "Objetivos, fases, OPSEC", "code": "# conceptual",
                 "exercises": ["Diseñar campaña de phishing controlada (simulada)"], "tasks": ["Plan de operación con OPSEC para un ejercicio de lab"]},
            ],
            "quiz": [{"q": "Un buen hallazgo incluye", "options": ["Solo PoC", "Riesgo + mitigación", "Código fuente"], "answer": 1}],
            "errors": [{"name": "Hallazgos sin impacto", "why": "No mapear a negocio", "how": "Describir impacto real y mitigación", "example": "Lista de CVEs sin contexto"}],
        },
        **{lvl: {"modules": [{"title": f"Pentest nivel {lvl} (placeholder)", "lesson": "Añade técnicas avanzadas (AD, evasión, cloud, red teaming)", "code": "# conceptual", "exercises": ["Ej1"], "tasks": ["Tarea/proyecto lab"]}], "quiz": [], "errors": []} for lvl in range(9, 21)}
    },

    "sec_blue": {  # Defensa/SOC
        0: {"modules": [
                {"title": "Fundamentos SOC", "lesson": "Alertas, TTPs, MITRE (overview)", "code": "# conceptual",
                 "exercises": ["Clasifica eventos: info/warn/crit"], "tasks": ["Playbook simple para phishing"]},
                {"title": "Logs y SIEM (intro)", "lesson": "fuentes de logs, normalización", "code": "# conceptual",
                 "exercises": ["Mapear campo src_ip/dst_ip"], "tasks": ["Diseña flujo de ingesta de logs"]},
            ],
            "quiz": [{"q": "MITRE ATT&CK es", "options": ["Framework de dev", "Matriz de tácticas/técnicas", "Herramienta de backup"], "answer": 1}],
            "errors": [{"name": "Alert fatigue", "why": "Exceso sin tuning", "how": "Umbrales y supresiones", "example": "Alertar cada ping"}],
        },
        1: {"modules": [
                {"title": "Detecciones básicas", "lesson": "reglas por IOCs y patrones", "code": "# pseudoregla",
                 "exercises": ["Regla de IP maliciosa"], "tasks": ["Playbook de contención para malware básico"]},
                {"title": "Respuesta a incidentes (intro)", "lesson": "triage, contención, erradicación", "code": "# conceptual",
                 "exercises": ["Lista de verificación de triage"], "tasks": ["Plan de contención para ransomware simulado"]},
            ],
            "quiz": [{"q": "Triage inicial busca", "options": ["Cerrar caso", "Evaluar severidad y alcance", "Instalar software"], "answer": 1}],
            "errors": [{"name": "Borrado de evidencias", "why": "Acción impulsiva", "how": "Preservar logs/imágenes antes de contener", "example": "Reformatear antes de adquirir disco"}],
        },
        **{lvl: {"modules": [{"title": f"Blue nivel {lvl} (placeholder)", "lesson": "Detecciones avanzadas, threat hunting, MITRE profundo", "code": "# conceptual", "exercises": ["Ej1"], "tasks": ["Playbook/Use-case avanzado"]}], "quiz": [], "errors": []} for lvl in range(2, 21)}
    },

    "sec_forensics": {
        0: {"modules": [
                {"title": "Adquisición básica", "lesson": "Imagen lógica vs física, hash", "code": "# conceptual",
                 "exercises": ["Explicar hash y cadena de custodia"], "tasks": ["Checklist de adquisición segura"]},
                {"title": "Artefactos iniciales", "lesson": "Timestamps, logs SO", "code": "# conceptual",
                 "exercises": ["Identificar zona horaria en logs"], "tasks": ["Plan de preservación de logs de sistema"]},
            ],
            "quiz": [{"q": "Hash se usa para", "options": ["Cifrar", "Integridad", "Comprimir"], "answer": 1}],
            "errors": [{"name": "Contaminar evidencia", "why": "Tocar disco origen", "how": "Imagenes, write-blocker", "example": "Montar RW el disco original"}],
        },
        1: {"modules": [
                {"title": "Timeline básico", "lesson": "MAC times, correlación", "code": "# conceptual",
                 "exercises": ["Ordenar eventos por hora"], "tasks": ["Timeline de un incidente simulado"]},
                {"title": "Memoria (intro)", "lesson": "Conceptos de volcado", "code": "# conceptual",
                 "exercises": ["Listar procesos sospechosos"], "tasks": ["Checklist de memoria en Windows/Linux"]},
            ],
            "quiz": [{"q": "Timeline sirve para", "options": ["Alertar en vivo", "Reconstruir eventos", "Cifrar discos"], "answer": 1}],
            "errors": [{"name": "Timezone errado", "why": "No ajustar zonas", "how": "Normalizar a UTC", "example": "Mezclar UTC y local sin conversión"}],
        },
        **{lvl: {"modules": [{"title": f"Forense nivel {lvl} (placeholder)", "lesson": "Análisis profundo, malware, memoria, artefactos avanzados", "code": "# conceptual", "exercises": ["Ej1"], "tasks": ["Caso práctico completo"]}], "quiz": [], "errors": []} for lvl in range(2, 21)}
    },

    "networks": {
        0: {"modules": [
                {"title": "Modelo OSI/TCP-IP", "lesson": "Capas, encapsulado", "code": "# conceptual",
                 "exercises": ["Asignar protocolos a capas"], "tasks": ["Diagrama simple de red doméstica"]},
                {"title": "TCP/UDP básico", "lesson": "Puertos, 3-way handshake", "code": "# conceptual",
                 "exercises": ["Explicar diferencia TCP/UDP"], "tasks": ["Lista de puertos críticos y su servicio"]},
            ],
            "quiz": [{"q": "Handshake es de", "options": ["UDP", "TCP", "ICMP"], "answer": 1}],
            "errors": [{"name": "NAT mal configurado", "why": "Reglas incorrectas", "how": "Revisar mapeos/ACL", "example": "PUERTO no expuesto"}],
        },
        1: {"modules": [
                {"title": "Ruteo y subredes", "lesson": "CIDR, gateways", "code": "# conceptual",
                 "exercises": ["Calcular /24 /25"], "tasks": ["Plan de direccionamiento para 3 subredes"]},
                {"title": "Seguridad básica", "lesson": "ACL, firewall", "code": "# conceptual",
                 "exercises": ["Regla para permitir 80/443"], "tasks": ["Política mínima de entrada/salida"]},
            ],
            "quiz": [{"q": "/24 equivale a", "options": ["255.255.255.0", "255.255.0.0", "255.0.0.0"], "answer": 0}],
            "errors": [{"name": "ACL demasiado permisiva", "why": "Any any", "how": "Principio de mínimo privilegio", "example": "Permitir todo hacia todo"}],
        },
        **{lvl: {"modules": [{"title": f"Redes nivel {lvl} (placeholder)", "lesson": "Ruteo avanzado, BGP, QoS, SDN, monitoreo", "code": "# conceptual", "exercises": ["Ej1"], "tasks": ["Diseño de red/lab"]}], "quiz": [], "errors": []} for lvl in range(2, 21)}
    },

    "linux_redhat": {
        0: {"modules": [
                {"title": "Shell y FS", "lesson": "pwd/ls/cd/cat, permisos básicos", "code": "ls -l\nchmod 644 file",
                 "exercises": ["Crear/mover archivos"], "tasks": ["Script que liste y archive logs"]},
                {"title": "Paquetes y servicios", "lesson": "dnf/yum, systemctl", "code": "sudo dnf install pkg\nsudo systemctl status sshd",
                 "exercises": ["Instalar y habilitar servicio"], "tasks": ["Servicio demo habilitado al arranque"]},
            ],
            "quiz": [{"q": "systemctl enable hace", "options": ["Inicia ahora", "Habilita al arranque", "Detiene"], "answer": 1}],
            "errors": [{"name": "SELinux bloquea", "why": "Contextos incorrectos", "how": "relabel/chcon o setsebool", "example": "Servicio sin contexto correcto"}],
        },
        1: {"modules": [
                {"title": "Usuarios y sudo", "lesson": "adduser/usermod, sudoers", "code": "sudo usermod -aG wheel user",
                 "exercises": ["Crear usuario y darle sudo"], "tasks": ["Política mínima de sudo para un rol"]},
                {"title": "Red y firewall", "lesson": "nmcli, firewalld", "code": "sudo firewall-cmd --add-service=http --permanent",
                 "exercises": ["Abrir puerto 8080"], "tasks": ["Reglas persistentes para web + ssh restricto"]},
            ],
            "quiz": [{"q": "firewalld zona 'public' por defecto", "options": ["Abre todo", "Permite básicos", "Bloquea todo"], "answer": 1}],
            "errors": [{"name": "Reglas no persistentes", "why": "Falta --permanent/--reload", "how": "Agregar y recargar", "example": "Regla desaparece tras reboot"}],
        },
        **{lvl: {"modules": [{"title": f"Linux/RedHat nivel {lvl} (placeholder)", "lesson": "Servicios, SELinux, automatización, HA, contenedores", "code": "# conceptual", "exercises": ["Ej1"], "tasks": ["Tarea operativa"]}], "quiz": [], "errors": []} for lvl in range(2, 21)}
    },
}

SUPPORTED_LANGS = sorted(TUTOR_CONTENT.keys())
SUPPORTED_LEVELS = sorted({lvl for lang in TUTOR_CONTENT.values() for lvl in lang.keys()})

# ============================================================
# UTILIDADES
# ============================================================
def _sanitize_text(s: str) -> str:
    s = s or ""
    if len(s) > MAX_TEXT_CHARS:
        s = s[:MAX_TEXT_CHARS] + "\n\n[Truncado por longitud]"
    return s.strip()


def _mb(bytes_size: int) -> float:
    return round(bytes_size / (1024 * 1024), 2)


def _rate_limited(user_id: int) -> bool:
    now = time.time()
    dq = rate_limit[user_id]
    while dq and now - dq[0] > RATE_LIMIT_WINDOW_S:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_MAX:
        return True
    dq.append(now)
    return False


def _is_mime_allowed(mime: str) -> bool:
    if not mime:
        return False
    mime = mime.lower().split(";")[0]
    return mime in ALLOWED_DOC_MIME


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def _reply_safe(update: Update, text: str, **kwargs):
    try:
        await update.message.reply_text(text, **kwargs)
    except Exception as e:
        logger.exception("Error enviando respuesta: %s", e)


def _remember(user_id: int, kind: str, text: str):
    user_history[user_id].append((kind, text[:500]))


def _get_history(user_id: int) -> str:
    hist = user_history.get(user_id, [])
    if not hist:
        return "No hay contexto previo."
    lines = []
    for i, (k, v) in enumerate(hist, 1):
        lines.append(f"{i}. ({k}) {v}")
    return "\n".join(lines)


def _log_ctx(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else "?"
    mid = update.message.message_id if update.message else "?"
    return f"[user={uid} msg={mid}]"


async def _download_with_retry(file_obj: File, suffix: str, max_mb: float) -> Optional[str]:
    file_info = await file_obj.get_file()
    size_mb = _mb(file_info.file_size or 0)
    if size_mb > max_mb:
        logger.warning("Archivo excede límite: %.2f MB > %.2f MB", size_mb, max_mb)
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_path = tmp.name
    tmp.close()
    last_err = None
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            await file_info.download_to_drive(tmp_path, timeout=DOWNLOAD_TIMEOUT)
            return tmp_path
        except RetryAfter as e:
            last_err = e
            logger.warning("RetryAfter (%.1fs). Intento %d/%d", e.retry_after, attempt, DOWNLOAD_MAX_RETRIES)
        except NetworkError as e:
            last_err = e
            logger.warning("NetworkError en descarga. Intento %d/%d: %s", attempt, DOWNLOAD_MAX_RETRIES, e)
    logger.error("Fallo la descarga tras %d intentos: %s", DOWNLOAD_MAX_RETRIES, last_err)
    return None

# ============================================================
# TUTOR HELPERS
# ============================================================
def tutor_set(user_id: int, lang: str, level: int):
    tutor_state[user_id] = {
        "lang": lang,
        "level": level,
        "module_idx": 0,
        "quiz_idx": 0,
        "score": 0,
    }
    save_state()


def tutor_current_module(user_id: int):
    st = tutor_state.get(user_id)
    if not st:
        return None, None, None
    lang, level, idx = st["lang"], st["level"], st["module_idx"]
    modules = TUTOR_CONTENT[lang][level]["modules"]
    if idx >= len(modules):
        return st, None, modules
    return st, modules[idx], modules


def tutor_quiz_question(user_id: int):
    st = tutor_state.get(user_id)
    if not st:
        return None, None, None
    lang, level, qidx = st["lang"], st["level"], st["quiz_idx"]
    quiz = TUTOR_CONTENT[lang][level]["quiz"]
    if qidx >= len(quiz):
        return st, None, quiz
    return st, quiz[qidx], quiz


def tutor_format_module(mod: dict, idx: int, total: int) -> str:
    exs = "\n- ".join(mod.get("exercises", []))
    tasks = "\n- ".join(mod.get("tasks", []))
    return (
        f"📚 Módulo {idx+1}/{total}: {mod['title']}\n\n"
        f"{mod['lesson']}\n\n"
        f"Ejemplo:\n```\n{mod['code']}\n```\n\n"
        f"Ejercicios:\n- {exs if exs else 'N/A'}\n\n"
        f"Tareas:\n- {tasks if tasks else 'N/A'}"
    )


def tutor_format_quiz(q: dict, idx: int, total: int) -> str:
    opts = "\n".join([f"{i+1}. {o}" for i, o in enumerate(q["options"])])
    return f"📝 Quiz {idx+1}/{total}:\n{q['q']}\n\n{opts}\n\nResponde con /answer <número>."


def tutor_list_modules(lang: str, level: int) -> str:
    mods = TUTOR_CONTENT[lang][level]["modules"]
    lines = [f"{i+1}. {m['title']}" for i, m in enumerate(mods)]
    return "\n".join(lines)


def tutor_errors(lang: str, level: int) -> str:
    errs = TUTOR_CONTENT[lang][level].get("errors", [])
    if not errs:
        return "No hay catálogo de errores aquí."
    lines = []
    for e in errs:
        lines.append(f"• {e['name']}: por qué: {e['why']}; cómo: {e['how']}; ejemplo: {e['example']}")
    return "\n".join(lines)

# ============================================================
# MENÚ
# ============================================================
def _menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Elegir ruta (/learn)", callback_data="menu_learn"),
         InlineKeyboardButton("➡️ Siguiente (/next)", callback_data="menu_next")],
        [InlineKeyboardButton("📝 Quiz (/quiz)", callback_data="menu_quiz"),
         InlineKeyboardButton("📈 Progreso (/progress)", callback_data="menu_progress")],
        [InlineKeyboardButton("ℹ️ Ayuda (/help)", callback_data="menu_help"),
         InlineKeyboardButton("🧹 Reset (/reset)", callback_data="menu_reset")],
        [InlineKeyboardButton("📂 Módulos (/modules)", callback_data="menu_modules"),
         InlineKeyboardButton("🚦 Errores comunes (/errors)", callback_data="menu_errors")],
    ])


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Menú rápido:", reply_markup=_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()
    fake_update = Update(update.update_id, message=query.message)
    if data == "menu_learn":
        await query.message.reply_text("Usa: /learn <lenguaje> <nivel>")
    elif data == "menu_next":
        await next_cmd(fake_update, context)
    elif data == "menu_quiz":
        await quiz_cmd(fake_update, context)
    elif data == "menu_progress":
        await progress_cmd(fake_update, context)
    elif data == "menu_reset":
        await reset_cmd(fake_update, context)
    elif data == "menu_help":
        await help_cmd(fake_update, context)
    elif data == "menu_modules":
        await modules_cmd(fake_update, context)
    elif data == "menu_errors":
        await errors_cmd(fake_update, context)

# ============================================================
# COMANDOS INFO
# ============================================================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    langs = ", ".join(SUPPORTED_LANGS)
    msg = (
        "📖 Ayuda rápida\n"
        "1) Envíame código/stacktrace o foto/documento: analizo y propongo solución.\n"
        "2) Tutor multi-nivel con lecciones, ejercicios, tareas y quizzes.\n\n"
        "Comandos:\n"
        "/start - saludo y menú\n"
        "/menu - teclado rápido\n"
        f"/learn <lenguaje> <nivel> (langs: {langs})\n"
        "/next - siguiente módulo\n"
        "/modules - lista de módulos del nivel actual\n"
        "/quiz - iniciar/reiniciar quiz\n"
        "/answer <n> - responder pregunta\n"
        "/errors - errores comunes del nivel\n"
        "/progress - ver progreso\n"
        "/reset - reiniciar ruta\n"
        "/context - historial reciente\n"
        "/help - esta ayuda\n\n"
        "Ejemplo: /learn python 0"
    )
    await _reply_safe(update, msg, reply_markup=_menu_keyboard())


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 Bot de soporte y tutoría:\n"
        "- Analiza código, capturas y documentos para encontrar errores y sugerir correcciones.\n"
        "- Tutor multi-nivel con lecciones, ejercicios, tareas y quizzes.\n"
        "- Acepta texto, fotos (OCR) y documentos (hasta 256MB; MIME configurables).\n"
        "Usa /help para ver comandos."
    )
    await _reply_safe(update, msg)

# ============================================================
# COMANDOS TUTOR
# ============================================================
async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args
    if len(args) < 2:
        langs = ", ".join(SUPPORTED_LANGS)
        await _reply_safe(update, f"Uso: /learn <lenguaje> <nivel>\nLenguajes: {langs}")
        return
    lang = args[0].lower()
    try:
        level = int(args[1])
    except ValueError:
        await _reply_safe(update, "Nivel debe ser número.")
        return
    if lang not in SUPPORTED_LANGS:
        await _reply_safe(update, f"Lenguaje no soportado. Opciones: {', '.join(SUPPORTED_LANGS)}")
        return
    if level not in TUTOR_CONTENT[lang]:
        await _reply_safe(update, f"Nivel no disponible para {lang}. Opciones: {sorted(TUTOR_CONTENT[lang].keys())}")
        return
    tutor_set(uid, lang, level)
    await _reply_safe(update, f"Tutor configurado: {lang} nivel {level}. Usa /next para iniciar.")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st, mod, mods = tutor_current_module(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn <lenguaje> <nivel>.")
        return
    if mod is None:
        await _reply_safe(update, "No hay más módulos en este nivel. Usa /quiz para repasar o /learn para cambiar.")
        return
    msg = tutor_format_module(mod, st["module_idx"], len(mods))
    st["module_idx"] += 1
    save_state()
    await _reply_safe(update, msg)


async def modules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = tutor_state.get(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn <lenguaje> <nivel>.")
        return
    lang, level = st["lang"], st["level"]
    await _reply_safe(update, f"Módulos de {lang} nivel {level}:\n{tutor_list_modules(lang, level)}")


async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = tutor_state.get(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn <lenguaje> <nivel>.")
        return
    st["quiz_idx"] = 0
    st["score"] = 0
    st2, q, quiz = tutor_quiz_question(uid)
    if q is None:
        await _reply_safe(update, "No hay quiz definido para este nivel.")
        return
    save_state()
    await _reply_safe(update, tutor_format_quiz(q, st2["quiz_idx"], len(quiz)))


async def answer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = tutor_state.get(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn y /quiz.")
        return
    if not context.args:
        await _reply_safe(update, "Responde: /answer <número>")
        return
    try:
        ans = int(context.args[0]) - 1
    except ValueError:
        await _reply_safe(update, "Debe ser un número.")
        return
    st2, q, quiz = tutor_quiz_question(uid)
    if q is None:
        await _reply_safe(update, "No hay más preguntas. Usa /quiz para reiniciar.")
        return
    correct = (ans == q["answer"])
    if correct:
        st["score"] += 1
        msg = "✅ Correcto."
    else:
        msg = f"❌ Incorrecto. La respuesta correcta era {q['answer']+1}."
    st["quiz_idx"] += 1
    if st["quiz_idx"] >= len(quiz):
        msg += f"\nFin del quiz. Puntuación: {st['score']}/{len(quiz)}."
    else:
        next_q = quiz[st["quiz_idx"]]
        msg += "\n\n" + tutor_format_quiz(next_q, st["quiz_idx"], len(quiz))
    save_state()
    await _reply_safe(update, msg)


async def progress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = tutor_state.get(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn <lenguaje> <nivel>.")
        return
    lang, level = st["lang"], st["level"]
    modules_total = len(TUTOR_CONTENT[lang][level]["modules"])
    modules_done = min(st["module_idx"], modules_total)
    msg = (
        f"Progreso {lang} nivel {level}:\n"
        f"- Módulos: {modules_done}/{modules_total}\n"
        f"- Último quiz: {st.get('score', 0)} puntos (se reinicia con /quiz)\n"
        f"Usa /next para continuar o /quiz para repasar."
    )
    await _reply_safe(update, msg)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tutor_state.pop(uid, None)
    save_state()
    await _reply_safe(update, "Tutor reiniciado. Usa /learn para comenzar de nuevo.")


async def errors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = tutor_state.get(uid)
    if not st:
        await _reply_safe(update, "Primero usa /learn <lenguaje> <nivel>.")
        return
    lang, level = st["lang"], st["level"]
    await _reply_safe(update, f"Errores comunes {lang} nivel {level}:\n{tutor_errors(lang, level)}")


async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    await _reply_safe(update, _get_history(uid))

# ============================================================
# CORE HANDLERS (ANÁLISIS DE CÓDIGO/IMAGEN/DOC)
# ============================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _log_ctx(update)
    if _rate_limited(update.effective_user.id):
        await _reply_safe(update, "Estás enviando muy rápido. Espera unos segundos.")
        logger.info("%s rate-limited", ctx)
        return
    text = _sanitize_text(update.message.text or "")
    if not text:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        analysis = analyze_code(text)
        _remember(update.effective_user.id, "texto", text)
    except Exception as e:
        logger.exception("%s Error analizando texto: %s", ctx, e)
        await _reply_safe(update, "Ocurrió un error analizando el texto. Intenta de nuevo.")
        return
    await _reply_safe(update, analysis)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _log_ctx(update)
    if _rate_limited(update.effective_user.id):
        await _reply_safe(update, "Estás enviando muy rápido. Espera unos segundos.")
        logger.info("%s rate-limited", ctx)
        return
    photos = update.message.photo
    if not photos:
        return
    best = photos[-1]
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    tmp_path = None
    try:
        tmp_path = await _download_with_retry(best, suffix=".jpg", max_mb=MAX_PHOTO_SIZE_MB)
        if not tmp_path:
            await _reply_safe(update, f"La foto excede el límite de {MAX_PHOTO_SIZE_MB} MB o no se pudo descargar.")
            return
        file_hash = _hash_file(tmp_path)
        if file_hash in ocr_cache:
            extracted = ocr_cache[file_hash]
        else:
            extracted = ocr_image_path(tmp_path) or ""
            ocr_cache[file_hash] = extracted
        extracted = _sanitize_text(extracted)
        if not extracted.strip():
            await _reply_safe(update, "No pude leer texto de la imagen. ¿Puedes reenviar en texto?")
            return
        analysis = analyze_code(extracted)
        _remember(update.effective_user.id, "foto", extracted[:200])
    except Exception as e:
        logger.exception("%s Error procesando foto: %s", ctx, e)
        await _reply_safe(update, "Ocurrió un error procesando la imagen.")
        return
    finally:
        if tmp_path and Path(tmp_path).exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    await _reply_safe(update, analysis)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = _log_ctx(update)
    if _rate_limited(update.effective_user.id):
        await _reply_safe(update, "Estás enviando muy rápido. Espera unos segundos.")
        logger.info("%s rate-limited", ctx)
        return
    doc = update.message.document
    if not doc:
        return
    mime = (doc.mime_type or "").lower().split(";")[0]
    size_mb = _mb(doc.file_size or 0)
    if not _is_mime_allowed(mime):
        await _reply_safe(
            update,
            f"Tipo de documento no permitido ({mime or 'desconocido'}). "
            f"Permitidos: {', '.join(sorted(ALLOWED_DOC_MIME))}. "
            f"Tamaño: {size_mb} MB."
        )
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    tmp_path = None
    try:
        tmp_path = await _download_with_retry(doc, suffix="", max_mb=MAX_DOC_SIZE_MB)
        if not tmp_path:
            await _reply_safe(update, f"El archivo excede el límite de {MAX_DOC_SIZE_MB} MB o no se pudo descargar.")
            return
        try:
            content = Path(tmp_path).read_text(errors="ignore")
        except UnicodeDecodeError:
            content = Path(tmp_path).read_text(encoding="latin-1", errors="ignore")
        content = _sanitize_text(content)
        if not content.strip():
            await _reply_safe(update, "El archivo está vacío o no pude leerlo.")
            return
        analysis = analyze_code(content)
        _remember(update.effective_user.id, "documento", content[:200])
    except Exception as e:
        logger.exception("%s Error procesando documento: %s", ctx, e)
        await _reply_safe(update, "Ocurrió un error procesando el documento.")
        return
    finally:
        if tmp_path and Path(tmp_path).exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    await _reply_safe(update, analysis)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Excepción no manejada en update=%s", update)

# ============================================================
# START
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola, soy tu bot de soporte y tutoría.\n\n"
        "• Envíame código o una captura de error: te digo el problema y cómo corregirlo.\n"
        "• Tutor: rutas por lenguaje y nivel, con lecciones, ejercicios, tareas y quizzes.\n\n"
        "Comienza con /menu o /help.",
        reply_markup=_menu_keyboard(),
    )

# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("Falta BOT_TOKEN (ni en entorno ni en config.json)")

    load_state()

    app = Application.builder().token(BOT_TOKEN).build()

    # Menú y callbacks
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback))

    # Tutor
    app.add_handler(CommandHandler("context", context_cmd))
    app.add_handler(CommandHandler("learn", learn_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("modules", modules_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("answer", answer_cmd))
    app.add_handler(CommandHandler("progress", progress_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("errors", errors_cmd))

    # Contenido
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
