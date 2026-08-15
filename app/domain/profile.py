# Онбординг пройден, когда заполнены фамилия и имя: экран онбординга
# требует только их, остальное можно дозаполнить в профиле.


def onboarding_done(first_name: str, last_name: str) -> bool:
    return bool(first_name.strip() and last_name.strip())
