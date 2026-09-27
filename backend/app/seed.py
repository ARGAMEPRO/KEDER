"""Idempotent first-boot seeder: demo users and Siberian settlements. Dev use only."""
import json
import os
from pathlib import Path

from passlib.context import CryptContext

from .models import Role, Settlement, SessionLocal, User, init_db

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = [
    ("citizen@kedr.ru", "Гражданин", Role.CITIZEN),
    ("forester@kedr.ru", "Лесничий", Role.FORESTER),
    ("mchs@kedr.ru", "Диспетчер МЧС", Role.MCHS),
]

# (name, name_en, region, lat, lon, population) — approximate demo values
SETTLEMENTS = [
    ("Красноярск", "Krasnoyarsk", "Красноярский край", 56.0153, 92.8932, 1190000),
    ("Дивногорск", "Divnogorsk", "Красноярский край", 55.9556, 92.3700, 29000),
    ("Сосновоборск", "Sosnovoborsk", "Красноярский край", 56.1200, 93.3300, 41000),
    ("Железногорск", "Zheleznogorsk", "Красноярский край", 56.2500, 93.5333, 85000),
    ("Лесосибирск", "Lesosibirsk", "Красноярский край", 58.2333, 92.4833, 60000),
    ("Енисейск", "Yeniseysk", "Красноярский край", 58.4500, 92.1667, 17000),
    ("Иркутск", "Irkutsk", "Иркутская область", 52.2870, 104.3050, 620000),
    ("Листвянка", "Listvyanka", "Иркутская область", 51.8667, 104.8333, 2000),
    ("Братск", "Bratsk", "Иркутская область", 56.1325, 101.6142, 220000),
    ("Усть-Илимск", "Ust-Ilimsk", "Иркутская область", 58.0000, 102.6500, 80000),
    ("Новосибирск", "Novosibirsk", "Новосибирская область", 55.0084, 82.9357, 1630000),
    ("Томск", "Tomsk", "Томская область", 56.4846, 84.9476, 570000),
    ("Москва", "Moscow", "Москва", 55.7558, 37.6173, 12600000),
    ("Санкт-Петербург", "Saint Petersburg", "Санкт-Петербург", 59.9343, 30.3351, 5400000),
    ("Екатеринбург", "Yekaterinburg", "Свердловская область", 56.8389, 60.6057, 1500000),
    ("Казань", "Kazan", "Татарстан", 55.8304, 49.0661, 1300000),
    ("Нижний Новгород", "Nizhny Novgorod", "Нижегородская область", 56.3267, 44.0075, 1250000),
    ("Челябинск", "Chelyabinsk", "Челябинская область", 55.1599, 61.4025, 1200000),
    ("Омск", "Omsk", "Омская область", 54.9885, 73.3242, 1150000),
    ("Самара", "Samara", "Самарская область", 53.1955, 50.1018, 1100000),
    ("Ростов-на-Дону", "Rostov-on-Don", "Ростовская область", 47.2357, 39.7018, 1100000),
    ("Уфа", "Ufa", "Башкортостан", 54.7386, 55.9722, 1100000),
    ("Владивосток", "Vladivostok", "Приморский край", 43.1332, 131.9113, 600000),
    ("Хабаровск", "Khabarovsk", "Хабаровский край", 48.4825, 135.0838, 610000),
    ("Якутск", "Yakutsk", "Якутия", 62.0355, 129.6755, 330000),
    ("Магадан", "Magadan", "Магаданская область", 59.5639, 150.8036, 95000),
    ("Курск", "Kursk", "Курская область", 51.7304, 36.1926, 450000),
    ("Брянск", "Bryansk", "Брянская область", 53.2434, 34.3641, 400000),
    ("Воронеж", "Voronezh", "Воронежская область", 51.6606, 39.2003, 1050000),
    ("Волгоград", "Volgograd", "Волгоградская область", 48.7081, 44.5133, 1000000),
    ("Саратов", "Saratov", "Саратовская область", 51.5336, 46.0350, 840000),
    ("Тула", "Tula", "Тульская область", 54.1930, 37.6173, 500000),
    ("Рязань", "Ryazan", "Рязанская область", 54.6269, 39.6917, 540000),
    ("Ярославль", "Yaroslavl", "Ярославская область", 57.6299, 39.8737, 600000),
    ("Архангельск", "Arkhangelsk", "Архангельская область", 64.5667, 40.5347, 350000),
    ("Мурманск", "Murmansk", "Мурманская область", 68.9585, 33.0827, 300000),
    ("Петрозаводск", "Petrozavodsk", "Карелия", 61.7850, 34.3468, 270000),
    ("Сыктывкар", "Syktyvkar", "Коми", 61.6686, 50.8368, 250000),
    ("Калининград", "Kaliningrad", "Калининградская область", 54.7104, 20.4522, 480000),
    ("Севастополь", "Sevastopol", "Севастополь", 44.6054, 33.8224, 550000),
    ("Симферополь", "Simferopol", "Крым", 44.9521, 34.1024, 340000),
]


def seed() -> None:
    init_db()
    password = os.getenv("SEED_PASSWORD", "123456")
    with SessionLocal() as db:
        if not db.query(User).first():
            hashed = pwd.hash(password)
            db.add_all(User(email=e, full_name=n, role=r, hashed_password=hashed) for e, n, r in USERS)
        if not db.query(Settlement).first():
            # Try to load from ru_cities.json for comprehensive coverage
            try:
                data_path = Path(__file__).parent / "data" / "ru_cities.json"
                if data_path.exists():
                    data = json.loads(data_path.read_text(encoding="utf-8"))
                    # Only cities with population >= 5000 to avoid overwhelming the database
                    cities = [c for c in data["cities"] if (c.get("p") or 0) >= 5000]
                    db.add_all(
                        Settlement(
                            name=c["n"], name_en=c.get("a") or "",
                            region=c.get("r") or "",
                            lat=c["lat"], lon=c["lon"],
                            population=c.get("p") or 0,
                        )
                        for c in cities
                    )
                    print(f"[kedr] seed: loaded {len(cities)} settlements from ru_cities.json")
                else:
                    # Fallback to hardcoded list
                    db.add_all(
                        Settlement(name=n, name_en=en, region=rg, lat=la, lon=lo, population=p)
                        for n, en, rg, la, lo, p in SETTLEMENTS
                    )
                    print(f"[kedr] seed: loaded {len(SETTLEMENTS)} settlements from hardcoded list")
            except Exception as e:
                print(f"[kedr] seed: failed to load ru_cities.json, using fallback: {e}")
                db.add_all(
                    Settlement(name=n, name_en=en, region=rg, lat=la, lon=lo, population=p)
                    for n, en, rg, la, lo, p in SETTLEMENTS
                )
        db.commit()
