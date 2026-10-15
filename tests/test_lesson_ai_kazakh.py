"""«Жоспар»: казахский ҚМЖ в Word — казахский целиком.

Язык документа — язык обучения, а не интерфейса (TEACHER_AI_BRIEF, раздел 1):
учитель с русским интерфейсом ведёт казахский класс, и завуч казахской школы
принимает план только на казахском. Полурусский документ — хуже, чем никакой:
его приходится перепечатывать руками, а русская подпись в казахской форме
выдаёт машинный перевод. Поэтому проверяется файл, который реально скачал
учитель, а не словарь подписей: подписи формы, цель обучения из казахской
редакции приказа (не перевод русской), буквы, шрифт, язык проверки орфографии
и имя файла.

Генератор — настоящий, ИИ — подставной, задания берутся руками (как
в `test_lesson_ai_kits.py`): черновик модели — живой казахский ответ gpt-4.1.
"""

import re
import zipfile
from io import BytesIO
from urllib.parse import unquote

import pytest
from docx import Document
from docx.oxml.ns import nsmap, qn
from lxml import etree

from app.adapters.docx.lesson_plan import FONT, LABELS, WORD_LANG
from app.lesson_ai.schemas.lesson_plan import STAGE_MINUTES
from tests.conftest import login_named
from tests.test_admin_curriculum import make_objective, make_program, make_section, make_subject
from tests.test_lesson_ai_kits import (
    CODE,
    DRAFT,
    OBJECTIVE_TEXT,
    TOPIC,
    run_next,
    sent_message,
)

# Доступ к «Жоспару» (L7) в этих тестах открыт: они проверяют продукт,
# а сама проверка доступа — в `test_lesson_ai_access.py`
pytestmark = pytest.mark.usefixtures("lesson_ai_open")

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

KZ_OBJECTIVE = "екітаңбалы сандарды ондықтан аттамай ауызша қосу және азайтуды орындау"
KZ_SECTION = "Сандар және шамалар"
KZ_SUBSECTION = "Сандарға амалдар қолдану"
KZ_TOPIC = "Екітаңбалы сандарды қосу және азайту"
RU_SECTION = "Числа и величины"
RU_SUBSECTION = "Операции над числами"

# Учитель вымышленный; казахские буквы в ФИО — ещё одна строка шапки,
# которая обязана доехать до документа без потерь
TEACHER = {"last_name": "Сәрсенқызы", "first_name": "Әйгерім", "middle_name": "Ұланқызы"}

KAZAKH_LOWER = "әғқңөұүһі"
KAZAKH_UPPER = "ӘҒҚҢӨҰҮҺІ"

# Казахские подписи формы — своей копией, а не из `LABELS`: тест, который
# берёт ожидание из того же словаря, что и код, промолчит, если в словарь
# вместо казахской подписи попадёт русская
KZ_LABELS = {
    "title": "Қысқа мерзімді жоспар",
    "section": "Бөлім",
    "teacher": "Педагогтің аты-жөні",
    "date": "Күні",
    "grade": "Сынып",
    "present": "Қатысушылар саны",
    "topic": "Сабақтың тақырыбы",
    "learning_objectives": (
        "Осы сабақта қол жеткізілетін оқу мақсаттары (оқу бағдарламасына сілтеме)"
    ),
    "lesson_goals": "Сабақтың мақсаты",
    "assessment_criteria": "Бағалау критерийлері",
    "language_goals": "Тілдік мақсаттар",
    "vocabulary": "Пәнге қатысты сөздік қор және терминология",
    "phrases": "Диалог пен жазылым үшін пайдалы тіркестер",
    "values": "Құндылықтарды дарыту",
    "cross_curricular_links": "Пәнаралық байланыстар",
    "prior_knowledge": "Алдыңғы білім",
    "course": "Сабақтың барысы",
    "stage": "Сабақтың кезеңі / уақыты",
    "teacher_actions": "Педагогтің әрекеті",
    "students_actions": "Оқушылардың әрекеті",
    "resources": "Ресурстар",
    "stage_beginning": "Сабақтың басы",
    "stage_middle": "Сабақтың ортасы",
    "stage_end": "Сабақтың соңы",
    "minutes": "мин",
    "differentiation": "Саралау",
    "assessment": "Бағалау",
    "health_and_safety": "Денсаулық және қауіпсіздік техникасының сақталуы",
    "reflection": "Сабақ бойынша рефлексия",
}

# Черновик ҚМЖ от модели — живой казахский ответ gpt-4.1 из прогона L3 без
# раздела и целей обучения (их кладёт сервер). В живом ответе не нашлось
# буквы «һ» и заглавных Ғ, Ұ, І — дописаны две фразы в языковые цели и одна
# в ценности, чтобы проверка букв покрывала все девять.
KZ_DRAFT = {
    "lesson_goals": [
        "Екітаңбалы сандарды ондықтан аспай ауызша қосады.",
        "Екітаңбалы сандарды ондықтан аспай ауызша азайтады.",
        "Қосуға және азайтуға қатысты есептерді шешеді.",
    ],
    "assessment_criteria": [
        "Екітаңбалы сандарды ондықтан аттамай дұрыс қосады.",
        "Екітаңбалы сандарды ондықтан аттамай дұрыс азайтады.",
        "Өз жауабын түсіндіріп, ауызша жеткізеді.",
    ],
    "language_goals": {
        "phrases": [
            "Мен алдымен бірліктерді қосамын/азайтамын.",
            "Ондықтан аспайтын есептерді шығарамын.",
            "Менің жауабым...",
            "Мен былай түсіндіріп бере аламын...",
            "Ұқыпты жазамын, Іштей санаймын.",
            "Ғажап, жауабым дұрыс шықты!",
        ],
        "vocabulary": [
            "екітаңбалы сан",
            "қосу",
            "азайту",
            "ондықтан аттамау",
            "теңдік",
        ],
    },
    "values": (
        "Оқушылар бір-бірімен жұптық тапсырмаларда достықты, өзара көмек көрсетуді"
        " үйренеді. Сыныптағы ортақ жиһазды ұқыпты ұстайды."
    ),
    "cross_curricular_links": (
        "Логикалық ойлау және сөйлеу арқылы қазақ тілі, өмірлік дағдыларымен байланысады."
    ),
    "prior_knowledge": "Оқушылар біртаңбалы сандарды ауызша қосып, азайта алады.",
    "stage_beginning": [
        {
            "teacher": (
                "Сәлемдесу. \"Бүгінгі сабағымызда екітаңбалы сандарды ондықтан аттамай ауызша "
                "қосу және азайтуды үйренеміз\" деп айтады. \"9+5\" мысалын ауызша шештіріп, "
                "қандай есептер оңай екенін сұрайды. \"24+3\" және \"53-2\" сандарының қайсысында "
                "ондықтан аттамау орындалатынын сұрайды."
            ),
            "students": (
                "Сәлемдеседі, мұғалімнің сұрақтарына жауап береді. Мысалдарды ауызша шығарып, "
                "пікірімен бөліседі."
            ),
            "resources": [
                "дәптер",
                "интерактивті тақта",
            ],
        },
    ],
    "stage_middle": [
        {
            "teacher": (
                "Екітаңбалы сандарды ондықтан аттамай қосу үлгісін көрсетеді: \"32+5\" деген "
                "мысалды тақтаға жазып, 32-нің үстіне 5 қосқанда, 32-де 2 бірлік, 5-ті қосқанда"
                " барлығы 7 бірлік болатынын, ондықтан аспайтынын түсіндіреді."
            ),
            "students": "Тыңдайды, қолына есепшот алып, мұғаліммен бірге есепті шешеді.",
            "resources": [
                "есепшот",
                "тақта",
            ],
        },
        {
            "teacher": (
                "Оқушыларға жұппен орындау үшін үш мысал береді: \"41+7\", \"24+5\", \"56+3\". "
                "Нәтижелерін салыстырып, оларды түсіндіруді сұрайды."
            ),
            "students": (
                "Жұптасып әр мысалды ауызша шығарады, жауабын айтады,"
                " қалай шешкенін түсіндіреді."
            ),
            "resources": [
                "карточкалар",
            ],
        },
        {
            "teacher": (
                "Екітаңбалы сандарды ондықтан аттамай азайтуды түсіндіреді. Мысалы: \"57-5\" "
                "дегенде, 57-дегі 7 бірліктен 5-ті азайтады, 2 қалады. Басқа мысалдарды "
                "(\"44-3\") сұрайды."
            ),
            "students": "Мысалдарды дәптерге шығарады, жауабын дауыстап оқиды.",
            "resources": [
                "дәптер",
                "есепшот",
            ],
        },
        {
            "teacher": (
                "Қысқа сергіту сәтін ұйымдастырады: әр оқушы өзі айтқан саннан 3-ті азайтады "
                "немесе 4-ті қосады (мыс. 28+4). Балалар бір-біріне сұрақ береді."
            ),
            "students": "Орындарынан тұрып, мысалдарды ауызша орындайды.",
            "resources": [],
        },
        {
            "teacher": (
                "Жеке жұмыс: \"62+6\", \"25-3\", \"37+2\", \"48-7\" мысалдарын береді. Есептерді "
                "ондықтан аттамай ауызша шығаруды тапсырады. Жылдам орындағандарға қосымша: "
                "\"53+5\", \"82-2\"."
            ),
            "students": (
                "Әр оқушы өз бетімен мысалдарды орындайды,"
                " дайын болған соң жауаптарын тексереді."
            ),
            "resources": [
                "дәптер",
            ],
        },
    ],
    "stage_end": [
        {
            "teacher": (
                "Сабақ соңында балалардан: \"Бүгін қандай жаңа нәрсе үйрендіңдер? Екітаңбалы "
                "сандарды қалай қосамыз және азайтамыз?\" деп сұрайды. Рефлексия үшін \"Саған қай"
                " есеп оңай болды? Қайсысы қиын?\" сұрақтарын қояды. Үй жұмысына: 33+4, 42-3 "
                "есептерін береді."
            ),
            "students": (
                "Сұрақтарға жауап береді, алған әсерімен бөліседі."
                " Үй тапсырмасын жазып алады."
            ),
            "resources": [
                "дәптер",
            ],
        },
    ],
    "differentiation": (
        "Қиындық көрген балаларға есепші құралын пайдалануға, бірге талқылауға мүмкіндік "
        "береді. Тапсырманы тез орындағандарға күрделірек мысалдар ауызша береді (мысалы: үш "
        "амалмен: \"25+3-2\")."
    ),
    "assessment": (
        "Оқушылардың жауаптарына мұқият құлақ асады, дұрыс орындалғанын ауызша мақтап, "
        "түсіндіргенін тыңдайды. Жұптық және жеке жұмыс кезінде қадағалап, сұрақтарға нақты "
        "жауап бергенін қолдайды."
    ),
    "health_and_safety": (
        "Сергіту сәтінде орындарынан тұрғызып, дене шынықтыру жаттығуларын бірге жасатады және "
        "көзге, саусақтарға арналған жаттығуларды қамтамасыз етеді."
    ),
    "reflection": [
        "Сабақта барлық оқушы мақсатқа жетті ме?",
        "Кімге көбірек қолдау қажет болды?",
        "Оқушылар екітаңбалы сандарды ондықтан аттамай қоса және азайта алды ма?",
    ],
}

# Новый шаг середины урока — ответ модели на перегенерацию блока
KZ_STAGE_MIDDLE = [
    {
        "teacher": (
            "«Қатарлар жарысы» ойынын өткізеді: әр қатарға ондықтан аттамайтын бір мысал"
            " береді (43+25, 68-16, 71+14). Һ әрпі бар сөзді де еске түсіреді: «гауһар»."
        ),
        "students": "Қатар бойынша мысалды ауызша шығарып, жауабын тақтаға жазады.",
        "resources": ["мысал жазылған карточкалар"],
    }
]


def seed_objective(subject_id: int, lang: str, section: str, subsection: str, text: str):
    """Одна опубликованная редакция программы с целью `CODE`. Раздел
    и подраздел — на языке редакции: в документ они идут дословно, и русский
    раздел в казахской программе испортил бы проверку «ни слова по-русски»."""
    program = make_program(subject_id, lang=lang, status="published")
    top = make_section(program.id, "1", section)
    sub = make_section(program.id, "1.2", subsection, parent_id=top.id)
    return make_objective(program.id, sub.id, CODE, text=text)


def kz_objective(subject_id: int | None = None):
    subject_id = subject_id or make_subject().id
    return seed_objective(subject_id, "kz", KZ_SECTION, KZ_SUBSECTION, KZ_OBJECTIVE)


def ru_objective(subject_id: int):
    return seed_objective(subject_id, "ru", RU_SECTION, RU_SUBSECTION, OBJECTIVE_TEXT)


def generate(client, ai, objective_id: int, draft: dict, topic: str, **headers) -> dict:
    """Заказ и одна генерация — по одной за раз, чтобы черновик достался
    именно своему комплекту."""
    resp = client.post(
        "/lesson_ai/kits",
        json={"objective_ids": [objective_id], "topic": topic, "kinds": ["lesson_plan"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ai.returns_json(draft)
    assert run_next(ai)
    kit = client.get(f"/lesson_ai/kits/{resp.json()['id']}", headers=headers).json()
    assert kit["status"] == "ready", kit
    return kit


def download(client, kit: dict, **headers):
    material_id = kit["materials"][0]["id"]
    resp = client.get(f"/lesson_ai/materials/{material_id}/file", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == DOCX
    return resp


def paragraphs(resp) -> list[str]:
    """Текст документа абзацами, как их видит Word. Берётся из XML, а не
    через `table.cell`: склеенная по вертикали ячейка этапа python-docx
    отдаёт по разу на каждую строку, и счёт букв и «?» поехал бы."""
    body = Document(BytesIO(resp.content)).element.body
    return ["".join(p.xpath(".//w:t/text()")) for p in body.iter(qn("w:p"))]


def label_lines(key: str, label: str) -> list[str]:
    """Как подпись стоит в документе отдельным абзацем: лексика и выражения —
    с двоеточием, минуты — после границ этапа."""
    if key in ("vocabulary", "phrases"):
        return [f"{label}:"]
    if key == "minutes":
        return [f"{start}–{end} {label}" for start, end in STAGE_MINUTES.values()]
    return [label]


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for part in value.values() for item in strings(part)]
    return [item for part in value for item in strings(part)]


def xml_part(resp, name: str) -> bytes:
    return zipfile.ZipFile(BytesIO(resp.content)).read(name)


def normal_rpr(resp):
    styles = etree.fromstring(xml_part(resp, "word/styles.xml"))
    [normal] = styles.xpath("w:style[@w:styleId='Normal']", namespaces={"w": nsmap["w"]})
    return normal.find(qn("w:rPr"))


def assert_language_of_form(lines: list[str], lang: str) -> None:
    """Все подписи формы своего языка на месте, и ни одной подписи чужого."""
    own, other = (KZ_LABELS, LABELS["ru"]) if lang == "kz" else (LABELS["ru"], KZ_LABELS)
    text = "\n".join(lines)
    for key, label in own.items():
        for line in label_lines(key, label):
            assert line in lines, (key, line)
        if other[key] != label:
            assert other[key] not in text, key


@pytest.fixture
def kz_kit(client, sms, ai) -> dict:
    """Казахский комплект готов, учитель с казахским ФИО вошёл."""
    objective = kz_objective()
    login_named(client, sms, **TEACHER)
    return generate(client, ai, objective.id, KZ_DRAFT, KZ_TOPIC)


# -- язык документа -------------------------------------------------------


def test_kazakh_objective_makes_a_kazakh_document(client, kz_kit):
    """Языка в заказе нет — его знает цель, взятая из казахской редакции.
    Комплект казахский, и документ открывается казахским заголовком формы."""
    assert kz_kit["lang"] == "kz"
    assert kz_kit["objectives"][0] == {
        "id": kz_kit["objectives"][0]["id"],
        "code": CODE,
        "text": KZ_OBJECTIVE,
        "marker": "",
    }

    lines = paragraphs(download(client, kz_kit))

    assert lines[0] == "Қысқа мерзімді жоспар"
    assert "Краткосрочный план урока" not in "\n".join(lines)


@pytest.mark.parametrize("key", list(LABELS["kz"]))
def test_every_label_is_kazakh_and_none_is_russian(client, kz_kit, key):
    """Каждая подпись формы — по отдельности: одна забытая русская подпись
    («Ход урока» посреди казахского плана) — уже повод завучу вернуть план.
    «мин» одинаков в обоих языках, у него проверяется только присутствие."""
    lines = paragraphs(download(client, kz_kit))
    text = "\n".join(lines)

    for line in label_lines(key, KZ_LABELS[key]):
        assert line in lines
    if LABELS["ru"][key] != KZ_LABELS[key]:
        assert LABELS["ru"][key] not in text


def test_both_languages_label_the_same_form():
    """Строка формы без подписи на одном из языков — дыра в документе:
    наборы ключей у двух языков и у копии в тесте совпадают."""
    assert set(LABELS["kz"]) == set(LABELS["ru"]) == set(KZ_LABELS)


def test_header_is_kazakh_and_taken_from_the_kit(client, kz_kit):
    """Шапка — из комплекта и справочника: казахские раздел и подраздел
    программы, ФИО из профиля, класс и тема как написал учитель."""
    lines = paragraphs(download(client, kz_kit))

    for line in (
        KZ_SECTION,
        KZ_SUBSECTION,
        "Сәрсенқызы Әйгерім Ұланқызы",
        "2",
        KZ_TOPIC,
    ):
        assert line in lines, line
    text = "\n".join(lines)
    assert RU_SECTION not in text
    assert RU_SUBSECTION not in text


# -- цель обучения --------------------------------------------------------


def test_objective_is_the_kazakh_dictionary_line_with_code(client, sms, ai):
    """Цель — строка казахской редакции приказа дословно, с кодом. Русская
    редакция с тем же кодом заведена рядом нарочно: сервер обязан взять цель
    по номеру из заказа, а не по коду — иначе в казахский план приехала бы
    русская формулировка или её перевод."""
    subject = make_subject()
    ru_objective(subject.id)
    objective = kz_objective(subject.id)
    login_named(client, sms, **TEACHER)

    kit = generate(client, ai, objective.id, KZ_DRAFT, KZ_TOPIC)
    lines = paragraphs(download(client, kit))

    assert f"{CODE} {KZ_OBJECTIVE}" in lines
    text = "\n".join(lines)
    assert OBJECTIVE_TEXT not in text
    assert "выполнять устно" not in text
    # Модель тоже писала урок от казахской цели — без кода, но её текстом
    message = sent_message(ai)
    assert f"Цель обучения по программе: {KZ_OBJECTIVE}" in message.splitlines()
    assert OBJECTIVE_TEXT not in message
    assert kit["materials"][0]["content"]["learning_objectives"] == [
        {"code": CODE, "text": KZ_OBJECTIVE, "marker": ""}
    ]


# -- буквы ----------------------------------------------------------------


def test_kazakh_letters_arrive_intact(client, kz_kit):
    """Все девять казахских букв доезжают как есть: ни «�», ни «?» на их
    месте. Подмена буквы — тихая порча, её видно только на бумаге. «?» в
    документе ровно столько, сколько настоящих вопросов было в черновике."""
    draft_text = "".join(strings(KZ_DRAFT))
    # Черновик сам покрывает все девять — иначе проверка ничего не доказывает
    assert set(KAZAKH_LOWER) <= set(draft_text)

    lines = paragraphs(download(client, kz_kit))
    text = "\n".join(lines)

    for line in strings(KZ_DRAFT):
        assert line in text, line
    for letter in KAZAKH_LOWER + KAZAKH_UPPER:
        assert text.count(letter) >= draft_text.count(letter), letter
    assert set(draft_text) <= set(text)
    assert "�" not in text
    assert text.count("?") == draft_text.count("?")


# -- оформление -----------------------------------------------------------


@pytest.mark.parametrize("lang", ["kz", "ru"])
def test_normal_style_names_language_and_font(client, sms, ai, lang):
    """Язык проверки орфографии — язык обучения: без `kk-KZ` Word
    подчёркивает казахский план целиком как русский с ошибками. Шрифт — во
    всех четырёх слотах: неназначенный слот Word заполнил бы шрифтом темы,
    а в нём казахских букв может не быть."""
    subject = make_subject()
    if lang == "kz":
        objective, draft, topic = kz_objective(subject.id), KZ_DRAFT, KZ_TOPIC
    else:
        objective, draft, topic = ru_objective(subject.id), DRAFT, TOPIC
    login_named(client, sms, **TEACHER)
    resp = download(client, generate(client, ai, objective.id, draft, topic))

    rpr = normal_rpr(resp)
    langs = rpr.findall(qn("w:lang"))
    assert [item.get(qn("w:val")) for item in langs] == [WORD_LANG[lang]]
    assert WORD_LANG[lang] == {"kz": "kk-KZ", "ru": "ru-RU"}[lang]
    fonts = rpr.find(qn("w:rFonts"))
    for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        assert fonts.get(qn(slot)) == "Times New Roman", slot
    assert FONT == "Times New Roman"
    # Тема шрифта в том же элементе важнее явного имени — её быть не должно
    assert not [name for name in fonts.attrib if name.endswith("Theme")]

    # Ни один абзац не спорит со стилем своим языком или шрифтом
    document = etree.fromstring(xml_part(resp, "word/document.xml"))
    assert document.findall(".//" + qn("w:lang")) == []
    assert document.findall(".//" + qn("w:rFonts")) == []


def test_document_xml_is_utf8_with_letters_as_is(client, kz_kit):
    """XML документа — UTF-8, и казахские буквы лежат в нём буквами, а не
    ссылками `&#1179;` и не вопросами: иначе ломается поиск по файлу и любая
    программа, которая читает .docx не через Word."""
    raw = xml_part(download(client, kz_kit), "word/document.xml")

    assert re.match(rb"<\?xml[^>]*encoding=['\"]UTF-8['\"]", raw, re.IGNORECASE)
    decoded = raw.decode("utf-8")
    assert not re.search(r"&#x?[0-9a-fA-F]+;", decoded)
    for letter in KAZAKH_LOWER:
        assert letter in decoded, letter
    assert f"{CODE} {KZ_OBJECTIVE}" in decoded
    assert "�" not in decoded


# -- имя файла ------------------------------------------------------------


def test_file_name_is_kazakh_with_ascii_fallback(client, kz_kit):
    """Учитель складывает планы в одну папку и различает их по теме — имя
    в UTF-8 обязано раскодироваться в казахскую тему буква в букву. Старому
    клиенту без `filename*` остаётся ASCII-имя с номером комплекта."""
    disposition = download(client, kz_kit).headers["content-disposition"]

    assert disposition.startswith("attachment;")
    fallback = re.search(r'filename="([^"]*)"', disposition).group(1)
    assert fallback == f"lesson_plan_{kz_kit['id']}.docx"
    assert fallback.isascii()
    encoded = re.search(r"filename\*=UTF-8''(\S+)", disposition).group(1)
    assert encoded.isascii()
    assert unquote(encoded, encoding="utf-8", errors="strict") == (
        "ҚМЖ — Екітаңбалы сандарды қосу және азайту.docx"
    )


# -- язык интерфейса ------------------------------------------------------


def test_interface_language_does_not_change_the_document(client, sms, ai):
    """Русский интерфейс учителя и русский браузер — не повод перевести
    казахский план: язык документа — язык класса, а не того, кто его готовит."""
    objective = kz_objective()
    login_named(client, sms, lang="ru", **TEACHER)
    assert client.get("/me").json()["lang"] == "ru"
    ru_browser = {"Accept-Language": "ru-RU,ru;q=0.9"}

    kit = generate(client, ai, objective.id, KZ_DRAFT, KZ_TOPIC, **ru_browser)
    lines = paragraphs(download(client, kit, **ru_browser))

    assert kit["lang"] == "kz"
    assert lines[0] == "Қысқа мерзімді жоспар"
    assert_language_of_form(lines, "kz")
    assert f"{CODE} {KZ_OBJECTIVE}" in lines
    assert "Язык обучения: казахский" in sent_message(ai).splitlines()


# -- перегенерация блока --------------------------------------------------


def test_regenerated_block_stays_kazakh(client, ai, kz_kit):
    """Переписанный блок — на языке класса: сообщение модели называет
    казахский язык обучения и требует казахский текст блока. Новый блок виден
    в следующем же скачанном файле, соседние блоки и подписи — прежние."""
    material_id = kz_kit["materials"][0]["id"]
    ai.returns_json({"stage_middle": KZ_STAGE_MIDDLE})

    resp = client.post(
        f"/lesson_ai/materials/{material_id}/blocks/stage_middle/regenerate",
        json={"instruction": "ойын қосыңыз"},
        headers={"Accept-Language": "ru"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == KZ_STAGE_MIDDLE
    call = ai.calls[-1]
    assert call["name"] == "lesson_plan_stage_middle"
    message = sent_message(ai)
    assert "Весь текст блока — на казахском языке." in message
    assert "Язык обучения: казахский" in message.splitlines()
    assert "русском языке" not in message
    assert "Просьба учителя: ойын қосыңыз" in message
    # Модели показан текущий казахский документ — новый блок согласуется с ним
    assert KZ_DRAFT["stage_beginning"][0]["students"] in message
    assert KZ_OBJECTIVE in message and CODE not in message

    lines = paragraphs(download(client, kz_kit))
    text = "\n".join(lines)
    for line in strings(KZ_STAGE_MIDDLE):
        assert line in lines, line
    assert KZ_DRAFT["stage_middle"][0]["teacher"] not in text
    assert KZ_DRAFT["stage_beginning"][0]["teacher"] in lines
    assert "һ" in text and "Һ" in text
    assert_language_of_form(lines, "kz")


# -- смешанный случай -----------------------------------------------------


def test_russian_and_kazakh_kits_keep_their_own_language(client, sms, ai):
    """Одна цель по коду в двух редакциях приказа — два комплекта и два
    документа, каждый целиком на своём языке. Подписи, раздел и формулировка
    цели не перетекают из одного в другой: общий код — не общий текст."""
    subject = make_subject()
    ru = ru_objective(subject.id)
    kz = kz_objective(subject.id)
    login_named(client, sms, **TEACHER)

    ru_kit = generate(client, ai, ru.id, DRAFT, TOPIC)
    ru_message = sent_message(ai)
    kz_kit = generate(client, ai, kz.id, KZ_DRAFT, KZ_TOPIC)
    kz_message = sent_message(ai)

    assert (ru_kit["lang"], kz_kit["lang"]) == ("ru", "kz")
    assert "Язык обучения: русский" in ru_message.splitlines()
    assert "Язык обучения: казахский" in kz_message.splitlines()

    ru_lines = paragraphs(download(client, ru_kit))
    kz_lines = paragraphs(download(client, kz_kit))
    ru_text, kz_text = "\n".join(ru_lines), "\n".join(kz_lines)

    assert ru_lines[0] == "Краткосрочный план урока"
    assert kz_lines[0] == "Қысқа мерзімді жоспар"
    assert_language_of_form(ru_lines, "ru")
    assert_language_of_form(kz_lines, "kz")

    assert f"{CODE} {OBJECTIVE_TEXT}" in ru_lines
    assert f"{CODE} {KZ_OBJECTIVE}" in kz_lines
    assert KZ_OBJECTIVE not in ru_text and OBJECTIVE_TEXT not in kz_text
    assert KZ_SECTION not in ru_text and RU_SECTION not in kz_text
    assert DRAFT["stage_middle"][0]["teacher"] in ru_text
    assert KZ_DRAFT["stage_middle"][0]["teacher"] in kz_text
    assert DRAFT["stage_middle"][0]["teacher"] not in kz_text
