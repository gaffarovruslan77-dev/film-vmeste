from collections.abc import Callable

import httpx

from kinomatch.infrastructure.memory import InMemoryStore

Headers = Callable[..., dict[str, str]]


async def create_room(client: httpx.AsyncClient, headers: dict[str, str], **body: object) -> str:
    response = await client.post("/api/v1/rooms", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["code"])


async def test_full_flow_two_users_find_match(
    client: httpx.AsyncClient, tg: Headers, store: InMemoryStore
) -> None:
    alice, bob = tg(1, "Алиса"), tg(2, "Боб")
    code = await create_room(client, alice)

    assert (await client.post(f"/api/v1/rooms/{code}/join", headers=bob)).status_code == 200
    room = (await client.post(f"/api/v1/rooms/{code}/start", headers=alice)).json()
    assert room["status"] == "voting"
    assert {p["display_name"] for p in room["participants"]} == {"Алиса", "Боб"}

    # Алиса лайкает первую карточку — Бобу она приходит первой как «горячая».
    card = (await client.get(f"/api/v1/rooms/{code}/next", headers=alice)).json()["card"]
    movie_id = card["movie"]["id"]
    r = await client.post(
        f"/api/v1/rooms/{code}/swipes", json={"movie_id": movie_id, "liked": True}, headers=alice
    )
    assert r.json() == {"match": None, "room_status": "voting"}

    bob_card = (await client.get(f"/api/v1/rooms/{code}/next", headers=bob)).json()["card"]
    assert bob_card["movie"]["id"] == movie_id
    assert bob_card["reason"] == "hot"

    r = await client.post(
        f"/api/v1/rooms/{code}/swipes", json={"movie_id": movie_id, "liked": True}, headers=bob
    )
    body = r.json()
    assert body["match"]["id"] == movie_id
    assert body["room_status"] == "finished"

    results = (await client.get(f"/api/v1/rooms/{code}/results")).json()
    assert [m["id"] for m in results["matches"]] == [movie_id]

    types = [e.type for e in store.events]
    assert types == [
        "participant_joined",
        "voting_started",
        "match_found",
        "room_finished",
    ]


async def test_filters_limit_candidates(client: httpx.AsyncClient, tg: Headers) -> None:
    alice, bob = tg(1), tg(2)
    code = await create_room(client, alice, filters={"genres": ["ужасы"]})
    await client.post(f"/api/v1/rooms/{code}/join", headers=bob)
    room = (await client.post(f"/api/v1/rooms/{code}/start", headers=alice)).json()
    assert room["candidates"] == 1


async def test_no_candidates_for_impossible_filters(client: httpx.AsyncClient, tg: Headers) -> None:
    r = await client.post("/api/v1/rooms", json={"filters": {"genres": ["вестерн"]}}, headers=tg(1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_candidates"


async def test_start_needs_two_participants(client: httpx.AsyncClient, tg: Headers) -> None:
    code = await create_room(client, tg(1))
    r = await client.post(f"/api/v1/rooms/{code}/start", headers=tg(1))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_enough_participants"


async def test_only_owner_can_start(client: httpx.AsyncClient, tg: Headers) -> None:
    code = await create_room(client, tg(1))
    await client.post(f"/api/v1/rooms/{code}/join", headers=tg(2))
    r = await client.post(f"/api/v1/rooms/{code}/start", headers=tg(2))
    assert r.status_code == 403


async def test_wrong_service_key_rejected(client: httpx.AsyncClient, tg: Headers) -> None:
    headers = tg(1) | {"X-Service-Key": "nope"}
    r = await client.post("/api/v1/rooms", json={}, headers=headers)
    assert r.status_code == 401


async def test_guest_token_flow(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/guest", json={"display_name": "Веб-гость"})
    token = r.json()["token"]
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["display_name"] == "Веб-гость"
    bad = await client.get("/api/v1/me", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401


async def test_room_codes_are_case_insensitive(client: httpx.AsyncClient, tg: Headers) -> None:
    code = await create_room(client, tg(1))
    r = await client.post(f"/api/v1/rooms/{code.lower()}/join", headers=tg(2))
    assert r.status_code == 200


async def test_unknown_room(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/rooms/NOPE00")
    assert r.status_code == 404


async def test_internal_endpoint_exposes_external_ids(
    client: httpx.AsyncClient, tg: Headers
) -> None:
    code = await create_room(client, tg(42))
    assert (await client.get(f"/api/v1/internal/rooms/{code}")).status_code == 401
    r = await client.get(
        f"/api/v1/internal/rooms/{code}", headers={"X-Service-Key": tg(1)["X-Service-Key"]}
    )
    [participant] = r.json()["participants"]
    assert participant["provider"] == "telegram"
    assert participant["external_id"] == "42"


async def test_genres(client: httpx.AsyncClient) -> None:
    genres = (await client.get("/api/v1/genres")).json()
    assert genres[:2] == [{"name": "драма", "movies": 2}, {"name": "комедия", "movies": 2}]


async def test_deck_exhausts(client: httpx.AsyncClient, tg: Headers) -> None:
    code = await create_room(client, tg(1), filters={"genres": ["ужасы"]})
    await client.post(f"/api/v1/rooms/{code}/join", headers=tg(2))
    await client.post(f"/api/v1/rooms/{code}/start", headers=tg(1))
    card = (await client.get(f"/api/v1/rooms/{code}/next", headers=tg(1))).json()["card"]
    await client.post(
        f"/api/v1/rooms/{code}/swipes",
        json={"movie_id": card["movie"]["id"], "liked": False},
        headers=tg(1),
    )
    assert (await client.get(f"/api/v1/rooms/{code}/next", headers=tg(1))).json() == {"card": None}
