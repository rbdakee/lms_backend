"""Порты — что сценариям нужно от внешнего мира. Порт описывает потребность
домена, а не API поставщика: смена провайдера SMS — это новый адаптер
и строчка в конфигурации, а не правки здесь.

Дальше сюда добавятся telegram, storage и video — по мере сессий плана.
"""

from typing import Protocol


class SmsPort(Protocol):
    def send_code(self, phone: str, code: str) -> None: ...
