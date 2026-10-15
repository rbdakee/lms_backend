"""Граница между реестром админских действий и «Жоспаром» — по коду, а не по промпту.

Бриф (TEACHER_AI_BRIEF, раздел 6): `registry.py` и `masking.py`
в учительский продукт **не импортируются** — не «не вызываются»,
а не импортируются. Промпт L6: «проверяется тестом на импорты, а не обещанием
в промпте». Поэтому здесь две независимые проверки одного и того же:

- **динамическая** — чистый интерпретатор импортирует весь учительский код
  и смотрит, что попало в `sys.modules`. Ловит и цепочку через третий модуль:
  `lesson_ai` → какой-нибудь общий сервис → реестр;
- **статическая** — по AST, с файлом и строкой. Ловит импорт, который
  динамика не видит: внутри функции, в ветке, которая при импорте не исполняется.

Переиспользуется только водопровод — общий цикл `app/application/ai_loop.py`
(L6.decisions, запись 1), поэтому он проверяется отдельно: нейтрален к реестру.
И отдельно — что у учительского чата ровно два своих инструмента.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Без него реестр пуст: инструменты регистрируются импортом модулей
import app.assistant.tools  # noqa: F401
from app.assistant import registry
from app.config import get_settings
from app.lesson_ai import order_tools
from app.lesson_ai.order_tools import OrderTools
from app.main import app as fastapi_app
from tests.conftest import ADMIN_PHONE, PHONE, age_codes, login, make_admin

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

BACKEND = Path(__file__).resolve().parent.parent
LESSON_AI_DIR = BACKEND / "app" / "lesson_ai"
AI_LOOP = BACKEND / "app" / "application" / "ai_loop.py"
# Сценарии учительского продукта вне пакета `app/lesson_ai/` и общий цикл
SCENARIOS = (
    "app.application.lesson_ai_chat",
    "app.application.lesson_kits",
    "app.application.lesson_materials",
    "app.application.lesson_ai_queue",
    "app.application.ai_loop",
)
FORBIDDEN = "app.assistant"


def lesson_ai_modules() -> list[str]:
    """Все модули пакета по файлам, а не через `pkgutil.walk_packages`:
    у `games/` нет `__init__.py`, и обход пакетов молча пропустил бы его —
    а новый модуль должен попадать в проверку сам, без правки теста."""
    names = []
    for path in sorted(LESSON_AI_DIR.rglob("*.py")):
        parts = list(path.relative_to(BACKEND).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


def module_file(name: str) -> Path:
    path = BACKEND.joinpath(*name.split("."))
    return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")


def teacher_files() -> list[Path]:
    return sorted(LESSON_AI_DIR.rglob("*.py")) + [module_file(name) for name in SCENARIOS]


# -- динамика: чистый интерпретатор -------------------------------------------

# Скрипт для отдельного процесса. В процессе pytest весь `app` уже загружен
# (conftest импортирует `app.main`), и `sys.modules` там ничего не докажет.
# Перехват `__import__` запоминает, кто первым притащил каждый модуль, —
# чтобы упавший тест называл цепочку, а не только имя.
_PROBE = r"""
import builtins, importlib, json, sys

importer = {}
_real = builtins.__import__

def _tracing(name, globals=None, locals=None, fromlist=(), level=0):
    before = set(sys.modules)
    try:
        return _real(name, globals, locals, fromlist, level)
    finally:
        who = (globals or {}).get("__name__", "?")
        for new in set(sys.modules) - before:
            # Вложенный импорт завершается раньше внешнего: setdefault
            # оставляет ближайшего виновника, а не корень
            importer.setdefault(new, who)

builtins.__import__ = _tracing
for root in json.loads(sys.argv[1]):
    before = set(sys.modules)
    importlib.import_module(root)
    for new in set(sys.modules) - before:
        importer.setdefault(new, "<тест: " + root + ">")

found = sorted(m for m in sys.modules if m == "app.assistant" or m.startswith("app.assistant."))
chains = {}
for mod in found:
    chain, cur = [mod], mod
    while cur in importer and not importer[cur].startswith("<") and len(chain) < 50:
        cur = importer[cur]
        chain.append(cur)
    chain.append(importer.get(cur, "?"))
    chains[mod] = " <- ".join(chain)
print(json.dumps({"found": found, "chains": chains}, ensure_ascii=False))
"""


def probe(roots: list[str]) -> dict:
    """Импорт `roots` в чистом интерпретаторе: что из `app.assistant` попало
    в `sys.modules` и кто его притащил."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(roots)],
        cwd=BACKEND,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, f"импорт в чистом интерпретаторе упал:\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def explain(found: dict) -> str:
    lines = [f"  {chain}" for chain in found["chains"].values()]
    return "учительский код притащил модули ассистента админки:\n" + "\n".join(lines)


def test_module_list_sees_every_subpackage():
    """Страховка самого обхода: если он перестанет видеть подпакеты,
    динамический тест станет зелёным на пустом месте."""
    names = lesson_ai_modules()
    for expected in (
        "app.lesson_ai.order_tools",
        "app.lesson_ai.prompts.order_chat",
        "app.lesson_ai.schemas",
        "app.lesson_ai.games.select",
    ):
        assert expected in names


def test_teacher_code_does_not_load_the_assistant():
    found = probe(lesson_ai_modules() + list(SCENARIOS))
    assert found["found"] == [], explain(found)


def test_ai_loop_alone_does_not_load_the_assistant():
    found = probe(["app.application.ai_loop"])
    assert found["found"] == [], explain(found)


# -- статика: AST ---------------------------------------------------------------


def _imports(path: Path) -> list[tuple[int, str]]:
    """Все импорты файла, включая вложенные в функции и ветки, и строки
    из `importlib.import_module(...)` / `__import__(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(path, node)
            out.append((node.lineno, module))
            # `from app import assistant` — тот же пакет, только другой записью
            out += [(node.lineno, f"{module}.{alias.name}") for alias in node.names]
        elif isinstance(node, ast.Call) and _is_dynamic_import(node.func):
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append((node.lineno, f"<динамический> {arg.value}"))
    return out


def _absolute(path: Path, node: ast.ImportFrom) -> str:
    """`from ..assistant import registry` — тот же импорт, что и абсолютный:
    относительная запись не должна проскакивать мимо проверки."""
    if node.level == 0:
        return node.module or ""
    package = list(path.relative_to(BACKEND).parent.parts)
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _is_dynamic_import(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return isinstance(func, ast.Attribute) and func.attr in ("__import__", "import_module")


def _offences(path: Path, banned) -> list[str]:
    rel = path.relative_to(BACKEND)
    return [f"{rel}:{line}: {name}" for line, name in _imports(path) if banned(name)]


def _touches_assistant(name: str) -> bool:
    if name.startswith("<динамический>"):
        # Строка, собранная в рантайме, статикой не видна; видна — та,
        # что лежит в коде целиком, и в ней достаточно слова
        return "assistant" in name
    return name == FORBIDDEN or name.startswith(FORBIDDEN + ".")


def test_teacher_code_has_no_assistant_imports_in_source():
    offences = [line for path in teacher_files() for line in _offences(path, _touches_assistant)]
    listed = "\n".join(offences)
    assert offences == [], f"импорт ассистента админки в учительском коде:\n{listed}"


def test_ai_loop_imports_neither_product():
    def banned(name: str) -> bool:
        return _touches_assistant(name) or "lesson_ai" in name

    offences = _offences(AI_LOOP, banned)
    assert offences == [], "общий цикл знает о продукте:\n" + "\n".join(offences)


def test_ai_loop_names_no_tool_of_either_product():
    """В общем цикле нет ни одного имени инструмента: чей цикл, решает тот,
    кто его собрал (L6.decisions, запись 1)."""
    text = AI_LOOP.read_text(encoding="utf-8")
    names = set(registry.all_tools()) | {item["function"]["name"] for item in order_tools.SCHEMAS}
    assert len(names) > 2
    mentioned = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", text))
    assert mentioned == []


# -- инструменты учительского чата ----------------------------------------------


def test_order_chat_has_exactly_two_tools():
    names = {item["function"]["name"] for item in OrderTools().schemas()}
    assert names == {"find_objectives", "get_objective"}
    assert names.isdisjoint(registry.all_tools())


def test_admin_tool_is_unknown_to_the_order_chat(monkeypatch):
    """Модель, которой в чужом промпте подсказали админский инструмент,
    получает отказ — и реестр при этом не зовётся вовсе."""
    calls = []
    monkeypatch.setattr(registry, "call", lambda name, *args, **kw: calls.append(name) or {})
    tools = OrderTools()
    results = {name: tools.call(name, {}, None) for name in registry.all_tools()}
    assert calls == [], f"учительский чат исполнил через реестр админа: {calls[:5]}"
    accepted = sorted(
        name
        for name, result in results.items()
        if (result.get("error") or {}).get("code") != "unknown_tool"
    )
    assert accepted == [], f"учительский чат не отказал в админских инструментах: {accepted[:5]}"


# -- роутер: только учительская кука --------------------------------------------


def chat_routes() -> list[tuple[str, str]]:
    """Ручки чата и справочника — из схемы OpenAPI самого приложения: новая
    ручка попадает в проверку без правки теста. Схема, а не `app.routes`:
    подключённые роутеры FastAPI держит обёртками без `path`."""
    out = []
    for path, operations in fastapi_app.openapi()["paths"].items():
        if path.startswith(("/lesson_ai/chats", "/lesson_ai/objectives")):
            concrete = path.replace("{chat_id}", "1")
            out += [(method.upper(), concrete) for method in sorted(operations)]
    return out


def test_chat_routes_are_found():
    methods = {method for method, _ in chat_routes()}
    assert len(chat_routes()) >= 6
    assert {"GET", "POST", "PATCH"} <= methods


@pytest.mark.parametrize(("method", "path"), chat_routes())
def test_chat_routes_need_login(client, method, path):
    resp = client.request(method, path, json={"text": "завтра 2 класс"})
    assert resp.status_code == 401, (method, path, resp.text)


def test_admin_cookie_is_not_a_teacher_cookie(client, sms):
    """Кука админки, пришедшая без `Origin` админки, учителя не заменяет:
    на площадке её имя не читается вовсе (TEACHER_AI_BRIEF, раздел 6:
    «кто вошёл — учитель, кука площадки»)."""
    admin_origins = get_settings().admin_origins
    if not admin_origins:
        pytest.skip("без ADMIN_ORIGINS кука одна на оба приложения — различать нечего")
    headers = {"Origin": admin_origins[0]}
    sent = {"phone": ADMIN_PHONE, "consent": True}
    assert client.post("/auth/request_code", json=sent, headers=headers).status_code == 200
    _, code = sms.sent[-1]
    resp = client.post(
        "/auth/verify_code", json={"phone": ADMIN_PHONE, "code": code}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    age_codes(2)
    make_admin(resp.json()["phone"])
    assert client.post("/lesson_ai/chats").status_code == 401
    assert client.get("/lesson_ai/objectives", params={"q": "сложение"}).status_code == 401


def test_teacher_cookie_opens_the_chat(client, sms):
    """Обратная сторона: обычный вход учителя чат открывает — иначе 401
    выше доказывали бы только то, что ручки сломаны."""
    login(client, sms, PHONE)
    assert client.post("/lesson_ai/chats").status_code == 201
