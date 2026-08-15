import re

# Нормализация к +7XXXXXXXXXX при сохранении (BACKEND_NOTES, раздел 8):
# «8701…», «+7 701 …» и «87012345678» — один человек. Уникальность в базе
# висит на нормализованном значении.


def normalize_phone(raw: str) -> str | None:
    """Возвращает +7XXXXXXXXXX или None, если это не казахстанский номер."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) != 11 or digits[0] != "7":
        return None
    return "+" + digits
