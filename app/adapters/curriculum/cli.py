"""Запуск импорта справочника из командной строки.

Входящий адаптер: справочник наполняется редко и вручную, ручки в API у него нет
(TEACHER_AI_BRIEF, раздел 9 — экран сверки идёт отдельной сессией). Пока запуск
выглядит так:

    uv run python -m app.adapters.curriculum.cli --subject math \\
        --ru doc399.json --kz doc399_kz.json

С `--fetch` файлы скачиваются сами, по НГР приказа:

    uv run python -m app.adapters.curriculum.cli --subject math --fetch

Скачивание живёт здесь, а не в парсере: разбор не должен зависеть от сети, иначе
его нельзя прогнать на сохранённом документе.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from sqlalchemy.orm import Session as OrmSession

from app.adapters.curriculum.parser import CurriculumParseError, parse_curriculum
from app.adapters.db.base import get_engine
from app.adapters.db.repos import CurriculumRepo
from app.application.curriculum_import import CurriculumImportService
from app.domain.errors import AppError

# НГР приказа Министра просвещения РК № 399 от 16.09.2022 в «Әділет».
ORDER_NGR = "V2200029767"
API = "https://adilet.zan.kz/api/documents/by-ngr/{ngr}?language={language}"
API_LANGUAGE = {"ru": "rus", "kz": "kaz"}


def fetch(lang: str) -> dict:
    """Скачать редакцию приказа. Авторизации не требует."""
    url = API.format(ngr=ORDER_NGR, language=API_LANGUAGE[lang])
    with urllib.request.urlopen(url, timeout=180) as response:  # noqa: S310 — адрес свой
        return json.loads(response.read().decode("utf-8"))


def load_document(path: str | None, lang: str) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return fetch(lang)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт справочника учебной программы")
    parser.add_argument("--subject", required=True, help="Код предмета, например math")
    parser.add_argument("--ru", help="Путь к JSON русской редакции приказа")
    parser.add_argument("--kz", help="Путь к JSON казахской редакции приказа")
    parser.add_argument("--fetch", action="store_true", help="Скачать редакции из «Әділет»")
    # Согласие потерять ручную вычитку: без ключа импорт поверх правленных
    # целей отбивается и называет, сколько правок пропадёт
    parser.add_argument(
        "--force", action="store_true", help="Стереть ручные правки целей и загрузить заново"
    )
    args = parser.parse_args(argv)

    if not args.fetch and not (args.ru and args.kz):
        parser.error("нужны оба файла (--ru и --kz) либо --fetch")

    parsed = {}
    for lang, path in (("ru", args.ru), ("kz", args.kz)):
        payload = load_document(None if args.fetch else path, lang)
        # Ответ API — {"document": {...}}, но сохранить могли и сам документ
        document = payload.get("document", payload)
        text = document.get("text_content") or ""
        try:
            parsed[lang] = parse_curriculum(
                text, lang, document=document, source_name=path or f"adilet:{lang}"
            )
        except CurriculumParseError as error:
            print(f"Редакция {lang}: {error}", file=sys.stderr)
            return 1

    with OrmSession(get_engine()) as db:
        service = CurriculumImportService(CurriculumRepo(db), commit=db.commit)
        try:
            result = service.load(args.subject, parsed, force=args.force)
        except AppError as error:
            # Отказ сценария — это ответ человеку, а не сбой программы:
            # трассировка стека здесь только мешает прочитать, что делать дальше
            print(error.message, file=sys.stderr)
            return 1

    print(f"Предмет: {result.subject_code}")
    for lang in ("ru", "kz"):
        print(f"  {lang}: программа #{result.program_ids[lang]}, целей {result.objectives[lang]}")
    print(
        f"Подозрений: {result.issues_total} "
        f"(ошибок {result.errors}, предупреждений {result.warnings})"
    )
    if result.errors:
        print("Публикация закрыта, пока ошибки не разобраны.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
