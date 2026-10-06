"""Цифры лендинга: курсы в каталоге, учителя и выданные сертификаты.

Раньше они стояли в разметке числами, придуманными для витрины. Теперь их
считает сервер в момент запроса — так же, как дашборд админа, без кэша
и без фоновых пересчётов (BACKEND_NOTES, раздел 13): три COUNT по индексам
дешевле, чем любая схема, которая бы их запоминала.

Каждая площадка видит только свои числа: каталог, доступы и сертификаты
у площадок раздельные (PLATFORMS_BRIEF, решение 2).
"""

from app.adapters.db.repos import CertificateRepo, CourseRepo


class PublicStatsService:
    def __init__(self, courses: CourseRepo, certificates: CertificateRepo):
        self.courses = courses
        self.certificates = certificates

    def public(self, platform: str) -> dict:
        return {
            "courses": self.courses.catalog_groups_count(platform),
            "teachers": self.courses.learners_count(platform),
            # Тот же счётчик, что справочное число дашборда: выданные,
            # не отозванные, без документов админов и без заявок
            "certificates": self.certificates.active_count(platform=platform),
        }
