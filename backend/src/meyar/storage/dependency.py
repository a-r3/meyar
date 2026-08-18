from meyar.config import get_settings
from meyar.storage.local import LocalFilesystemStorage


def get_document_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=get_settings().storage_root)
