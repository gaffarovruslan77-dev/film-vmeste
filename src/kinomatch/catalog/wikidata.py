"""Импорт каталога из Wikidata (через быстрое бесплатное зеркало QLever, ключ не нужен).

Берём фильмы, у которых есть ID Кинопоиска и русское название. Популярность —
число языковых Википедий со статьёй о фильме (sitelinks): у «Матрицы» их больше сотни,
у малоизвестного фильма — единицы.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from kinomatch.catalog.genres import canonical_genres
from kinomatch.domain.models import Movie

log = logging.getLogger(__name__)

QLEVER_URL = "https://qlever.dev/api/wikidata"
USER_AGENT = "kinomatch-pet-project/0.1 (educational)"
FILM = "http://www.wikidata.org/entity/Q11424"
ANIMATED_FILM = "http://www.wikidata.org/entity/Q202866"

QUERY = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX schema: <http://schema.org/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?film ?kp ?title ?desc ?links (MIN(?y) AS ?year) (MAX(?d) AS ?dur)
       (GROUP_CONCAT(DISTINCT ?genreLabel; separator="|") AS ?genres)
       (GROUP_CONCAT(DISTINCT STR(?type); separator="|") AS ?types) WHERE {
  ?film wdt:P2603 ?kp ;
        wikibase:sitelinks ?links .
  FILTER(xsd:integer(?links) >= %(min_sitelinks)d)
  ?film wdt:P31 ?type .
  VALUES ?type { wd:Q11424 wd:Q202866 }
  ?film rdfs:label ?title . FILTER(LANG(?title) = "ru")
  OPTIONAL { ?film schema:description ?desc . FILTER(LANG(?desc) = "ru") }
  OPTIONAL { ?film wdt:P577 ?date . BIND(YEAR(?date) AS ?y) }
  OPTIONAL { ?film wdt:P2047 ?d }
  OPTIONAL {
    ?film wdt:P136 ?genre .
    ?genre rdfs:label ?genreLabel . FILTER(LANG(?genreLabel) = "ru")
  }
}
GROUP BY ?film ?kp ?title ?desc ?links
"""


@dataclass(frozen=True, slots=True)
class CatalogItem:
    wikidata_id: str
    kinopoisk_id: int
    title: str
    year: int | None
    duration_min: int | None
    genres: list[str]
    popularity: int
    description: str | None

    def to_movie(self) -> Movie:
        return Movie(
            id=0,
            title=self.title,
            year=self.year,
            genres=frozenset(self.genres),
            popularity=self.popularity,
            duration_min=self.duration_min,
            kinopoisk_id=self.kinopoisk_id,
            wikidata_id=self.wikidata_id,
            description=self.description,
        )


def fetch_bindings(min_sitelinks: int, *, retries: int = 6) -> list[dict[str, Any]]:
    query = QUERY % {"min_sitelinks": min_sitelinks}
    headers = {"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT}
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        for attempt in range(retries):
            response = client.post(QLEVER_URL, data={"query": query}, headers=headers)
            if response.status_code in (429, 502, 503, 504):
                wait = int(response.headers.get("Retry-After") or 10 * (attempt + 1))
                log.warning("Wikidata вернул %s, повтор через %s с", response.status_code, wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            bindings: list[dict[str, Any]] = response.json()["results"]["bindings"]
            return bindings
    raise RuntimeError("Wikidata недоступна, попробуйте позже")


def _value(row: dict[str, Any], key: str) -> str | None:
    cell = row.get(key)
    return cell["value"] if cell else None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse(bindings: list[dict[str, Any]]) -> list[CatalogItem]:
    by_film: dict[str, CatalogItem] = {}
    for row in bindings:
        kinopoisk_id = _to_int(_value(row, "kp"))
        title = _value(row, "title")
        film = _value(row, "film")
        if kinopoisk_id is None or not title or not film:
            continue
        duration = _to_int(_value(row, "dur"))
        if duration is not None and not 20 <= duration <= 400:
            duration = None  # мусор или другие единицы измерения
        types = set((_value(row, "types") or "").split("|"))
        item = CatalogItem(
            wikidata_id=film.rsplit("/", 1)[-1],
            kinopoisk_id=kinopoisk_id,
            title=title,
            year=_to_int(_value(row, "year")),
            duration_min=duration,
            genres=canonical_genres(
                (_value(row, "genres") or "").split("|"),
                animated=ANIMATED_FILM in types,
            ),
            popularity=_to_int(_value(row, "links")) or 0,
            description=_value(row, "desc"),
        )
        # У фильма в Wikidata бывает несколько ID Кинопоиска — оставляем меньший (основной).
        current = by_film.get(item.wikidata_id)
        if current is None or item.kinopoisk_id < current.kinopoisk_id:
            by_film[item.wikidata_id] = item

    # И наоборот: один ID Кинопоиска у разных объектов-дублей — оставляем популярный.
    by_kinopoisk: dict[int, CatalogItem] = {}
    for item in by_film.values():
        current = by_kinopoisk.get(item.kinopoisk_id)
        if current is None or item.popularity > current.popularity:
            by_kinopoisk[item.kinopoisk_id] = item
    return sorted(by_kinopoisk.values(), key=lambda i: (-i.popularity, i.title))


def save(items: list[CatalogItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(i) for i in items]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def load(path: Path) -> list[CatalogItem]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [CatalogItem(**row) for row in raw]
