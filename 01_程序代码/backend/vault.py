"""Local credential key storage; Windows binds the key to the current user via DPAPI."""
import os
from pathlib import Path

PREFIX = b'EF-DPAPI-1\n'

def _dpapi(data: bytes, decrypt=False) -> bytes:
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('无法访问当前 Windows 用户的凭据，请在此电脑重新填写连接密钥')
    try: return ctypes.string_at(target.data, target.size)
    finally: kernel.LocalFree(target.data)

def load_key(private: Path, generate):
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = private / 'connections.key'
    if os.name != 'nt': private.chmod(0o700)
    if not path.exists():
        key = generate()
        content = PREFIX + _dpapi(key) if os.name == 'nt' else key
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
            f.write(content)
    content = path.read_bytes()
    if os.name == 'nt':
        if not content.startswith(PREFIX):
            raise ValueError('此密钥来自其他平台，请在当前 Windows 电脑重新配置连接')
        return _dpapi(content[len(PREFIX):], decrypt=True)
    path.chmod(0o600)
    if content.startswith(PREFIX): raise ValueError('Windows 凭据不能迁移到此平台，请重新配置连接')
    return content
