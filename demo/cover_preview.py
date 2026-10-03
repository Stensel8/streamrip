"""Offline cover preview for Linux/Windows testers; no account or network needed.

From the checkout: poetry run python demo/cover_preview.py
"""

import asyncio
import io

from PIL import Image, ImageDraw, ImageFont

from streamrip.metadata import SearchResults, Summary
from streamrip.rip.cover_preview import image_widget
from streamrip.rip.search_menu import CoverCache, SearchMenu


def artwork(accent: str) -> bytes:
    """Synthetic album art with fine lines and small type to compare renderers."""
    image = Image.new("RGB", (640, 640))
    draw = ImageDraw.Draw(image)
    for y in range(640):
        draw.line((0, y, 640, y), fill=(12 + y // 20, 20 + y // 9, 48 + y // 5))
    draw.ellipse((155, 80, 495, 420), fill=accent)
    for radius in range(20, 165, 8):
        draw.ellipse(
            (325 - radius, 250 - radius, 325 + radius, 250 + radius),
            outline="#fcdfb0",
            width=2,
        )
    draw.polygon(
        [
            (0, 470),
            (200, 290),
            (340, 460),
            (470, 345),
            (640, 495),
            (640, 640),
            (0, 640),
        ],
        fill="#102636",
    )
    for x in range(0, 640, 12):
        draw.line((x, 470, 320 + (x - 320) * 2, 640), fill="#466579", width=1)
    draw.text(
        (38, 34), "NORTH / SOUTH", font=ImageFont.load_default(size=28), fill="white"
    )
    draw.text(
        (36, 520), "AFTER THE RAIN", font=ImageFont.load_default(size=52), fill="white"
    )
    draw.text(
        (40, 594),
        "SYNTHETIC COVER  /  TERMINAL PREVIEW",
        font=ImageFont.load_default(size=17),
        fill="#c7dce5",
    )
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


class DemoCovers(CoverCache):
    """Exercise the same decoding/cache pipeline with generated image bytes."""

    async def _fetch(self, url: str) -> bytes:
        await asyncio.sleep(0.25)
        return artwork("#da875a" if url == "demo:warm" else "#36adab")


async def main():
    pixel = image_widget()  # Probe before the application owns input.
    renderer = "pixels" if pixel else "blocks"
    results = SearchResults(
        [
            Summary(
                "album",
                "1",
                "After the Rain",
                "North / South",
                year="2026",
                details=[
                    ("Released", "2026-10-03"),
                    ("Tracks", "12"),
                    ("Genre", "Ambient / Electronic"),
                    ("Quality", "FLAC 24-bit / 96 kHz"),
                    ("Preview", renderer),
                ],
                image_url="demo:warm",
            ),
            Summary(
                "album",
                "2",
                "After the Rain (Night Edition)",
                "North / South",
                year="2026",
                details=[("Tracks", "15"), ("Preview", renderer)],
                image_url="demo:cool",
            ),
            Summary(
                "album",
                "3",
                "Without Artwork",
                "North / South",
                details=[("Tracks", "8")],
            ),
        ]
    )
    app = SearchMenu(results, "demo", "album", "North / South", pixel_widget=pixel)
    app.covers = DemoCovers()
    try:
        choices = await app.run_async()
    finally:
        await app.covers.close()
    print(f"Demo selection: {choices}. No downloads performed.")


if __name__ == "__main__":
    asyncio.run(main())
