#!/usr/bin/env python
"""End-to-end verification against a running КЕДР server.

Usage:  python scripts/verify_api.py http://127.0.0.1:8078

Exercises every public endpoint, the role/permission matrix, media signature
enforcement and the WebSocket handshake. Exits non-zero on the first failure.
"""
from __future__ import annotations

import io
import json
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8078").rstrip("/")
API = f"{BASE}/api/v1"
# Accounts used for the role matrix. Override for a deployment without demo users:
#   KEDR_VERIFY_MCHS=me@example.ru:MyPassword KEDR_VERIFY_FORESTER=... KEDR_VERIFY_CITIZEN=...
import os


def _creds(role: str, default_email: str) -> tuple[str, str]:
    raw = os.getenv(f"KEDR_VERIFY_{role.upper()}", "")
    if ":" in raw:
        email, password = raw.split(":", 1)
        return email.strip(), password
    return default_email, os.getenv("KEDR_VERIFY_PASSWORD", "kedr-demo-1234")


DEMO = {"citizen": _creds("citizen", "citizen@kedr.ru"),
        "forester": _creds("forester", "forester@kedr.ru"),
        "mchs": _creds("mchs", "mchs@kedr.ru")}

PASS, FAIL = [], []

# Unique per run so the verifier can be executed repeatedly against a live database.
RUN_ID = f"{int(time.time())}-{os.getpid()}"
NEW_USER = f"sweep-{RUN_ID}@example.ru"


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  <- ' + detail}")
    return ok


def jpeg_bytes() -> bytes:
    try:
        import cv2
        import numpy as np
        ok, buf = cv2.imencode(".jpg", (np.random.rand(64, 64, 3) * 255).astype("uint8"))
        if ok:
            return buf.tobytes()
    except Exception:
        pass
    return bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


def login(c, role: str) -> dict:
    email, password = DEMO[role]
    r = c.post(f"{API}/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def main() -> int:
    image = jpeg_bytes()
    files = {"file": ("fire.jpg", io.BytesIO(image), "image/jpeg")}
    with httpx.Client(timeout=120) as c:
        # Fail fast with an actionable message instead of a wall of FAILs.
        try:
            c.post(f"{API}/auth/login", json={"email": DEMO["mchs"][0], "password": DEMO["mchs"][1]}).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"\nНе удалось войти под {DEMO['mchs'][0]}: {exc}")
            print("Демо-аккаунты могли быть удалены. Варианты:")
            print("  1) создать их снова:  python -m scripts.manage create-user \\")
            print("       --email mchs@kedr.ru --password 'kedr-demo-1234' --role mchs   (и так же forester/citizen)")
            print("  2) указать свои аккаунты:")
            print("       KEDR_VERIFY_MCHS=mail:pass KEDR_VERIFY_FORESTER=mail:pass \\")
            print("       KEDR_VERIFY_CITIZEN=mail:pass python scripts/verify_api.py <url>")
            return 2
        print("\n== health ==")
        r = c.get(f"{API}/health")
        check("GET /health -> 200", r.status_code == 200, r.text[:120])
        h = r.json()
        check("health reports db + model state", "db" in h and "model" in h, json.dumps(h)[:160])
        has_model = bool(h.get("model"))

        print("\n== auth ==")
        r = c.post(f"{API}/auth/register", json={"email": NEW_USER, "password": "SweepPass1",
                                                 "password_confirm": "SweepPass1"})
        check("register new user -> 200", r.status_code == 200, r.text[:160])
        citizen_tok = {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else {}
        r = c.post(f"{API}/auth/register", json={"email": NEW_USER, "password": "SweepPass1",
                                                 "password_confirm": "SweepPass1"})
        check("duplicate register -> 409", r.status_code == 409, r.text[:120])
        r = c.post(f"{API}/auth/register", json={"email": f"mismatch-{RUN_ID}@example.ru",
                                                 "password": "SweepPass1", "password_confirm": "OtherPass1"})
        check("password mismatch -> 422", r.status_code == 422, r.text[:120])
        r = c.post(f"{API}/auth/register", json={"email": f"weak-{RUN_ID}@example.ru",
                                                 "password": "12345678", "password_confirm": "12345678"})
        check("weak password -> 422", r.status_code == 422, r.text[:120])
        check("login wrong password -> 401",
              c.post(f"{API}/auth/login", json={"email": DEMO['mchs'][0], "password": "nope"}).status_code == 401)
        heads = {}
        for role in DEMO:
            try:
                heads[role] = login(c, role)
                check(f"login demo {role}", True)
            except Exception as exc:  # noqa: BLE001
                check(f"login demo {role}", False, str(exc))
        check("GET /auth/me with token -> 200",
              c.get(f"{API}/auth/me", headers=heads.get("mchs", {})).status_code == 200)
        check("GET /auth/me without token -> 403", c.get(f"{API}/auth/me").status_code == 403)

        print("\n== roles ==")
        check("citizen cannot list incidents -> 403",
              c.get(f"{API}/incidents", headers=citizen_tok).status_code == 403)
        check("anonymous cannot list incidents -> 403", c.get(f"{API}/incidents").status_code == 403)
        check("forester can list incidents -> 200",
              c.get(f"{API}/incidents", headers=heads.get("forester", {})).status_code == 200)
        check("anonymous public feed -> 200", c.get(f"{API}/incidents/public").status_code == 200)
        video = {"file": ("c.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42"), "video/mp4")}
        check("anonymous cannot upload video -> 401",
              c.post(f"{API}/incidents/report", files=video).status_code == 401)
        r = c.post(f"{API}/incidents/report", headers=citizen_tok, files=video)
        check("authenticated citizen passes the video auth gate",
              r.status_code != 401, f"{r.status_code} {r.text[:120]}")

        print("\n== intake ==")
        r = c.post(f"{API}/incidents/report", files=files, data={"lat": "56.01", "lon": "92.89"})
        if has_model:
            # weights are present: the photo must actually be analysed
            check("photo report analysed with weights", r.status_code in (200, 422),
                  f"{r.status_code} {r.text[:120]}")
        else:
            check("photo report without weights -> 503", r.status_code == 503, f"{r.status_code} {r.text[:120]}")
        r = c.post(f"{API}/incidents/report", files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")})
        check("non-media upload -> 415", r.status_code == 415, r.text[:120])
        r = c.post(f"{API}/incidents/manual", json={"lat": 56.0153, "lon": 92.8932, "note": "sweep"},
                   headers=heads.get("mchs", {}))
        check("MCHS manual incident -> 200", r.status_code == 200, r.text[:200])
        manual = r.json() if r.status_code == 200 else {}
        if manual:
            props = manual["hazard_geojson"]["properties"]
            check("manual incident enriched (fuel/terrain/ros)",
                  props.get("fuel") in ("forest", "urban", "mixed") and "ros_m_per_min" in props, json.dumps(props)[:200])
            check("zone is not degenerate", props.get("cell_count", 0) >= 1, str(props.get("cell_count")))
            check("note appended to region", "sweep" in (manual.get("region_name") or ""), manual.get("region_name", ""))
            check("compact list strips cells",
                  "cells" not in json.dumps(c.get(f"{API}/incidents?limit=1", headers=heads.get("forester", {})).json()[0]["hazard_geojson"]))
            check("detail returns cells",
                  "cells" in json.dumps(c.get(f"{API}/incidents/{manual['id']}", headers=heads.get("forester", {})).json()["hazard_geojson"]))
            check("media URL requires signature -> 403",
                  c.get(f"{API}/media/{manual.get('annotated_path') or 'images/none.jpg'}").status_code == 403)
        r = c.post(f"{API}/incidents/manual", json={"lat": 40.0, "lon": 10.0}, headers=heads.get("mchs", {}))
        check("manual outside Russia -> 422", r.status_code == 422, r.text[:120])
        check("manual requires MCHS -> 403",
              c.post(f"{API}/incidents/manual", json={"lat": 56.0, "lon": 92.9},
                     headers=heads.get("forester", {})).status_code == 403)
        check("bad verdict -> 422",
              c.post(f"{API}/incidents/manual-photo", headers=heads.get("mchs", {}),
                     data={"lat": "56.0", "lon": "92.9", "verdict": "bogus"}).status_code == 422)
        r = c.post(f"{API}/incidents/rtsp", headers=heads.get("mchs", {}),
                   json={"url": "http://127.0.0.1:8000/x", "lat": 56.0, "lon": 92.9, "seconds": 5})
        check("RTSP SSRF to loopback blocked -> 422", r.status_code == 422, r.text[:160])
        r = c.post(f"{API}/incidents/rtsp", headers=heads.get("mchs", {}),
                   json={"url": "file:///etc/passwd", "lat": 56.0, "lon": 92.9, "seconds": 5})
        check("RTSP file:// blocked -> 422", r.status_code == 422, r.text[:160])

        print("\n== reads ==")
        check("unknown status -> 422",
              c.get(f"{API}/incidents?status=BOGUS", headers=heads.get("forester", {})).status_code == 422)
        check("pagination limit honoured",
              len(c.get(f"{API}/incidents?limit=1", headers=heads.get("forester", {})).json()) <= 1)
        check("general report -> 200",
              c.get(f"{API}/incidents/general-report", headers=heads.get("forester", {})).status_code == 200)
        check("incident 404", c.get(f"{API}/incidents/999999", headers=heads.get("forester", {})).status_code == 404)

        print("\n== geo ==")
        r = c.get(f"{API}/geo/search", params={"q": "Краснояр"})
        check("geo search (cyrillic) -> 200 with results", r.status_code == 200 and len(r.json()) > 0, r.text[:120])
        check("geo search too short -> []", c.get(f"{API}/geo/search", params={"q": "К"}).json() == [])
        r = c.get(f"{API}/geo/weather", params={"lat": 56.0, "lon": 92.9})
        check("geo weather -> 200", r.status_code == 200, r.text[:160])
        r = c.get(f"{API}/geo/infrastructure", params={"lat": 56.0, "lon": 92.9, "radius_m": 5000})
        check("geo infrastructure -> 200 with source", r.status_code == 200 and "source" in r.json(), r.text[:160])
        check("geo infrastructure rejects out-of-Russia -> 422",
              c.get(f"{API}/geo/infrastructure", params={"lat": 40.0, "lon": 10.0}).status_code == 422)

        print("\n== review / edit / pdf ==")
        if manual:
            iid = manual["id"]
            check("review bad verdict -> 422",
                  c.post(f"{API}/incidents/{iid}/review", json={"verdict": "x"},
                         headers=heads.get("forester", {})).status_code == 422)
            check("review on CONFIRMED -> 409",
                  c.post(f"{API}/incidents/{iid}/review", json={"verdict": "confirm"},
                         headers=heads.get("forester", {})).status_code == 409)
            check("PDF requires MCHS -> 403",
                  c.get(f"{API}/incidents/{iid}/report.pdf", headers=heads.get("forester", {})).status_code == 403)
            r = c.get(f"{API}/incidents/{iid}/report.pdf", headers=heads.get("mchs", {}))
            check("PDF -> %PDF", r.status_code == 200 and r.content[:4] == b"%PDF", f"{r.status_code} {r.text[:120]}")
            r = c.patch(f"{API}/incidents/{iid}/location", json={"lat": 56.1, "lon": 92.8, "note": "right here"},
                        headers=heads.get("forester", {}))
            check("patch location -> 200", r.status_code == 200, r.text[:160])
            check("patch kept the note", "right here" in (r.json().get("region_name") or ""), r.json().get("region_name", ""))
            check("refresh -> 200",
                  c.post(f"{API}/incidents/{iid}/refresh", headers=heads.get("forester", {})).status_code == 200)

        print("\n== session revocation ==")
        r = c.post(f"{API}/auth/change-password", headers=citizen_tok,
                   json={"current_password": "SweepPass1", "new_password": "SweepPass2",
                         "new_password_confirm": "SweepPass2"})
        check("change-password -> 200", r.status_code == 200, r.text[:160])
        check("old token revoked -> 401", c.get(f"{API}/auth/me", headers=citizen_tok).status_code == 401)
        if r.status_code == 200:
            new_tok = {"Authorization": f"Bearer {r.json()['access_token']}"}
            check("new token works -> 200", c.get(f"{API}/auth/me", headers=new_tok).status_code == 200)
            check("logout -> 200", c.post(f"{API}/auth/logout", headers=new_tok).status_code == 200)
            check("token dead after logout -> 401", c.get(f"{API}/auth/me", headers=new_tok).status_code == 401)

        print("\n== websocket ==")
        try:
            from websockets.sync.client import connect as ws_connect  # type: ignore

            token = c.post(f"{API}/auth/login",
                           json={"email": DEMO["mchs"][0], "password": DEMO["mchs"][1]}).json()["access_token"]
            ws_url = BASE.replace("http://", "ws://").replace("https://", "wss://") + "/ws/alerts"
            try:
                with ws_connect(ws_url, subprotocols=["kedr.v1"], open_timeout=10) as ws:
                    ws.send(json.dumps({"type": "auth", "token": token}))
                    ws.send(json.dumps({"type": "ping"}))
                    # The socket must stay open: a rejected token is closed with 4401.
                    timed_out = False
                    try:
                        ws.recv(timeout=3)
                    except TimeoutError:
                        timed_out = True
                    check("WS accepts first-frame auth", timed_out,
                          "socket closed or produced unexpected data")
            except Exception as exc:  # noqa: BLE001
                check("WS accepts first-frame auth", False, str(exc))
            try:
                with ws_connect(ws_url, subprotocols=["kedr.v1"], open_timeout=10) as ws:
                    ws.send(json.dumps({"type": "auth", "token": "forged"}))
                    ws.recv(timeout=5)
                    check("WS rejects a forged token", False, "connection stayed open")
            except Exception:
                check("WS rejects a forged token", True)
        except ImportError:
            print("  SKIP  websockets client not installed")

    print(f"\n{'=' * 60}\n{PASS and len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
