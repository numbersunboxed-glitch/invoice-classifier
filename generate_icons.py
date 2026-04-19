"""
Generate PWA icons as simple SVG-based PNGs.
Run once: python generate_icons.py
Requires: pip install Pillow
"""
import os, math

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Installing Pillow...")
    os.system("pip install Pillow --break-system-packages -q")
    from PIL import Image, ImageDraw, ImageFont

os.makedirs("static/icons", exist_ok=True)

def make_icon(size, path):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded rect
    radius = size // 5
    bg_color = (80, 70, 228)  # accent purple
    draw.rounded_rectangle([0, 0, size, size], radius=radius, fill=bg_color)

    # Draw a simple "invoice" shape: white rectangle with lines
    pad = size // 6
    doc_x1, doc_y1 = pad, pad
    doc_x2, doc_y2 = size - pad, size - pad
    doc_w = doc_x2 - doc_x1
    doc_h = doc_y2 - doc_y1

    # White doc background
    draw.rounded_rectangle([doc_x1, doc_y1, doc_x2, doc_y2], radius=size//20, fill=(255, 255, 255, 230))

    # Lines representing text
    line_color = (80, 70, 228, 180)
    line_h = max(2, size // 32)
    gap = doc_h // 6
    for i in range(1, 5):
        y = doc_y1 + gap * i
        x_end = doc_x2 - pad // 2 if i == 4 else doc_x2 - pad // 4
        draw.rounded_rectangle([doc_x1 + pad//2, y, x_end, y + line_h], radius=1, fill=line_color)

    # Small checkmark / spark in top-right
    spark_size = size // 6
    sx = doc_x2 - spark_size // 2
    sy = doc_y1 - spark_size // 2
    draw.ellipse([sx, sy, sx + spark_size, sy + spark_size], fill=(61, 214, 140))

    img.save(path, "PNG")
    print(f"  Created {path} ({size}x{size})")


def make_screenshot(w, h, path, label="Invoice Classifier"):
    img = Image.new("RGB", (w, h), (12, 12, 15))
    draw = ImageDraw.Draw(img)

    # Simple header bar
    draw.rectangle([0, 0, w, 60], fill=(20, 20, 23))

    # Logo text area
    draw.rounded_rectangle([20, 15, 180, 45], radius=8, fill=(30, 30, 35))

    # Fake cards
    card_color = (20, 20, 23)
    for i in range(3):
        y = 80 + i * 120
        draw.rounded_rectangle([20, y, w - 20, y + 100], radius=10, fill=card_color)
        # Fake badge
        draw.rounded_rectangle([30, y + 15, 100, y + 35], radius=6, fill=(80, 70, 228, 180))
        # Fake lines
        draw.rounded_rectangle([30, y + 50, w - 60, y + 58], radius=3, fill=(40, 40, 45))
        draw.rounded_rectangle([30, y + 68, w - 100, y + 76], radius=3, fill=(35, 35, 40))

    img.save(path, "PNG")
    print(f"  Created {path} ({w}x{h})")


print("Generating PWA icons...")
make_icon(192, "static/icons/icon-192.png")
make_icon(512, "static/icons/icon-512.png")

print("Generating screenshots...")
make_screenshot(1280, 720, "static/icons/screenshot-wide.png")
make_screenshot(390, 844, "static/icons/screenshot-narrow.png")

print("Done! All PWA assets generated.")
