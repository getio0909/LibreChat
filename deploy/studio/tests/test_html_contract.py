import base64
from pathlib import Path

from deploy.studio.models3d import build_model_viewer_html
from deploy.studio.regions import build_compare_viewer_html, build_selection_editor_html


FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "client/src/components/MCPUIResource/__tests__/fixtures"
)
EXPIRY = "2030-01-01T00:00:00Z"


def signed(kind: str, item: str, char: str) -> str:
    payload = f"{kind}:user_1:1893456000:{item}"
    if kind == "edt":
        payload = f"edt:user_1:1893456000:{'a' * 64}:nonce_{char}:{item}"
    elif kind == "model":
        payload = f"user_1:1893456000:{item}"
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"/files/{token}.{char * 64}"


def test_client_html_fixtures_match_real_studio_builders() -> None:
    generated = {
        "selection.html": build_selection_editor_html(
            image_id="img_demo123",
            signed_url=signed("edt", "img_demo123", "a"),
            preview_url=signed("img", "img_demo123", "b"),
            expires_at=EXPIRY,
            preview_expires_at=EXPIRY,
            width=400,
            height=300,
        ),
        "compare.html": build_compare_viewer_html(
            image_id="img_after123",
            base_id="img_demo123",
            selection_id="sel_0123456789abcdef",
            before_url=signed("img", "img_demo123", "c"),
            after_url=signed("img", "img_after123", "d"),
            expires_at=EXPIRY,
            width=400,
            height=300,
            selected_pixels=120,
        ),
        "model.html": build_model_viewer_html(
            model_id="mdl_demo123",
            signed_url=signed("model", "mdl_demo123", "e"),
            expires_at=EXPIRY,
            faces=1000,
            file_size=1024,
        ),
    }
    for filename, html in generated.items():
        assert (FIXTURES / filename).read_text(encoding="utf-8") == html
