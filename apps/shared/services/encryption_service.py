import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class EncryptionService:
    @staticmethod
    def _get_fernet():
        raw_key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        fernet_key = base64.urlsafe_b64encode(raw_key)
        return Fernet(fernet_key)

    @classmethod
    def encrypt(cls, value):
        if not value:
            return ""

        return cls._get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")

    @classmethod
    def decrypt(cls, value):
        if not value:
            return ""

        try:
            return cls._get_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return ""
