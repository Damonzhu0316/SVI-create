import io
import unittest
from unittest.mock import patch

from PIL import Image

import front_view as api


class StaticImageApiTests(unittest.TestCase):
    def test_heading_parameters_are_numeric(self):
        self.assertEqual(api.parameters("PANO", 271.5), {
            "qt": "pr3d", "panoid": "PANO", "heading": "271.500000", "pitch": 0,
            "fovy": 35, "width": 1024, "height": 512, "quality": 90,
        })

    def test_image_must_be_valid_dimensions_and_nonuniform(self):
        image = Image.new("RGB", (1024, 512), "black")
        image.paste("white", (512, 0, 1024, 512))
        stream = io.BytesIO()
        image.save(stream, "JPEG")
        self.assertTrue(api.image_bytes(stream.getvalue()))
        blank = io.BytesIO()
        Image.new("RGB", (1024, 512), "white").save(blank, "JPEG")
        with self.assertRaises(api.Failure):
            api.image_bytes(blank.getvalue())

    def test_static_image_request_uses_requested_heading_once(self):
        image = Image.new("RGB", (1024, 512), "black")
        image.paste("white", (512, 0, 1024, 512))
        stream = io.BytesIO()
        image.save(stream, "JPEG")
        with patch.object(api, "get", return_value=("image/jpeg", stream.getvalue())) as request:
            api.download("PANO", 123.25)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], api.IMAGE_API)
        self.assertEqual(request.call_args.args[1]["qt"], "pr3d")
        self.assertEqual(request.call_args.args[1]["heading"], "123.250000")

    def test_non_image_response_is_rejected(self):
        with patch.object(api, "get", return_value=("application/json", b'{"status":403}')):
            with self.assertRaises(api.Failure) as raised:
                api.download("PANO", 90)
        self.assertEqual(raised.exception.status, "IMAGE_DOWNLOAD_FAILED")

    def test_date_is_not_guessed_from_pano_id(self):
        self.assertEqual(api.capture_date({"ID": "201701012345"}), "NA")
        self.assertEqual(api.capture_date({"Date": "20260230"}), "NA")


if __name__ == "__main__":
    unittest.main()
