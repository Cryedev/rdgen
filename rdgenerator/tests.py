import base64
import io
import os
import shutil
import tempfile
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase
from PIL import Image

from . import images
from .api_views import validate_generate_params
from .forms import GenerateForm
from .images import ImageValidationError, normalize_png
from .views import _store_image, get_png


def make_png(width=64, height=64, mode='RGBA', fmt='PNG', **save_kwargs):
    color = (255, 0, 0, 128) if mode == 'RGBA' else 128 if mode in ('L', 'P', 'I;16') else (255, 0, 0)
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def data_url(raw, mime='image/png'):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def decode(png):
    img = Image.open(io.BytesIO(png))
    img.load()
    return img


class InChdirTestCase(SimpleTestCase):
    """save_png/get_png use paths relative to the cwd; keep them in a tmp dir."""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)


class NormalizePngTests(SimpleTestCase):
    def test_empty_inputs_return_none(self):
        for value in (None, '', '   ', b'', False):
            self.assertIsNone(normalize_png(value, 'icon'))

    def test_accepts_bytes_data_url_bare_base64_and_upload(self):
        raw = make_png()
        upload = SimpleUploadedFile('icon.png', raw, content_type='image/png')
        for value in (raw, data_url(raw), base64.b64encode(raw).decode(), upload):
            img = decode(normalize_png(value, 'icon'))
            self.assertEqual((img.format, img.size, img.mode), ('PNG', (64, 64), 'RGBA'))

    def test_base64_with_line_breaks(self):
        raw = make_png()
        wrapped = base64.encodebytes(raw).decode()  # 76-char lines
        self.assertIsNotNone(normalize_png(wrapped, 'icon'))

    def test_output_is_re_encoded_without_metadata(self):
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        info.add_text('Comment', 'x' * 1000)
        raw = make_png(pnginfo=info)
        out = normalize_png(raw, 'logo')
        self.assertNotIn(b'xxxxxxxx', out)
        self.assertNotIn('Comment', decode(out).info)

    def test_preserves_palette_transparency(self):
        img = Image.new('P', (32, 32), 0)
        img.putpalette([0, 0, 0, 255, 255, 255] * 128)
        buf = io.BytesIO()
        img.save(buf, format='PNG', transparency=0)
        out = decode(normalize_png(buf.getvalue(), 'icon'))
        self.assertEqual(out.mode, 'P')
        self.assertEqual(out.info.get('transparency'), 0)

    def test_16_bit_is_converted(self):
        out = decode(normalize_png(make_png(mode='I;16'), 'logo'))
        self.assertIn(out.mode, {'RGBA', 'L', 'RGB', 'LA'})

    def test_rejects_non_png(self):
        jpeg = make_png(mode='RGB', fmt='JPEG')
        with self.assertRaisesMessage(ImageValidationError, 'must be a PNG'):
            normalize_png(jpeg, 'icon')
        with self.assertRaisesMessage(ImageValidationError, 'must be a PNG'):
            normalize_png(data_url(jpeg, 'image/jpeg'), 'icon')
        with self.assertRaisesMessage(ImageValidationError, 'must be a PNG'):
            normalize_png(b'not an image at all', 'logo')

    def test_rejects_svg_or_html_disguised_as_png_data_url(self):
        with self.assertRaisesMessage(ImageValidationError, 'must be a PNG'):
            normalize_png(data_url(b'<svg onload="alert(1)"/>'), 'logo')

    def test_rejects_truncated_png(self):
        raw = make_png(128, 128, mode='RGB')
        with self.assertRaisesMessage(ImageValidationError, 'not a valid PNG'):
            normalize_png(raw[:len(raw) // 2], 'icon')

    def test_rejects_invalid_base64(self):
        with self.assertRaisesMessage(ImageValidationError, 'not valid base64'):
            normalize_png('data:image/png;base64,@@@not-base64@@@', 'icon')

    def test_icon_must_be_square_and_not_tiny(self):
        with self.assertRaisesMessage(ImageValidationError, 'must be square (got 64x32)'):
            normalize_png(make_png(64, 32), 'icon')
        with self.assertRaisesMessage(ImageValidationError, 'at least 16x16'):
            normalize_png(make_png(8, 8), 'icon')

    def test_logo_and_privacy_may_be_rectangular(self):
        self.assertIsNotNone(normalize_png(make_png(300, 60), 'logo'))
        self.assertIsNotNone(normalize_png(make_png(1920, 1080, mode='RGB'), 'privacy'))

    def test_rejects_oversized_dimensions_without_decoding(self):
        # A highly compressible 5000x5000 PNG is small on disk but over the limit.
        raw = make_png(5000, 5000, mode='L')
        self.assertLess(len(raw), images.MAX_IMAGE_BYTES)
        with self.assertRaisesMessage(ImageValidationError, 'maximum is 4096x4096'):
            normalize_png(raw, 'privacy')

    def test_rejects_oversized_bytes(self):
        too_big = images._PNG_SIGNATURE + b'\0' * images.MAX_IMAGE_BYTES
        with self.assertRaisesMessage(ImageValidationError, 'larger than 5 MB'):
            normalize_png(too_big, 'logo')
        with self.assertRaisesMessage(ImageValidationError, 'larger than 5 MB'):
            normalize_png(base64.b64encode(too_big).decode(), 'logo')
        upload = SimpleUploadedFile('big.png', too_big, content_type='image/png')
        with self.assertRaisesMessage(ImageValidationError, 'larger than 5 MB'):
            normalize_png(upload, 'logo')


class GenerateFormImageTests(SimpleTestCase):
    def form(self, files=None, **data):
        base = {
            'platform': 'windows', 'version': '1.5.0', 'exename': 'test',
            'direction': 'both', 'installation': 'installationY', 'settings': 'settingsY',
            'theme': 'system', 'themeDorO': 'default', 'passApproveMode': 'password-click',
            'permissionsDorO': 'default', 'permissionsType': 'custom',
        }
        base.update(data)
        return GenerateForm(base, files or {})

    def upload(self, raw, name='x.png'):
        return SimpleUploadedFile(name, raw, content_type='image/png')

    def test_valid_upload_is_normalised(self):
        form = self.form({'iconfile': self.upload(make_png())})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsInstance(form.cleaned_data['iconfile'], bytes)

    def test_non_square_icon_error_is_not_double_wrapped(self):
        form = self.form({'iconfile': self.upload(make_png(64, 32))})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['iconfile'], ['Custom App Icon must be square (got 64x32).'])

    def test_logo_and_privacy_uploads_are_validated(self):
        form = self.form({
            'logofile': self.upload(b'GIF89a...'),
            'privacyfile': self.upload(make_png(mode='RGB', fmt='JPEG')),
        })
        self.assertFalse(form.is_valid())
        self.assertIn('logofile', form.errors)
        self.assertIn('privacyfile', form.errors)

    def test_loaded_config_base64_is_validated_and_reported_on_file_field(self):
        form = self.form(iconbase64=data_url(make_png(64, 32)))
        self.assertFalse(form.is_valid())
        self.assertIn('iconfile', form.errors)

        form = self.form(logobase64=data_url(make_png(300, 60)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsInstance(form.cleaned_data['logofile'], bytes)
        self.assertEqual(form.cleaned_data['logobase64'], '')

    def test_upload_wins_over_base64(self):
        form = self.form({'iconfile': self.upload(make_png(32, 32))},
                         iconbase64=data_url(make_png(64, 64)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(decode(form.cleaned_data['iconfile']).size, (32, 32))

    def test_no_images(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        for field in ('iconfile', 'logofile', 'privacyfile'):
            self.assertIsNone(form.cleaned_data[field])


class ApiImageValidationTests(SimpleTestCase):
    def test_valid_base64_fields(self):
        cleaned, errors = validate_generate_params({
            'exename': 'x',
            'iconbase64': data_url(make_png()),
            'logobase64': base64.b64encode(make_png(200, 50)).decode(),
        })
        self.assertEqual(errors, {})
        self.assertIsInstance(cleaned['iconfile'], bytes)
        self.assertIsInstance(cleaned['logofile'], bytes)
        self.assertIsNone(cleaned['privacyfile'])

    def test_saved_config_keys_are_accepted(self):
        cleaned, errors = validate_generate_params({
            'exename': 'x',
            'iconfile': data_url(make_png()),
            'privacyfile': data_url(make_png(800, 600)),
        })
        self.assertEqual(errors, {})
        self.assertIsInstance(cleaned['iconfile'], bytes)
        self.assertIsInstance(cleaned['privacyfile'], bytes)

    def test_invalid_images_are_reported(self):
        cleaned, errors = validate_generate_params({
            'exename': 'x',
            'iconbase64': data_url(make_png(64, 32)),
            'logofile': 'data:image/png;base64,AAAA',
            'privacybase64': 123,
        })
        self.assertIn('square', errors['iconbase64'])
        self.assertIn('logofile', errors)
        self.assertIn('privacybase64', errors)


class StoreAndServeTests(InChdirTestCase):
    def test_store_image_writes_png_and_returns_link_parts(self):
        myuuid = str(uuid.uuid4())
        url, u, name = _store_image({'iconfile': make_png()}, 'icon', myuuid, 'https://gen')
        self.assertEqual((url, u, name), ('https://gen', myuuid, 'icon.png'))
        with open(os.path.join('png', myuuid, 'icon.png'), 'rb') as f:
            self.assertEqual(decode(f.read()).size, (64, 64))

    def test_store_image_without_image_creates_nothing(self):
        myuuid = str(uuid.uuid4())
        self.assertEqual(_store_image({}, 'logo', myuuid, 'https://gen'), ('false',) * 3)
        self.assertFalse(os.path.exists(os.path.join('png', myuuid)))

    def test_store_image_with_bad_image_falls_back(self):
        myuuid = str(uuid.uuid4())
        result = _store_image({'logobase64': 'garbage'}, 'logo', myuuid, 'https://gen')
        self.assertEqual(result, ('false',) * 3)

    def test_get_png_serves_image_png(self):
        myuuid = str(uuid.uuid4())
        _store_image({'iconfile': make_png()}, 'icon', myuuid, 'https://gen')
        request = RequestFactory().get('/get_png', {'filename': 'icon.png', 'uuid': myuuid})
        response = get_png(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertTrue(response.content.startswith(images._PNG_SIGNATURE))

    def test_get_png_missing_file_is_404_and_bad_params_403(self):
        from django.http import Http404
        request = RequestFactory().get('/get_png', {'filename': 'icon.png', 'uuid': str(uuid.uuid4())})
        with self.assertRaises(Http404):
            get_png(request)
        for params in ({'filename': '../x', 'uuid': str(uuid.uuid4())}, {'filename': 'icon.png'}, {}):
            self.assertEqual(get_png(RequestFactory().get('/get_png', params)).status_code, 403)
