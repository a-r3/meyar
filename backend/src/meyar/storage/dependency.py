from meyar.config import get_settings
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage


def get_document_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=get_settings().storage_root)


def get_photo_storage() -> LocalPhotoStorage:
    return LocalPhotoStorage(root=get_settings().storage_root)
