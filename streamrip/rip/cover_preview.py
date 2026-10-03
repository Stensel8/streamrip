"""Terminal image support, isolated from the search menu and provider clients."""

import io
import sys

from PIL import Image, ImageOps
from rich.color import Color
from rich.control import Control
from rich.style import Style
from rich.text import Text
from textual.containers import Container
from textual.widgets import Static


def decode_cover(data: bytes) -> Image.Image:
    """Decode once, preserving aspect ratio and bounding preview memory."""
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 16_000_000:
            raise ValueError("Cover too large for a preview")
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((640, 640), Image.Resampling.LANCZOS)
    return image


def image_widget():
    """Probe once, before Textual owns input; None means quadrant blocks.

    Keep these imports lazy: commands such as --help, JSON search and downloads
    must not query the terminal.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        from textual_image import renderable, widget

        if renderable.Image is renderable.SixelImage:
            return _sixel_widget()
        if renderable.Image is renderable.TGPImage:
            return widget.Image
    except Exception:
        pass  # An unsupported terminal must still have a usable search menu.
    return None


def _sixel_widget():
    """Adapt textual-image 0.14's SIXEL cursor restoration to Textual's strips.

    Its last segment moves to the row *below* the image, but Textual continues
    writing the last image row (including adjacent metadata) there and may add
    a newline. Near the bottom this scrolls the entire terminal. Keep the cursor
    on that last row. This small private-API shim is covered by terminal tests;
    keep textual-image within 0.14 until a dependency upgrade verifies it again.
    """
    from textual_image.widget.sixel import Image as SixelImage
    from textual_image.widget.sixel import _ImageSixelImpl

    class CoverSixels(_ImageSixelImpl):
        def _get_sixel_segments(self, sixel_data):
            segments = list(super()._get_sixel_segments(sixel_data))
            region = self.screen.find_widget(self).visible_region
            segments[-1] = segments[-1]._replace(
                text=Control.move_to(region.right, region.bottom - 1).segment.text
            )
            return segments

        def render_lines(self, crop):
            try:
                return super().render_lines(crop)
            except Exception as error:
                self.post_message(self.Failed(error))
                return []

    class CoverSixelImage(SixelImage, Renderable=SixelImage._Renderable):
        def compose(self):
            yield CoverSixels(self.image, self._sixel_options)

    return CoverSixelImage


# Quadrant blocks by which quarters of the cell they fill: top left 1, top
# right 2, bottom left 4, bottom right 8.
QUADRANTS = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"
CORNERS = ((0, 0), (1, 0), (0, 1), (1, 1))


def _mean(pixels: list) -> tuple[int, int, int]:
    """The average color of some (r, g, b) pixels."""
    return tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))


def _cell(pixels: list) -> tuple[str, tuple, tuple]:
    """The quadrant block, and its two colors, closest to a cell's 2x2 pixels."""
    best = None
    for mask in range(1, 8):  # the other 8 are these with the colors swapped
        on = [p for i, p in enumerate(pixels) if mask >> i & 1]
        off = [p for i, p in enumerate(pixels) if not mask >> i & 1]
        fg, bg = _mean(on), _mean(off)
        error = sum(
            sum((a - b) ** 2 for a, b in zip(p, fg if mask >> i & 1 else bg))
            for i, p in enumerate(pixels)
        )
        if best is None or error < best[0]:
            best = (error, QUADRANTS[mask], fg, bg)
    return best[1:]


def cover_rows(data: bytes | Image.Image, rows: int) -> list[Text]:
    """An image as rows of quadrant blocks, two colors per character: twice
    as sharp across as half blocks, which show only a top and bottom pixel.
    """
    columns = rows * 2  # a character is about twice as tall as it is wide
    image = decode_cover(data) if isinstance(data, bytes) else data
    image = ImageOps.pad(
        image,
        (columns * 2, columns * 2),
        method=Image.Resampling.LANCZOS,
        color="black",
    )
    image = image.resize((columns * 2, rows * 2), Image.Resampling.LANCZOS)
    lines = []
    for y in range(rows):
        line = Text()
        for x in range(columns):
            pixels = [image.getpixel((2 * x + dx, 2 * y + dy)) for dx, dy in CORNERS]
            char, fg, bg = _cell(pixels)
            line.append(
                char, Style(color=Color.from_rgb(*fg), bgcolor=Color.from_rgb(*bg))
            )
        lines.append(line)
    return lines


class BlockCover(Static):
    """The existing two-color quadrant renderer, sized by its parent."""

    DEFAULT_CSS = "BlockCover { width: 100%; height: 100%; }"

    def __init__(self, image: Image.Image):
        super().__init__()
        self.image = image
        self._rows = -1
        self._rendered = Text()

    def render(self):
        rows = min(self.size.height, self.size.width // 2)
        if rows != self._rows:
            self._rows = rows
            self._rendered = (
                Text("\n").join(cover_rows(self.image, rows)) if rows > 0 else Text()
            )
        return self._rendered


class CoverPane(Container):
    """A fixed preview slot; only replace its widget when the image changes."""

    DEFAULT_CSS = """
    CoverPane { height: 100%; overflow: hidden; }
    CoverPane > * { width: 100%; height: 100%; }
    """

    def __init__(self, pixel_widget=None):
        super().__init__(id="cover")
        self.pixel_widget = pixel_widget
        self.image = None

    def _fallback(self, error: Exception):
        # The library invokes this for asynchronous encoding/render failures.
        # Disable pixels for subsequent covers for this session as well.
        self.pixel_widget = None
        # A fallback nested inside an auto-sized image needs a definite height.
        for child in self.children:
            child.styles.width = "100%"
            child.styles.height = "100%"
        return BlockCover(self.image)

    async def show_image(self, image: Image.Image | None):
        if image is self.image:
            return
        self.image = image
        await self.remove_children()
        if image is None:
            return
        try:
            if self.pixel_widget:
                child = self.pixel_widget(image, on_error=self._fallback)
                # Let the renderer fit actual terminal pixels, not an assumed
                # 2:1 character aspect ratio (fonts and display scaling vary).
                child.styles.width = "auto"
                child.styles.height = "auto"
                child.styles.max_width = "100%"
                child.styles.max_height = "100%"
                if self.pixel_widget is None:  # on_error during construction
                    child = BlockCover(image)
            else:
                child = BlockCover(image)
        except Exception as error:
            child = self._fallback(error)
        await self.mount(child)
