"""Точка входа: `kinomatch <команда>` или `python -m kinomatch <команда>`."""

import argparse
import asyncio
import logging
from pathlib import Path

from kinomatch.config import get_settings


def _import_wikidata(args: argparse.Namespace) -> None:
    from kinomatch.catalog import wikidata

    items = wikidata.parse(wikidata.fetch_bindings(args.min_sitelinks))
    wikidata.save(items, args.out)
    print(f"Сохранено фильмов: {len(items)} -> {args.out}")


def _seed(args: argparse.Namespace) -> None:
    from kinomatch.catalog.wikidata import load
    from kinomatch.infrastructure.db.session import create_engine, session_factory
    from kinomatch.infrastructure.db.uow import SqlUnitOfWork

    async def run() -> None:
        engine = create_engine(get_settings().database_url)
        factory = session_factory(engine)
        movies = [item.to_movie() for item in load(args.path)]
        async with SqlUnitOfWork(factory) as uow:
            count = await uow.movies.upsert_many(movies)
            await uow.commit()
        await engine.dispose()
        print(f"Загружено фильмов: {count}")

    asyncio.run(run())


def _api(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "kinomatch.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=get_settings().log_level.lower(),
    )


def _worker(_: argparse.Namespace) -> None:
    from kinomatch.worker import main

    asyncio.run(main())


def _bot(_: argparse.Namespace) -> None:
    from kinomatch.bot.main import main

    asyncio.run(main())


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="kinomatch")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-wikidata", help="выгрузить каталог фильмов из Wikidata в JSON")
    p.add_argument("--min-sitelinks", type=int, default=20)
    p.add_argument("--out", type=Path, default=get_settings().catalog_path)
    p.set_defaults(func=_import_wikidata)

    p = sub.add_parser("seed", help="загрузить каталог из JSON в PostgreSQL")
    p.add_argument("--path", type=Path, default=get_settings().catalog_path)
    p.set_defaults(func=_seed)

    p = sub.add_parser("api", help="запустить HTTP API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_api)

    sub.add_parser("worker", help="доставка событий из outbox в Redis").set_defaults(func=_worker)
    sub.add_parser("bot", help="запустить Telegram-бота").set_defaults(func=_bot)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
