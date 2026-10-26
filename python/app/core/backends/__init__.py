"""解码后端包：统一的 DecodeBackend 接口 + 三种实现 + 自动选择。"""
from app.core.backends.base import DecodeBackend, DecodeError
from app.core.backends.factory import auto_backend, describe_backends, open_backend

__all__ = ["DecodeBackend", "DecodeError", "auto_backend", "open_backend", "describe_backends"]
