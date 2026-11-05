#!/usr/bin/env python
"""КЕДР management CLI.

Run from the `backend/` directory (inside the container: `docker compose exec backend ...`):

    python -m scripts.manage list-users
    python -m scripts.manage create-user --email boss@example.ru --password 'StrongPass1' --role mchs
    python -m scripts.manage create-user --email forest@example.ru --password 'StrongPass1' --role forester
    python -m scripts.manage delete-demo-users
    python -m scripts.manage set-role --email user@example.ru --role forester
    python -m scripts.manage reset-password --email user@example.ru --password 'NewPass123'
    python -m scripts.manage purge-demo-incidents
    python -m scripts.manage reset-db --yes

Removing the demo accounts is REQUIRED before going live.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_conf import setup_logging  # noqa: E402
from app.models import Base, Incident, Role, SessionLocal, Settlement, User, engine, init_db  # noqa: E402
from app.security import PasswordPolicyError, hash_password, validate_password  # noqa: E402

DEMO_EMAILS = ("citizen@kedr.ru", "forester@kedr.ru", "mchs@kedr.ru")


def _role(value: str) -> Role:
    try:
        return Role(value.strip().lower())
    except ValueError as exc:
        raise SystemExit(f"Некорректная роль: {value!r}. Допустимо: citizen, forester, mchs") from exc


def cmd_list_users(_args) -> int:
    with SessionLocal() as db:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("Пользователей нет.")
            return 0
        print(f"{'id':>4}  {'email':32} {'role':9} {'demo':5} {'active':6} name")
        for u in users:
            print(f"{u.id:>4}  {u.email:32} {u.role.value:9} "
                  f"{'yes' if u.is_demo else 'no':5} {'yes' if u.is_active else 'no':6} {u.full_name}")
    return 0


def cmd_create_user(args) -> int:
    try:
        validate_password(args.password)
    except PasswordPolicyError as exc:
        raise SystemExit(f"Пароль не соответствует политике: {exc}") from exc
    with SessionLocal() as db:
        email = args.email.strip().lower()
        if db.query(User).filter(User.email == email).first():
            raise SystemExit(f"Пользователь {email} уже существует")
        u = User(email=email, full_name=args.full_name or "", role=_role(args.role), is_demo=False,
                 hashed_password=hash_password(args.password))
        db.add(u)
        db.commit()
        print(f"Создан пользователь #{u.id} {u.email} с ролью {u.role.value}")
    return 0


def cmd_delete_demo_users(_args) -> int:
    with SessionLocal() as db:
        users = db.query(User).filter((User.is_demo.is_(True)) | (User.email.in_(DEMO_EMAILS))).all()
        if not users:
            print("Демо-аккаунтов нет — ничего удалять не нужно.")
            return 0
        for u in users:
            print(f"Удаляю {u.email} (роль {u.role.value})")
            db.delete(u)
        db.commit()
        print(f"Удалено демо-аккаунтов: {len(users)}")
    return 0


def cmd_purge_demo_incidents(_args) -> int:
    with SessionLocal() as db:
        demo_ids = [u.id for u in db.query(User).filter((User.is_demo.is_(True)) | (User.email.in_(DEMO_EMAILS)))]
        if not demo_ids:
            print("Демо-пользователей нет — связанных инцидентов тоже.")
            return 0
        rows = db.query(Incident).filter(Incident.reporter_id.in_(demo_ids)).all()
        for r in rows:
            db.delete(r)
        db.commit()
        print(f"Удалено инцидентов, созданных демо-аккаунтами: {len(rows)}")
    return 0


def cmd_set_role(args) -> int:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == args.email.strip().lower()).first()
        if not u:
            raise SystemExit(f"Пользователь {args.email} не найден")
        u.role = _role(args.role)
        u.token_version += 1
        db.commit()
        print(f"{u.email}: роль изменена на {u.role.value}; выданные токены отозваны")
    return 0


def cmd_reset_password(args) -> int:
    try:
        validate_password(args.password)
    except PasswordPolicyError as exc:
        raise SystemExit(f"Пароль не соответствует политике: {exc}") from exc
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == args.email.strip().lower()).first()
        if not u:
            raise SystemExit(f"Пользователь {args.email} не найден")
        u.hashed_password = hash_password(args.password)
        u.token_version += 1
        db.commit()
        print(f"Пароль для {u.email} обновлён; выданные токены отозваны")
    return 0


def cmd_deactivate(args) -> int:
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == args.email.strip().lower()).first()
        if not u:
            raise SystemExit(f"Пользователь {args.email} не найден")
        u.is_active = False
        u.token_version += 1
        db.commit()
        print(f"{u.email} отключён")
    return 0


def cmd_stats(_args) -> int:
    with SessionLocal() as db:
        print(f"Пользователи : {db.query(User).count()} (демо: {db.query(User).filter(User.is_demo.is_(True)).count()})")
        print(f"Инциденты    : {db.query(Incident).count()}")
        print(f"Населённые п.: {db.query(Settlement).count()}")
    return 0


def cmd_reset_db(args) -> int:
    if not args.yes:
        raise SystemExit("Это удалит ВСЕ данные. Повторите с флагом --yes")
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS alembic_version"))
    init_db()
    print("База данных пересоздана (схема пустая).")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(prog="manage", description="КЕДР: управление пользователями и данными")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-users", help="показать всех пользователей").set_defaults(func=cmd_list_users)
    sub.add_parser("stats", help="краткая статистика по базе").set_defaults(func=cmd_stats)

    c = sub.add_parser("create-user", help="создать пользователя")
    c.add_argument("--email", required=True)
    c.add_argument("--password", required=True)
    c.add_argument("--role", default="citizen", choices=["citizen", "forester", "mchs"])
    c.add_argument("--full-name", default="")
    c.set_defaults(func=cmd_create_user)

    sub.add_parser("delete-demo-users", help="удалить демо-аккаунты (обязательно перед продакшеном)"
                   ).set_defaults(func=cmd_delete_demo_users)
    sub.add_parser("purge-demo-incidents", help="удалить инциденты, созданные демо-аккаунтами"
                   ).set_defaults(func=cmd_purge_demo_incidents)

    r = sub.add_parser("set-role", help="сменить роль пользователя")
    r.add_argument("--email", required=True)
    r.add_argument("--role", required=True, choices=["citizen", "forester", "mchs"])
    r.set_defaults(func=cmd_set_role)

    pw = sub.add_parser("reset-password", help="сменить пароль пользователя")
    pw.add_argument("--email", required=True)
    pw.add_argument("--password", required=True)
    pw.set_defaults(func=cmd_reset_password)

    d = sub.add_parser("deactivate", help="отключить пользователя")
    d.add_argument("--email", required=True)
    d.set_defaults(func=cmd_deactivate)

    rd = sub.add_parser("reset-db", help="пересоздать схему (УДАЛЯЕТ ВСЕ ДАННЫЕ)")
    rd.add_argument("--yes", action="store_true")
    rd.set_defaults(func=cmd_reset_db)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
