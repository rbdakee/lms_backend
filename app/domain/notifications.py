"""Тексты уведомлений: тип + params + язык читателя → готовая строка.

В базе лежат только `type` и `params`, готового текста там нет: учитель
переключит язык, и старые уведомления обязаны стать казахскими
(BACKEND_NOTES, раздел 11). Поэтому текст собирается здесь, в момент чтения.

Это домен — импортов фреймворка тут нет.
"""

LANGS = ("ru", "kz")
DEFAULT_LANG = "ru"

# Основные шаблоны: тип → язык → строка с подстановкой из params.
TEXTS: dict[str, dict[str, str]] = {
    "access_granted": {
        "ru": "Открыт доступ к курсу «{course_title}»",
        "kz": "«{course_title}» курсына қолжетімділік ашылды",
    },
    "submission_reviewed": {
        "ru": "Ваше задание «{task_title}» {verdict_text}",
        "kz": "«{task_title}» тапсырмаңыз {verdict_text}",
    },
    # Кто именно ответил, params не несут, а отвечает не только админ:
    # «часто коллега отвечает быстрее» (DESIGN_BRIEF) — отсюда безличное «ответили»
    "answer_posted": {
        "ru": "На ваш вопрос к уроку «{lesson_title}» ответили",
        "kz": "«{lesson_title}» сабағына қойған сұрағыңызға жауап берілді",
    },
    "certificate_issued": {
        "ru": "Сертификат по курсу «{course_title}» готов",
        "kz": "«{course_title}» курсы бойынша сертификат дайын",
    },
    # Без этого письма человек не узнает, что тест снова открыт, и будет
    # считать курс потерянным (CONTRACT, сессия 7б)
    "retake_allowed": {
        "ru": "Открыта пересдача теста «{quiz_title}»",
        "kz": "«{quiz_title}» тестін қайта тапсыруға рұқсат берілді",
    },
}

# Ключ params, ради которого в шаблоне стоят кавычки.
TITLE_PARAM: dict[str, str] = {
    "access_granted": "course_title",
    "submission_reviewed": "task_title",
    "answer_posted": "lesson_title",
    "certificate_issued": "course_title",
    "retake_allowed": "quiz_title",
}

# Запасные шаблоны — без названия. Уведомления, записанные до сессии 6,
# названий в params не несут, и такая строка не должна ни ронять ответ,
# ни показывать пустые кавычки (CONTRACT, сессия 6).
TEXTS_WITHOUT_TITLE: dict[str, dict[str, str]] = {
    "access_granted": {
        "ru": "Открыт доступ к курсу",
        "kz": "Курсқа қолжетімділік ашылды",
    },
    "submission_reviewed": {
        "ru": "Ваше задание {verdict_text}",
        "kz": "Тапсырмаңыз {verdict_text}",
    },
    "answer_posted": {
        "ru": "На ваш вопрос к уроку ответили",
        "kz": "Сабаққа қойған сұрағыңызға жауап берілді",
    },
    "certificate_issued": {
        "ru": "Сертификат по курсу готов",
        "kz": "Курс бойынша сертификат дайын",
    },
    "retake_allowed": {
        "ru": "Открыта пересдача теста",
        "kz": "Тестті қайта тапсыруға рұқсат берілді",
    },
}

# Оценка задания бинарная, и от неё зависит хвост фразы, а не весь шаблон.
VERDICT_TEXTS: dict[str, dict[str, str]] = {
    "accepted": {"ru": "зачтено", "kz": "есептелді"},
    "rework": {"ru": "отправлено на доработку", "kz": "пысықтауға жіберілді"},
}


class _Values(dict):
    """Отсутствующий ключ — пустая строка: уведомление старого образца
    не должно превращаться в 500 на колокольчике."""

    def __missing__(self, key: str) -> str:
        return ""


def notification_text(type_: str, params: dict, lang: str) -> str:
    if lang not in LANGS:
        lang = DEFAULT_LANG
    templates = TEXTS.get(type_)
    if templates is None:
        # Неизвестный тип пишет только наш же код — но пустой текст лучше отказа
        return ""
    title_param = TITLE_PARAM.get(type_)
    if title_param and not params.get(title_param):
        templates = TEXTS_WITHOUT_TITLE.get(type_, templates)
    values = _Values(params)
    verdict = VERDICT_TEXTS.get(str(params.get("verdict")))
    if verdict is not None:
        values["verdict_text"] = verdict[lang]
    # strip: без вердикта или названия у запасной фразы остаётся хвостовой пробел
    return templates[lang].format_map(values).strip()
