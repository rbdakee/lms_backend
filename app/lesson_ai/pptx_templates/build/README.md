# Как собрана библиотека

Справка, а не часть сервиса: рантайм читает только `library.pptx` и `catalog.toml`.

`build_library.py` описывает каждый шаблон один раз (фигуры в дюймах на слайде
13.333 × 7.5) и из этого описания рисует и `.pptx` (python-pptx, уже есть в
проекте), и HTML для снимков — поэтому `previews/` не расходится с файлом.
LibreOffice не нужен. `shoot.cjs` — замер вместимости слотов и снимки в
headless Chromium; Playwright ставится во временную папку, в проект не входит.

```sh
OUT=/tmp/tpl                              # любая временная папка
IMG=app/lesson_ai/images/files            # из корня backend/
export NODE_PATH=/путь/к/node_modules     # где лежит playwright
export CHROMIUM=/путь/к/chrome-headless-shell   # необязательно

uv run python app/lesson_ai/pptx_templates/build/build_library.py measure $OUT $IMG
node app/lesson_ai/pptx_templates/build/shoot.cjs measure $OUT   # -> measure.json
uv run python app/lesson_ai/pptx_templates/build/build_library.py build $OUT $IMG
node app/lesson_ai/pptx_templates/build/shoot.cjs shots $OUT     # -> previews/*.png
cp $OUT/library.pptx $OUT/catalog.toml app/lesson_ai/pptx_templates/
cp $OUT/previews/*.png app/lesson_ai/pptx_templates/previews/
uv run pytest tests/test_pptx_templates.py
```

`max_chars` в каталоге — замер: сколько знаков обычного текста урока помещается
в слот при номинальном кегле (для `bullets` — при `max_items` пунктах), минус 10 %
на разницу переносов браузера и PowerPoint. Ряды `table` и `match_pairs` обязаны
помещаться в одну строку, `number` — тоже.

Снимки `<key>__filled.png` — тот же шаблон с текстом урока «Verb to be», 3 класс
(словарь `FILLED`), `title__filled_kz.png` — казахский пример. Незаполненный
необязательный слот на них убран, как это делает исполнитель.

Правка шаблона — только здесь и пересборкой: правка руками в PowerPoint
разойдётся со снимками и с замером.
