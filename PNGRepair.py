import sys
import struct
import zlib
import numpy as np

from PyQt6.QtWidgets import QApplication, QWidget, QPushButton, QLabel, QFileDialog, QVBoxLayout
from PyQt6.QtGui import QPixmap, QImage
from PIL import Image
from io import BytesIO

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class PNGRepair:
    def __init__(self, filepath):
        self.filepath = filepath
        self.chunks = []
        self.width = None
        self.height = None
        self.bit_depth = None
        self.color_type = None

    def read_file(self):
        with open(self.filepath, 'rb') as f:
            self.data = f.read()

    def fix_signature(self):
        if not self.data.startswith(PNG_SIGNATURE):
            print("Fixing PNG signature...")
            self.data = PNG_SIGNATURE + self.data[8:]

    def parse_chunks(self):
        offset = 8
        data = self.data

        while offset < len(data):
            try:
                length = struct.unpack(">I", data[offset:offset+4])[0]
                chunk_type = data[offset+4:offset+8]
                chunk_data = data[offset+8:offset+8+length]
                # crc = data[offset+8+length:offset+12+length]  # ignored

                self.chunks.append((chunk_type, chunk_data))

                if chunk_type == b'IHDR':
                    self.width, self.height, self.bit_depth, self.color_type = struct.unpack(">IIBB", chunk_data[:10])

                offset += 12 + length
            except:
                break

    def reconstruct_image(self):
        idat_data = b''

        for chunk_type, chunk_data in self.chunks:
            if chunk_type == b'IDAT':
                idat_data += chunk_data

        try:
            decompressed = zlib.decompress(idat_data)
            return self.build_image(decompressed)
        except:
            print("Partial decompression attempt...")
            return self.partial_decompress(idat_data)

    def partial_decompress(self, data):
        d = zlib.decompressobj()
        try:
            decompressed = d.decompress(data)
            return self.build_image(decompressed)
        except:
            print("Decompression failed completely.")
            return None

    def build_image(self, raw):
        if self.width is None or self.height is None:
            return None

        try:
            # Assume RGB for now (color_type 2)
            bytes_per_pixel = 3
            stride = self.width * bytes_per_pixel + 1  # +1 for filter byte

            rows = []
            for y in range(self.height):
                start = y * stride
                if start + stride > len(raw):
                    break

                row = raw[start+1:start+stride]  # skip filter byte
                rows.append(row)

            img_data = b''.join(rows)

            img = Image.frombytes('RGB', (self.width, len(rows)), img_data)
            return img
        except:
            return None


class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PNG Repair Tool")

        self.label = QLabel("Select a PNG file")
        self.image_label = QLabel()

        btn = QPushButton("Open PNG")
        btn.clicked.connect(self.open_file)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(btn)
        layout.addWidget(self.image_label)

        self.setLayout(layout)

    def open_file(self):
        file, _ = QFileDialog.getOpenFileName(self, "Open PNG", "", "PNG Files (*.png);;All Files (*)")

        if file:
            self.label.setText(file)

            # Try normal load first
            try:
                img = Image.open(file)
                img = img.convert("RGB")
                self.display_image(img)
                return
            except:
                pass

            # Repair path
            repair = PNGRepair(file)
            repair.read_file()
            repair.fix_signature()
            repair.parse_chunks()

            img = repair.reconstruct_image()

            if img:
                self.display_image(img)
            else:
                self.label.setText("Could not repair image.")

    def display_image(self, pil_img):
        data = pil_img.tobytes("raw", "RGB")
        qimg = QImage(data, pil_img.width, pil_img.height, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self.image_label.setPixmap(pixmap)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = App()
    window.resize(800, 600)
    window.show()
    sys.exit(app.exec())