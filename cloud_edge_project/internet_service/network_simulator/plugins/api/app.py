"""FastAPI application factory for the read-only controller API."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from domain.enums import NetworkState

from .routes import ApiReadService
from .schemas import HealthResponse, LinkResponse, RuntimeResponse


def _package_version() -> str:
    """AUD-08: read the single source of truth from pyproject.toml."""
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject.open("rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, ValueError, ImportError):
        # pyproject.toml 不可用时回退到与 Docker 镜像一致的当前发布版本。
        return "3.1.0"


def create_app(service: ApiReadService) -> FastAPI:
    app = FastAPI(
        title="Network Simulator V3 API",
        version=_package_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return service.health()

    @app.get("/api/v1/network/links", response_model=list[LinkResponse])
    def list_links(
        sender_id: str | None = None,
        edge_id: str | None = None,
        state: NetworkState | None = None,
        available: bool | None = None,
    ) -> list[LinkResponse]:
        return service.list_links(
            sender_id=sender_id,
            edge_id=edge_id,
            state=state,
            available=available,
        )

    @app.get("/api/v1/network/links/{link_id}", response_model=LinkResponse)
    def get_link(link_id: str) -> LinkResponse:
        try:
            return service.get_link(link_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown link_id") from exc

    @app.get("/api/v1/network/runtime", response_model=RuntimeResponse)
    def runtime() -> RuntimeResponse:
        return service.runtime()

    return app
