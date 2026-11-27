"""
Validation and normalisation of the custom icon, logo and privacy-screen PNGs.

Images reach the generator three ways: a multipart upload (web form), a data
URL in a hidden field (web form after "Load Configuration") and a base64
string in the JSON API. All three go through ``normalize_png`` so the same
rules apply everywhere, and what is written to ``png/<uuid>/`` -- and later
fed to ImageMagick and embedded in the client by the build workflows -- is
always a PNG that Pillow itself has decoded and re-encoded.
"""
import base64
import binascii
import io
import os
import re
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# DATA_UPLOAD_MAX_MEMORY_SIZE is None in settings, so nothing upstream caps
# the size of a base64 image in a request body.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MIN_ICON_DIMENSION = 16

# (form file field, base64 field, stored file name, label, must be square)
IMAGE_KINDS = {
    'icon': ('iconfile', 'iconbase64', 'icon.png', 'Custom App Icon', True),
    'logo': ('logofile', 'logobase64', 'logo.png', 'Custom App Logo', False),
    'privacy': ('privacyfile', 'privacybase64', 'privacy.png', 'Custom privacy screen', False),
}

# Modes Pillow writes to PNG without loss; anything else (16-bit, CMYK, ...)
# is converted to RGBA.
_PNG_MODES = {'1', 'L', 'LA', 'P', 'RGB', 'RGBA'}
_DATA_URL_RE = re.compile(r'^data:([\w.+/-]*)(;[\w=.+-]+)*;base64,', re.IGNORECASE)
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class ImageValidationError(ValueError):
    pass


def _read_input(value, label):
    """Return the raw bytes of an upload, data URL or base64 string (None if empty)."""
    if value is None or value == '' or value is False:
        return None
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        match = _DATA_URL_RE.match(text)
        if match:
            mime = match.group(1).lower()
            if mime and mime != 'image/png':
                raise ImageValidationError(f"{label} must be a PNG image (got {mime}).")
            text = text[match.end():]
        # base64 inflates by 4/3; refuse before decoding anything huge.
        if len(text) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ImageValidationError(f"{label} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        try:
            data = base64.b64decode(re.sub(r'\s+', '', text), validate=True)
        except (binascii.Error, ValueError):
            raise ImageValidationError(f"{label} is not valid base64 data.")
    elif hasattr(value, 'chunks'):
        if getattr(value, 'size', None) and value.size > MAX_IMAGE_BYTES:
            raise ImageValidationError(f"{label} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        buf = io.BytesIO()
        for chunk in value.chunks():
            buf.write(chunk)
            if buf.tell() > MAX_IMAGE_BYTES:
                raise ImageValidationError(f"{label} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        data = buf.getvalue()
    else:
        raise ImageValidationError(f"{label} has an unsupported type.")

    if not data:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageValidationError(f"{label} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    return data


def normalize_png(value, kind):
    """
    Validate an icon/logo/privacy image and return it re-encoded as PNG bytes.

    ``value`` may be an uploaded file, a data URL, a bare base64 string or raw
    bytes. Returns None when no image was given; raises ImageValidationError
    with a user-facing message when the image is unusable.
    """
    label = IMAGE_KINDS[kind][3]
    must_be_square = IMAGE_KINDS[kind][4]
    data = _read_input(value, label)
    if data is None:
        return None
    if not data.startswith(_PNG_SIGNATURE):
        raise ImageValidationError(f"{label} must be a PNG image.")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format != 'PNG':
                raise ImageValidationError(f"{label} must be a PNG image.")
            width, height = probe.size
            # Check dimensions from the header before decoding any pixels, so
            # a tiny file claiming a huge canvas is never expanded in memory.
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                raise ImageValidationError(
                    f"{label} is {width}x{height}; the maximum is "
                    f"{MAX_IMAGE_DIMENSION}x{MAX_IMAGE_DIMENSION}."
                )
            if must_be_square:
                if width != height:
                    raise ImageValidationError(f"{label} must be square (got {width}x{height}).")
                if width < MIN_ICON_DIMENSION:
                    raise ImageValidationError(
                        f"{label} must be at least {MIN_ICON_DIMENSION}x{MIN_ICON_DIMENSION} "
                        "(256x256 recommended)."
                    )
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in _PNG_MODES:
                img = img.convert('RGBA')
            out = io.BytesIO()
            save_kwargs = {'optimize': True}
            if 'transparency' in img.info and img.mode != 'RGBA':
                save_kwargs['transparency'] = img.info['transparency']
            img.save(out, format='PNG', **save_kwargs)
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ImageValidationError(f"{label} is not a valid PNG image.")

    return out.getvalue()


def save_png(png_bytes, uuid, name):
    """Write already-normalised PNG bytes to png/<uuid>/<name>."""
    directory = Path('png') / uuid
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(png_bytes)
    os.replace(tmp, path)
    return str(path)
