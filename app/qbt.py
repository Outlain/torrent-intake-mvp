from __future__ import annotations
import qbittorrentapi
from .config import get_settings


class QbtService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def client(self) -> qbittorrentapi.Client:
        client = qbittorrentapi.Client(
            host=self.settings.qbt_host,
            username=self.settings.qbt_username,
            password=self.settings.qbt_password,
            VERIFY_WEBUI_CERTIFICATE=self.settings.qbt_verify_certificate,
            REQUESTS_ARGS={"timeout": self.settings.qbt_request_timeout_seconds},
        )
        try:
            client.auth_log_in()
        except Exception as exc:
            raise RuntimeError(
                "qBittorrent login failed "
                f"(host={self.settings.qbt_host}, user={self.settings.qbt_username}): {self._format_exc(exc)}"
            ) from exc
        return client

    @staticmethod
    def _format_exc(exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return f"{exc.__class__.__name__}: {message}"
        return repr(exc)

    def add_torrent(self, magnet_uri: str, save_path: str, tags: list[str], category: str) -> None:
        client = self.client()
        try:
            client.torrents_add(
                urls=magnet_uri,
                save_path=save_path,
                tags=tags,
                category=category,
                is_paused=False,
            )
        except Exception as exc:
            raise RuntimeError(
                "qBittorrent rejected torrent add request "
                f"(save_path={save_path}, category={category}): {self._format_exc(exc)}"
            ) from exc

    def find_by_unique_tag(self, unique_tag: str):
        client = self.client()
        torrents = client.torrents_info()
        for torrent in torrents:
            torrent_tags = getattr(torrent, "tags", "") or ""
            tags = {t.strip() for t in torrent_tags.split(",") if t.strip()}
            if unique_tag in tags:
                return torrent
        return None

    def get_torrent(self, torrent_hash: str):
        client = self.client()
        torrents = client.torrents_info(torrent_hashes=torrent_hash)
        if not torrents:
            return None
        return torrents[0]

    def pause(self, torrent_hash: str) -> None:
        self.client().torrents_pause(torrent_hashes=torrent_hash)

    def resume(self, torrent_hash: str) -> None:
        self.client().torrents_resume(torrent_hashes=torrent_hash)

    def delete_with_files(self, torrent_hash: str) -> None:
        self.client().torrents_delete(torrent_hashes=torrent_hash, delete_files=True)

    def set_location(self, torrent_hash: str, location: str) -> None:
        self.client().torrents_set_location(torrent_hashes=torrent_hash, location=location)

    def set_category(self, torrent_hash: str, category: str) -> None:
        self.client().torrents_set_category(torrent_hashes=torrent_hash, category=category)

    def set_save_path(self, torrent_hash: str, save_path: str) -> None:
        self.client().torrents_set_save_path(torrent_hashes=torrent_hash, save_path=save_path)

    def list_categories(self) -> list[str]:
        categories = self.client().torrents_categories()
        if hasattr(categories, "keys"):
            return sorted(str(name) for name in categories.keys())
        return []

    def list_save_path_suggestions(self) -> list[str]:
        client = self.client()
        paths: set[str] = set()
        for torrent in client.torrents_info():
            path = getattr(torrent, "save_path", None)
            if isinstance(path, str) and path.strip():
                paths.add(path.strip())
        categories = client.torrents_categories()
        if hasattr(categories, "values"):
            for info in categories.values():
                path = getattr(info, "save_path", None) or getattr(info, "savePath", None)
                if isinstance(path, str) and path.strip():
                    paths.add(path.strip())
        return sorted(paths)

    def resolve_or_create_category(self, category: str, *, create_if_missing: bool) -> str:
        requested = category.strip()
        if not requested:
            raise RuntimeError("final category is empty")

        categories = self.client().torrents_categories()
        existing = {str(name): str(name) for name in categories.keys()}
        exact = existing.get(requested)
        if exact:
            return exact

        lower_map = {name.lower(): name for name in existing}
        case_match = lower_map.get(requested.lower())
        if case_match:
            return case_match

        if not create_if_missing:
            raise RuntimeError(
                f"final category '{requested}' not found in qBittorrent; "
                "enable TI_AUTO_CREATE_FINAL_CATEGORY or create it in qBittorrent first"
            )

        try:
            self.client().torrents_create_category(name=requested)
        except Exception as exc:
            raise RuntimeError(
                f"failed to create qBittorrent category '{requested}': {self._format_exc(exc)}"
            ) from exc
        return requested
