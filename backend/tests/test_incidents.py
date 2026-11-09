"""Incident lifecycle: intake, escalation, review, permissions, payload shape."""
from __future__ import annotations

import io

from .conftest import fake_detection, incident_count


def _photo():
    return {"file": ("fire.jpg", io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg-bytes"), "image/jpeg")}


# ------------------------------------------------------------------ intake
def test_report_photo_creates_critical_incident(client, stub_analyze, mchs_headers):
    stub_analyze(fake_detection(0.95, "fire"))
    r = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.01", "lon": "92.89"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CRITICAL_ALERT"
    assert body["confidence"] == 0.95
    assert body["gps_source"] == "DEVICE"
    assert body["image_path"].startswith("images/")
    assert body["image_url"].startswith("/api/v1/media/")
    assert "token=" in body["image_url"]
    # enrichment ran
    assert body["hazard_geojson"]["properties"]["fuel"] in ("forest", "urban", "mixed")
    assert body["hazard_geojson"]["properties"]["cells"] is not None
    assert body["evacuation"], "evacuation plan should list the nearby settlement"
    assert body["weather_fallback"] == 0


def test_smoke_needs_more_confidence_than_fire(client, stub_analyze):
    stub_analyze(fake_detection(0.85, "smoke"))
    r = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.01", "lon": "92.89"})
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING_VERIFICATION"  # 0.85 smoke is not critical

    stub_analyze(fake_detection(0.95, "smoke"))
    r2 = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.02", "lon": "92.90"})
    assert r2.json()["status"] == "CRITICAL_ALERT"


def test_low_confidence_photo_is_clear_and_deduplicated(client, stub_analyze):
    stub_analyze(fake_detection(0.10, "fire"))
    first = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.01", "lon": "92.89"})
    assert first.status_code == 200
    assert first.json()["status"] == "CLEAR"

    second = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.011", "lon": "92.891"})
    assert second.status_code == 200
    assert second.json()["status"] == "CLEAR"
    assert second.json()["id"] == first.json()["id"], "near-identical CLEAR detections must fold into one row"
    assert incident_count() == 1


def test_report_rejects_non_media(client, stub_analyze):
    stub_analyze()
    r = client.post("/api/v1/incidents/report",
                    files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 415


def test_video_upload_requires_authentication(client, stub_analyze):
    stub_analyze()
    r = client.post("/api/v1/incidents/report",
                    files={"file": ("clip.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42"), "video/mp4")})
    assert r.status_code == 401


def test_report_503_when_model_missing(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Веса модели не найдены: weights/best.pt")

    monkeypatch.setattr("app.services.analyze", boom)
    r = client.post("/api/v1/incidents/report", files=_photo())
    assert r.status_code == 503
    assert "Веса модели не найдены" in r.json()["detail"]


def test_gps_outside_russia_is_dropped_not_rejected(client, stub_analyze):
    stub_analyze(fake_detection(0.5))
    r = client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "48.85", "lon": "2.35"})
    assert r.status_code == 200
    assert r.json()["lat"] is None and r.json()["gps_source"] == "NONE"


# ------------------------------------------------------------------ manual
def test_manual_requires_mchs(client, forester_headers, mchs_headers):
    payload = {"lat": 56.0, "lon": 92.9}
    assert client.post("/api/v1/incidents/manual", json=payload).status_code == 403
    assert client.post("/api/v1/incidents/manual", json=payload, headers=forester_headers).status_code == 403
    r = client.post("/api/v1/incidents/manual", json=payload, headers=mchs_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "CONFIRMED"


def test_manual_rejects_coordinates_outside_russia(client, mchs_headers):
    r = client.post("/api/v1/incidents/manual", json={"lat": 40.0, "lon": 10.0}, headers=mchs_headers)
    assert r.status_code == 422


def test_forester_needs_photo_for_manual_photo(client, stub_analyze, forester_headers):
    stub_analyze()
    r = client.post("/api/v1/incidents/manual-photo", data={"lat": "56.0", "lon": "92.9"},
                    headers=forester_headers)
    assert r.status_code == 422


def test_forester_can_override_the_model(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.05, "fire"))  # model sees nothing
    r = client.post("/api/v1/incidents/manual-photo", data={"lat": "56.0", "lon": "92.9", "verdict": "auto"},
                    files=_photo(), headers=forester_headers)
    assert r.status_code == 422  # auto mode refuses silently-low detections

    r2 = client.post("/api/v1/incidents/manual-photo",
                     data={"lat": "56.0", "lon": "92.9", "verdict": "confirm"},
                     files=_photo(), headers=forester_headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "CONFIRMED"


def test_mchs_can_create_without_photo(client, stub_analyze, mchs_headers):
    stub_analyze()
    r = client.post("/api/v1/incidents/manual-photo",
                    data={"lat": "56.0", "lon": "92.9", "verdict": "critical"}, headers=mchs_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "CRITICAL_ALERT"


def test_manual_photo_rejects_bad_verdict(client, stub_analyze, mchs_headers):
    stub_analyze()
    r = client.post("/api/v1/incidents/manual-photo",
                    data={"lat": "56.0", "lon": "92.9", "verdict": "nonsense"}, headers=mchs_headers)
    assert r.status_code == 422


# ------------------------------------------------------------------ reading
def test_list_incidents_is_compact_and_paginated(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.9))
    for i in range(3):
        client.post("/api/v1/incidents/report", files=_photo(),
                    data={"lat": f"56.0{i}", "lon": "92.9"})
    r = client.get("/api/v1/incidents?limit=2", headers=forester_headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    props = rows[0]["hazard_geojson"]["properties"]
    assert "cells" not in props, "list payload must not carry per-cell geometry"
    assert "cell_count" in props
    assert "geometry" in rows[0]["hazard_geojson"]


def test_list_incidents_rejects_unknown_status(client, forester_headers):
    r = client.get("/api/v1/incidents?status=BOGUS", headers=forester_headers)
    assert r.status_code == 422


def test_list_incidents_requires_staff(client, citizen_headers):
    assert client.get("/api/v1/incidents", headers=citizen_headers).status_code == 403


def test_incident_detail_contains_cells(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.9))
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]
    r = client.get(f"/api/v1/incidents/{iid}", headers=forester_headers)
    assert r.status_code == 200
    assert r.json()["hazard_geojson"]["properties"]["cells"] is not None
    assert client.get("/api/v1/incidents/999999", headers=forester_headers).status_code == 404


def test_public_incidents_needs_no_auth_and_hides_geometry(client):
    r = client.get("/api/v1/incidents/public")
    assert r.status_code == 200
    for row in r.json():
        assert set(row) == {"id", "lat", "lon", "status", "confidence", "created_at", "region_name"}


# ------------------------------------------------------------------ review
def test_review_confirm_and_false_alarm(client, stub_analyze, forester_headers, mchs_headers):
    stub_analyze(fake_detection(0.5, "fire"))  # -> PENDING_VERIFICATION
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]

    assert client.post(f"/api/v1/incidents/{iid}/review", json={"verdict": "maybe"},
                       headers=forester_headers).status_code == 422
    ok = client.post(f"/api/v1/incidents/{iid}/review", json={"verdict": "confirm"}, headers=forester_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "CONFIRMED"
    assert ok.json()["reviewed_by_id"] is not None
    # a second review is rejected
    assert client.post(f"/api/v1/incidents/{iid}/review", json={"verdict": "confirm"},
                       headers=forester_headers).status_code == 409


def test_false_alarm_moves_the_clean_image(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.5, "fire"))
    body = client.post("/api/v1/incidents/report", files=_photo(),
                       data={"lat": "56.0", "lon": "92.9"}).json()
    iid = body["id"]
    r = client.post(f"/api/v1/incidents/{iid}/review", json={"verdict": "false_alarm"}, headers=forester_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "FALSE_ALARM"
    assert r.json()["image_path"].startswith("false_positives/")


def test_review_on_clear_incident_conflicts(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.05))
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]
    r = client.post(f"/api/v1/incidents/{iid}/review", json={"verdict": "confirm"}, headers=forester_headers)
    assert r.status_code == 409


# ------------------------------------------------------------------ editing
def test_patch_location_permissions_and_effect(client, stub_analyze, citizen_headers, forester_headers):
    stub_analyze(fake_detection(0.9))
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]
    # anonymous incident -> a citizen may not move it
    assert client.patch(f"/api/v1/incidents/{iid}/location", json={"lat": 56.1, "lon": 92.8},
                        headers=citizen_headers).status_code == 403
    r = client.patch(f"/api/v1/incidents/{iid}/location", json={"lat": 56.1, "lon": 92.8, "note": "уточнено"},
                     headers=forester_headers)
    assert r.status_code == 200
    assert r.json()["lat"] == 56.1
    assert r.json()["gps_source"] == "MANUAL"
    assert "уточнено" in r.json()["region_name"]
    assert r.json()["hazard_geojson"] is not None


def test_delete_incident_requires_staff_and_removes_files(client, stub_analyze, citizen_headers, forester_headers):
    stub_analyze(fake_detection(0.9))
    body = client.post("/api/v1/incidents/report", files=_photo(),
                       data={"lat": "56.0", "lon": "92.9"}).json()
    iid = body["id"]
    assert client.delete(f"/api/v1/incidents/{iid}", headers=citizen_headers).status_code == 403
    assert client.delete(f"/api/v1/incidents/{iid}", headers=forester_headers).status_code == 200
    assert client.get(f"/api/v1/incidents/{iid}", headers=forester_headers).status_code == 404


def test_media_requires_a_valid_signature(client, stub_analyze):
    stub_analyze(fake_detection(0.9))
    body = client.post("/api/v1/incidents/report", files=_photo(),
                       data={"lat": "56.0", "lon": "92.9"}).json()
    path = body["image_path"]
    assert client.get(f"/api/v1/media/{path}").status_code == 403
    assert client.get(f"/api/v1/media/{path}?token=999.forged").status_code == 403
    ok = client.get(body["image_url"])
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"


def test_refresh_requires_staff(client, stub_analyze, citizen_headers, forester_headers):
    stub_analyze(fake_detection(0.9))
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]
    assert client.post(f"/api/v1/incidents/{iid}/refresh", headers=citizen_headers).status_code == 403
    assert client.post(f"/api/v1/incidents/{iid}/refresh", headers=forester_headers).status_code == 200


def test_general_report_aggregates(client, stub_analyze, forester_headers):
    stub_analyze(fake_detection(0.9))
    client.post("/api/v1/incidents/report", files=_photo(), data={"lat": "56.0", "lon": "92.9"})
    r = client.get("/api/v1/incidents/general-report", headers=forester_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["by_status"]["CRITICAL_ALERT"] == 1
    assert data["by_source"]["PHOTO"] == 1


def test_pdf_report(client, stub_analyze, mchs_headers, forester_headers):
    stub_analyze(fake_detection(0.9))
    iid = client.post("/api/v1/incidents/report", files=_photo(),
                      data={"lat": "56.0", "lon": "92.9"}).json()["id"]
    assert client.get(f"/api/v1/incidents/{iid}/report.pdf", headers=forester_headers).status_code == 403
    r = client.get(f"/api/v1/incidents/{iid}/report.pdf", headers=mchs_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


# ------------------------------------------------------------------ guided intake
def test_analyze_returns_draft_without_creating_an_incident(client, stub_analyze):
    stub_analyze(fake_detection(0.93))
    r = client.post("/api/v1/incidents/analyze", files=_photo())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["draft_id"] and d["confidence"] == 0.93
    assert d["status"] == "CRITICAL_ALERT"
    assert d["annotated_url"].startswith("/api/v1/media/drafts/")
    assert incident_count() == 0, "analysis alone must not create an incident"


def test_analyze_rejects_non_media(client, stub_analyze):
    stub_analyze()
    r = client.post("/api/v1/incidents/analyze",
                    files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")})
    assert r.status_code == 415


def test_confirm_creates_incident_at_the_confirmed_point(client, stub_analyze):
    stub_analyze(fake_detection(0.9))
    draft = client.post("/api/v1/incidents/analyze", files=_photo()).json()
    r = client.post("/api/v1/incidents/confirm",
                    json={"draft_id": draft["draft_id"], "lat": 56.02, "lon": 92.88, "note": "уточнено"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lat"] == 56.02 and body["gps_source"] == "MANUAL"
    assert body["status"] == "CRITICAL_ALERT"
    assert "уточнено" in body["region_name"]
    assert body["annotated_url"].startswith("/api/v1/media/images/")
    assert incident_count() == 1


def test_confirm_requires_a_fresh_draft(client, stub_analyze):
    stub_analyze()
    r = client.post("/api/v1/incidents/confirm", json={"draft_id": "deadbeefdeadbeef", "lat": 56.0, "lon": 92.9})
    assert r.status_code == 410


def test_confirm_rejects_coordinates_outside_russia(client, stub_analyze):
    stub_analyze()
    draft = client.post("/api/v1/incidents/analyze", files=_photo()).json()
    r = client.post("/api/v1/incidents/confirm",
                    json={"draft_id": draft["draft_id"], "lat": 40.0, "lon": 10.0})
    assert r.status_code == 422


def test_analyze_reports_missing_exif(client, stub_analyze):
    stub_analyze(fake_detection(0.9))
    d = client.post("/api/v1/incidents/analyze", files=_photo()).json()
    assert d["has_exif"] is False
    assert d["lat"] is None and d["gps_source"] == "NONE"


def test_analyze_and_confirm_flow_keeps_a_thumbnail(client, stub_analyze):
    stub_analyze(fake_detection(0.9, "fire"))
    r = client.post("/api/v1/incidents/analyze", files=_photo())
    assert r.status_code == 200
    d = client.post("/api/v1/incidents/confirm",
                    json={"draft_id": r.json()["draft_id"], "lat": 56.0, "lon": 92.9}).json()
    assert d["thumb_url"], "a thumbnail must be produced"
    from pathlib import Path

    from app import config
    assert (Path(config.UPLOADS) / d["annotated_path"]).exists()
    assert (Path(config.UPLOADS) / d["thumb_path"]).exists()


def test_forester_may_scan_rtsp_but_citizen_may_not(client, forester_headers, citizen_headers):
    """RTSP belongs to the analysis page for foresters and MCHS."""
    body = {"url": "rtsp://camera.example.ru/stream", "lat": 56.0, "lon": 92.9, "seconds": 5}
    assert client.post("/api/v1/incidents/rtsp", json=body, headers=citizen_headers).status_code == 403
    # an unreachable but well-formed stream must not be rejected by the auth layer
    assert client.post("/api/v1/incidents/rtsp", json=body, headers=forester_headers).status_code != 403
