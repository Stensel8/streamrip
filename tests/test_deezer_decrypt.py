import os

import pytest
from Cryptodome.Cipher import Blowfish

from streamrip.client.downloadable import DEEZER_STRIPE, DeezerDownloadable

IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"


def _encrypt(key: bytes, plain: bytes) -> bytes:
    """What Deezer serves: the first 2048 bytes of every stripe encrypted."""
    out = bytearray(plain)
    for i in range(0, len(out), DEEZER_STRIPE):
        if len(out) - i >= 2048:
            block = bytes(out[i : i + 2048])
            out[i : i + 2048] = Blowfish.new(key, Blowfish.MODE_CBC, IV).encrypt(block)
    return bytes(out)


class _Content:
    def __init__(self, body: bytes, sizes: list[int]):
        self.body, self.sizes = body, sizes

    async def iter_chunked(self, _n):
        # Irregular sizes, so stripes straddle the chunks the server sends.
        pos, i = 0, 0
        while pos < len(self.body):
            size = self.sizes[i % len(self.sizes)]
            yield self.body[pos : pos + size]
            pos, i = pos + size, i + 1


class _Resp:
    def __init__(self, body: bytes):
        self.headers = {"Content-Length": str(len(body))}
        self.content = _Content(body, [1000, 7000, 333, 20000])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, body: bytes):
        self.body = body

    def get(self, _url, **_):
        return _Resp(self.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [5 * DEEZER_STRIPE + 3000, 5 * DEEZER_STRIPE + 100])
async def test_encrypted_stream_decrypts_to_the_original(tmp_path, length):
    plain = os.urandom(length)
    info = {
        "url": "https://cdn.example/mobile/1/track",
        "quality_to_size": [0, 0, length],
        "quality": 2,
        "id": "3135556",
    }
    key = DeezerDownloadable._generate_blowfish_key(info["id"])
    downloadable = DeezerDownloadable(_Session(_encrypt(key, plain)), info)
    received = []

    await downloadable._download(str(tmp_path / "t.flac"), received.append)

    assert (tmp_path / "t.flac").read_bytes() == plain
    assert sum(received) == length
