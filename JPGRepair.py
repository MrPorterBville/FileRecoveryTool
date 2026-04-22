import sys
import warnings
import numpy as np
from io import BytesIO

from PyQt6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel,
    QFileDialog, QVBoxLayout, QCheckBox
)
from PyQt6.QtGui import QPixmap, QImage

from PIL import Image, ImageFile

# --- Pillow config ---
warnings.filterwarnings("ignore", category=UserWarning)
ImageFile.LOAD_TRUNCATED_IMAGES = True


# =========================
# JPEG REPAIR ENGINE
# =========================
class JPEGRepair:
    def __init__(self, filepath):
        self.filepath = filepath

    def read_file(self):
        with open(self.filepath, 'rb') as f:
            return f.read()

    def is_jpeg(self, data):
        return data.startswith(b'\xFF\xD8')

    def fix_markers(self, data):
        if not data.startswith(b'\xFF\xD8'):
            data = b'\xFF\xD8' + data

        if not data.endswith(b'\xFF\xD9'):
            data = data + b'\xFF\xD9'

        return data

    def strip_to_jpeg(self, data):
        start = data.find(b'\xFF\xD8')
        end = data.rfind(b'\xFF\xD9')

        if start != -1 and end != -1 and end > start:
            return data[start:end+2]

        return data

    def try_decode(self, data):
        try:
            img = Image.open(BytesIO(data))
            img = img.convert("RGB")
            return img
        except:
            return None

    def progressive_recovery(self, data):
        print("Trying progressive recovery...")

        for i in range(len(data), int(len(data) * 0.2), -256):
            chunk = data[:i]
            try:
                img = Image.open(BytesIO(chunk))
                img = img.convert("RGB")
                print(f"Recovered at {i} bytes")
                return img
            except:
                continue

        return None

    # =========================
    # FORCE RENDER MODE
    # =========================
    def force_render(self, data):
        print("Force rendering raw data...")

        if len(data) < 100:
            return None

        # Remove obvious JPEG markers
        cleaned = data.replace(b'\xFF\xD8', b'').replace(b'\xFF\xD9', b'')

        arr = np.frombuffer(cleaned, dtype=np.uint8)

        usable_length = (len(arr) // 3) * 3
        arr = arr[:usable_length]

        if usable_length == 0:
            return None

        possible_widths = [256, 320, 512, 640, 800, 1024]

        for width in possible_widths:
            height = usable_length // (3 * width)

            if height <= 0:
                continue

            try:
                reshaped = arr[:width * height * 3].reshape((height, width, 3))
                return Image.fromarray(reshaped, 'RGB')
            except:
                continue

        # fallback square
        size = int((usable_length // 3) ** 0.5)
        try:
            reshaped = arr[:size * size * 3].reshape((size, size, 3))
            return Image.fromarray(reshaped, 'RGB')
        except:
            return None

    def repair(self):
        data = self.read_file()

        # If not JPEG → go straight to force render
        if not self.is_jpeg(data):
            print("Not a valid JPEG → using force render")
            return self.force_render(data)

        data = self.strip_to_jpeg(data)
        data = self.fix_markers(data)

        img = self.try_decode(data)
        if img:
            print("Standard decode succeeded")
            return img

        img = self.progressive_recovery(data)
        if img:
            return img

        print("Falling back to force render...")
        return self.force_render(data)


# =========================
# GUI APPLICATION
# =========================
class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JPEG Repair Tool")

        self.label = QLabel("Select a JPG file")
        self.image_label = QLabel()

        self.force_checkbox = QCheckBox("Force Render Mode (always use raw)")
        
        btn = QPushButton("Open JPG")
        btn.clicked.connect(self.open_file)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(btn)
        layout.addWidget(self.force_checkbox)
        layout.addWidget(self.image_label)

        self.setLayout(layout)

    def open_file(self):
        file, _ = QFileDialog.getOpenFileName(
            self,
            "Open JPG",
            "",
            "JPEG Files (*.jpg *.jpeg);;All Files (*)"
        )

        if not file:
            return

        self.label.setText(file)

        repair = JPEGRepair(file)
        data = repair.read_file()

        # Force mode override
        if self.force_checkbox.isChecked():
            img = repair.force_render(data)
        else:
            img = repair.repair()

        if img:
            self.display_image(img)
        else:
            self.label.setText("Could not render image.")

    def display_image(self, pil_img):
        pil_img = pil_img.convert("RGB")
        data = pil_img.tobytes("raw", "RGB")

        qimg = QImage(
            data,
            pil_img.width,
            pil_img.height,
            QImage.Format.Format_RGB888
        )

        pixmap = QPixmap.fromImage(qimg)
        self.image_label.setPixmap(pixmap)


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = App()
    window.resize(900, 700)
    window.show()
    sys.exit(app.exec())