import os
import asyncio
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    from bleak import BleakScanner, BleakClient
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False


class PortablePrinterDriver:
    """便携式热敏打印机驱动（适配 inniFun P0 等蓝牙/USB小票机 + 隐藏元数据水印）"""

    def __init__(self, cfg: dict):
        self.mode = cfg.get("mode", "auto")
        self.bt_keyword = cfg.get("bluetooth_name_keyword", "inniFun").lower()
        self.serial_port = cfg.get("serial_port", "").strip()
        self.baudrate = cfg.get("baudrate", 9600)
        self.output_dir = "printed_tickets_backup"
        os.makedirs(self.output_dir, exist_ok=True)

    def _render_ticket_image(self, ticket: dict) -> str:
        width, height = 384, 520
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)

        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
        font_large = font_medium = font_small = ImageFont.load_default()
        for fp in font_paths:
            if os.path.exists(fp):
                font_large = ImageFont.truetype(fp, 26)
                font_medium = ImageFont.truetype(fp, 20)
                font_small = ImageFont.truetype(fp, 16)
                break

        draw.rectangle([10, 10, width - 10, height - 10], outline="black", width=3)
        draw.text((35, 28), "G1 校园数字共创凭证", fill="black", font=font_large)
        draw.line([(20, 70), (width - 20, 70)], fill="black", width=2)

        draw.text((25, 88), f"作品编号：{ticket['ticket_no']}", fill="black", font=font_small)
        draw.text((25, 118), f"生成时间：{ticket['issued_at']}", fill="black", font=font_small)
        draw.text((25, 148), f"光影姿态：[ Pose - {ticket['action_id']} ]", fill="black", font=font_medium)

        draw.line([(20, 185), (width - 20, 185)], fill="black", width=1)
        draw.text((25, 200), "★ 你的专属记忆点：", fill="black", font=font_small)

        mem = f"「{ticket['memory']}」"
        line1 = mem[:13]
        line2 = mem[13:]
        draw.text((25, 232), line1, fill="black", font=font_medium)
        if line2:
            draw.text((25, 262), line2, fill="black", font=font_medium)

        draw.line([(20, 305), (width - 20, 305)], fill="black", width=2)
        draw.text((25, 325), "凭下方核销码兑换 3D 打印礼品：", fill="black", font=font_small)
        draw.rectangle([45, 360, width - 45, 435], outline="black", width=3)
        draw.text((95, 378), f"核销码: {ticket['code']}", fill="black", font=font_large)

        draw.text((45, 458), "—— 时迁 · 具身智能实验室 ——", fill="black", font=font_small)

        # 在 PNG 图片内部写入隐藏元数据水印（表面完全不可见）
        meta = PngImagePlugin.PngInfo()
        meta.add_text("AuthorSignature", "千逐")
        meta.add_text("SystemCode", "SHIQIAN-G1")

        safe_no = ticket["ticket_no"].replace("#", "")
        img_path = os.path.join(self.output_dir, f"ticket_{safe_no}_{ticket['code']}.png")
        img.save(img_path, pnginfo=meta)
        return img_path

    def _image_to_escpos_raster(self, img_path: str) -> bytes:
        img = Image.open(img_path).convert("L")
        bw = img.point(lambda x: 0 if x < 180 else 255, "1")
        w, h = bw.size
        width_bytes = (w + 7) // 8

        header = b"\x1d\x76\x30\x00" + bytes([
            width_bytes & 0xFF, (width_bytes >> 8) & 0xFF,
            h & 0xFF, (h >> 8) & 0xFF
        ])

        pixels = bw.load()
        data = bytearray()
        for y in range(h):
            for bx in range(width_bytes):
                byte_val = 0
                for bit in range(8):
                    x = bx * 8 + bit
                    if x < w and pixels[x, y] == 0:
                        byte_val |= (1 << (7 - bit))
                data.append(byte_val)

        return b"\x1b\x40" + header + bytes(data) + b"\x0a\x0a\x0a"

    def _print_via_serial(self, escpos_bytes: bytes) -> bool:
        if not SERIAL_AVAILABLE:
            return False

        target_port = self.serial_port
        if not target_port:
            ports = serial.tools.list_ports.comports()
            for p in ports:
                desc = f"{p.description} {p.device}".lower()
                if self.bt_keyword in desc or "bluetooth" in desc or "蓝牙" in desc:
                    target_port = p.device
                    break

        if not target_port:
            return False

        try:
            with serial.Serial(target_port, baudrate=self.baudrate, timeout=3) as ser:
                ser.write(escpos_bytes)
                ser.flush()
            print("✅ [便携打印机] 蓝牙无线打印完成！")
            return True
        except Exception:
            return False

    async def _print_via_ble(self, escpos_bytes: bytes) -> bool:
        if not BLEAK_AVAILABLE:
            return False
        try:
            devices = await BleakScanner.discover(timeout=3.0)
            target_dev = next((d for d in devices if d.name and self.bt_keyword in d.name.lower()), None)
            if not target_dev:
                return False

            async with BleakClient(target_dev.address, timeout=5.0) as client:
                write_char = None
                for service in client.services:
                    for char in service.characteristics:
                        if "write-without-response" in char.properties or "write" in char.properties:
                            write_char = char.uuid
                            break
                    if write_char:
                        break

                if not write_char:
                    return False

                for i in range(0, len(escpos_bytes), 100):
                    await client.write_gatt_char(write_char, escpos_bytes[i:i + 100], response=False)
                    await asyncio.sleep(0.02)

            print("✅ [便携打印机] 蓝牙无线打印完成！")
            return True
        except Exception:
            return False

    def print_ticket(self, ticket: dict) -> str:
        img_path = self._render_ticket_image(ticket)
        print(f"🖼️ [小票生成] 已生成电子纪念小票图片: {img_path}")

        if self.mode == "image_only":
            return img_path

        escpos_bytes = self._image_to_escpos_raster(img_path)

        if self._print_via_serial(escpos_bytes):
            return img_path

        try:
            if asyncio.run(self._print_via_ble(escpos_bytes)):
                return img_path
        except Exception:
            pass

        print("💡 [打印机提示] 当前未连接实体蓝牙打印机，已自动保存电子小票图片（可用手机 inniFun App 选图打印）！")
        return img_path
