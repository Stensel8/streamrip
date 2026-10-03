TIDAL_COVER_URL = "https://resources.tidal.com/images/{uuid}/{width}x{height}.jpg"


class Covers:
    """An album's cover art: per size, a url and (once downloaded) a path."""

    SIZES = ("original", "large", "small", "thumbnail")  # largest first
    CoverEntry = tuple[str, str | None, str | None]

    def __init__(self):
        """Start with one empty entry per size."""
        self._covers: list[Covers.CoverEntry] = [(s, None, None) for s in self.SIZES]

    def set_cover(self, size: str, url: str | None, path: str | None):
        """Set the URL and local path of one size."""
        self._covers[self.SIZES.index(size)] = (size, url, path)

    def set_cover_url(self, size: str, url: str):
        """Set the URL of one size, with no local path yet."""
        self.set_cover(size, url, None)

    def set_path(self, size: str, path: str):
        """Set the local path of one size, keeping its URL."""
        _, url, _ = self._covers[self.SIZES.index(size)]
        self.set_cover(size, url, path)

    def set_largest_path(self, path: str):
        """Set the local path of the largest cover that has a URL."""
        size, url, _ = self.largest()
        self.set_cover(size, url, path)

    def empty(self) -> bool:
        """Whether no size has a URL."""
        return all(url is None for _, url, _ in self._covers)

    def largest(self) -> CoverEntry:
        """The largest cover that has a URL."""
        return self._first_from(0)

    def get_size(self, size: str) -> CoverEntry:
        """The cover of this size, or else the next smaller one there is."""
        return self._first_from(self.SIZES.index(size))

    def _first_from(self, i: int) -> CoverEntry:
        """The first entry with a URL from size index `i` down; raises if there is none."""
        for entry in self._covers[i:]:
            if entry[1] is not None:
                return entry
        raise Exception(f"No cover of size {self.SIZES[i]} or smaller in {self}")

    @classmethod
    def from_qobuz(cls, resp):
        img = resp["image"]

        c = cls()
        c.set_cover_url("original", "org".join(img["large"].rsplit("600", 1)))
        c.set_cover_url("large", img["large"])
        c.set_cover_url("small", img["small"])
        c.set_cover_url("thumbnail", img["thumbnail"])
        return c

    @classmethod
    def from_deezer(cls, resp):
        c = cls()
        c.set_cover_url("original", resp["cover_xl"])
        c.set_cover_url("large", resp["cover_big"])
        c.set_cover_url("small", resp["cover_medium"])
        c.set_cover_url("thumbnail", resp["cover_small"])
        return c

    @classmethod
    def from_soundcloud(cls, resp):
        c = cls()
        cover_url = (resp["artwork_url"] or resp["user"].get("avatar_url")).replace(
            "large",
            "t500x500",
        )
        c.set_cover_url("large", cover_url)
        return c

    @classmethod
    def from_tidal(cls, resp):
        """The covers a Tidal response's `cover` id gives, or None without one."""
        uuid = resp["cover"]
        if not uuid:
            return None

        c = cls()
        for size, px in zip(cls.SIZES, (1280, 640, 320, 160)):
            url = TIDAL_COVER_URL.format(
                uuid=uuid.replace("-", "/"), width=px, height=px
            )
            c.set_cover_url(size, url)
        return c

    def __repr__(self):
        covers = "\n".join(map(repr, self._covers))
        return f"Covers({covers})"
