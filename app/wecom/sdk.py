"""ctypes 封装 WeWorkFinanceSdk.dll（企业微信会话存档 SDK v3.0，Windows）。

函数签名以 vendor/wecom_sdk/WeWorkFinanceSdk_C.h 为准。
返回值非 0 统一转成 WeComSdkError(code)。重点错误码（见头文件注释）：
  10001 网络错误 -> 可重试
  10009 IP 非法 -> 触发公网 IP 变化流程，不高频重试
  10010 数据过期（超过 5 天）-> 告警
"""
from __future__ import annotations

import ctypes
import os
from ctypes import c_char_p, c_int, c_uint, c_ulonglong
from pathlib import Path

SDK_BIN_DIR = Path(__file__).resolve().parents[2] / "vendor" / "wecom_sdk" / "bin"
DLL_PATH = SDK_BIN_DIR / "WeWorkFinanceSdk.dll"


class WeComSdkError(Exception):
    """SDK 返回非 0 错误码。"""

    def __init__(self, code: int, api: str = ""):
        self.code = code
        self.api = api
        super().__init__(f"WeComSdkError({code})" + (f" in {api}" if api else ""))


def _load() -> ctypes.CDLL:
    if not DLL_PATH.exists():
        raise FileNotFoundError(f"找不到 SDK：{DLL_PATH}（请把 WeWorkFinanceSdk.dll 及依赖 dll 放到该目录）")
    # 让 Windows 能从 bin 目录找到 libcrypto/libssl/libcurl 等依赖
    os.add_dll_directory(str(SDK_BIN_DIR))
    return ctypes.CDLL(str(DLL_PATH))


class WeComFinanceSdk:
    """会话存档 SDK 的薄封装。用 with 管理生命周期。"""

    def __init__(self, corpid: str, secret: str):
        self._lib = _load()
        lib = self._lib
        lib.NewSdk.restype = ctypes.c_void_p
        lib.Init.argtypes = [ctypes.c_void_p, c_char_p, c_char_p]
        lib.GetChatData.argtypes = [ctypes.c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, ctypes.c_void_p]
        lib.DecryptData.argtypes = [c_char_p, c_char_p, ctypes.c_void_p]
        lib.DestroySdk.argtypes = [ctypes.c_void_p]
        lib.NewSlice.restype = ctypes.c_void_p
        lib.FreeSlice.argtypes = [ctypes.c_void_p]
        lib.GetContentFromSlice.argtypes = [ctypes.c_void_p]
        lib.GetContentFromSlice.restype = c_char_p
        lib.GetSliceLen.argtypes = [ctypes.c_void_p]
        lib.NewMediaData.restype = ctypes.c_void_p
        lib.FreeMediaData.argtypes = [ctypes.c_void_p]
        lib.GetMediaData.argtypes = [ctypes.c_void_p, c_char_p, c_char_p, c_char_p, c_char_p, c_int, ctypes.c_void_p]
        lib.GetOutIndexBuf.argtypes = [ctypes.c_void_p]
        lib.GetOutIndexBuf.restype = c_char_p
        lib.GetData.argtypes = [ctypes.c_void_p]
        lib.GetData.restype = ctypes.POINTER(ctypes.c_char)
        lib.GetDataLen.argtypes = [ctypes.c_void_p]
        lib.IsMediaDataFinish.argtypes = [ctypes.c_void_p]

        self._sdk = lib.NewSdk()
        if not self._sdk:
            raise WeComSdkError(-1, "NewSdk")
        ret = lib.Init(self._sdk, corpid.encode(), secret.encode())
        if ret != 0:
            lib.DestroySdk(self._sdk)
            self._sdk = None
            raise WeComSdkError(ret, "Init")

    def close(self) -> None:
        if self._sdk:
            self._lib.DestroySdk(self._sdk)
            self._sdk = None

    def __enter__(self) -> "WeComFinanceSdk":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_chat_data(self, seq: int, limit: int = 1000, timeout: int = 10,
                      proxy: str = "", passwd: str = "") -> str:
        """返回 JSON 字符串（errcode/errmsg/chatdata）。SDK 错误抛 WeComSdkError。"""
        slice_ = self._lib.NewSlice()
        try:
            ret = self._lib.GetChatData(
                self._sdk, c_ulonglong(seq), limit,
                proxy.encode() if proxy else None,
                passwd.encode() if passwd else None,
                timeout, slice_,
            )
            if ret != 0:
                raise WeComSdkError(ret, "GetChatData")
            data = self._lib.GetContentFromSlice(slice_)
            return data.decode("utf-8") if data else ""
        finally:
            self._lib.FreeSlice(slice_)

    def decrypt_data(self, encrypt_key: bytes, encrypt_msg: str) -> str:
        """encrypt_key 是 encrypt_random_key 用私钥 RSA 解密后的字节串。"""
        slice_ = self._lib.NewSlice()
        try:
            ret = self._lib.DecryptData(encrypt_key, encrypt_msg.encode(), slice_)
            if ret != 0:
                raise WeComSdkError(ret, "DecryptData")
            data = self._lib.GetContentFromSlice(slice_)
            return data.decode("utf-8") if data else ""
        finally:
            self._lib.FreeSlice(slice_)

    def get_media_data(self, sdkfileid: str, timeout: int = 30,
                       proxy: str = "", passwd: str = "") -> bytes:
        """分片下载媒体文件，返回完整内容。"""
        chunks: list[bytes] = []
        indexbuf = b""
        while True:
            media = self._lib.NewMediaData()
            try:
                ret = self._lib.GetMediaData(
                    self._sdk, indexbuf, sdkfileid.encode(),
                    proxy.encode() if proxy else None,
                    passwd.encode() if passwd else None,
                    timeout, media,
                )
                if ret != 0:
                    raise WeComSdkError(ret, "GetMediaData")
                data_len = self._lib.GetDataLen(media)
                if data_len > 0:
                    data_ptr = self._lib.GetData(media)
                    chunks.append(ctypes.string_at(data_ptr, data_len))
                if self._lib.IsMediaDataFinish(media) == 1:
                    break
                out = self._lib.GetOutIndexBuf(media)
                indexbuf = out or b""
            finally:
                self._lib.FreeMediaData(media)
        return b"".join(chunks)
