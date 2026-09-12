from app.core.cloudinary import delete_file_from_cloudinary


async def test_delete_by_public_id_uses_image_resource_type_not_auto(monkeypatch):
    """Live-environment fix: Cloudinary's destroy API rejects
    resource_type='auto' (only valid for uploads), which crashed every
    caller of delete_file_from_cloudinary with an unhandled 500."""
    captured = {}

    def fake_destroy(public_id, resource_type=None, **kwargs):
        captured["public_id"] = public_id
        captured["resource_type"] = resource_type
        return {"result": "ok"}

    monkeypatch.setattr("cloudinary.uploader.destroy", fake_destroy)

    await delete_file_from_cloudinary("kazihub/portfolio/abc123")

    assert captured["resource_type"] == "image"
    assert captured["resource_type"] != "auto"
    assert captured["public_id"] == "kazihub/portfolio/abc123"


async def test_delete_by_full_url_extracts_resource_type_from_path(monkeypatch):
    captured = {}

    def fake_destroy(public_id, resource_type=None, **kwargs):
        captured["public_id"] = public_id
        captured["resource_type"] = resource_type
        return {"result": "ok"}

    monkeypatch.setattr("cloudinary.uploader.destroy", fake_destroy)

    url = "https://res.cloudinary.com/demo/image/upload/v1234567890/kazihub/portfolio/abc123.png"
    await delete_file_from_cloudinary(url)

    assert captured["resource_type"] == "image"
    assert captured["public_id"] == "kazihub/portfolio/abc123"
