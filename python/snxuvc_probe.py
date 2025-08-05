#!/usr/bin/env python3
# Probe UVC Extension Unit mapping for Sonix SPI read (Windows, PyUSB + libusb-package)
import sys, binascii
import usb.core, usb.util
from usb.backend import libusb1
from array import array

# ---- backend wiring ----
try:
    import libusb_package
    BACKEND = libusb1.get_backend(find_library=libusb_package.find_library)
except Exception:
    BACKEND = libusb1.get_backend()
if BACKEND is None:
    sys.exit("libusb backend not found. pip install libusb-package")

VID, PID, VC_IF = 0x0C45, 0x6366, 0
XU_CANDIDATES = list(range(1, 9))
CS_PAIRS = [(0x21,0x22), (0x23,0x24), (0x25,0x26), (0x27,0x28)]
TESTS = [(0x000000, 64), (0x000100, 64)]

UVC_SET_CUR, UVC_GET_CUR = 0x01, 0x81

def ensure_open(dev):
    # Open the libusb device handle via PyUSB’s manager
    try:
        if getattr(dev._ctx, "handle", None) is None:
            dev._ctx.managed_open()
    except Exception:
        # fall back to backend open if needed
        dev._ctx.handle = dev._ctx.backend.open_device(dev)

def ctrl(dev, bm, br, wValue, wIndex, data_or_len, timeout=2000):
    ensure_open(dev)
    if (bm & 0x80):  # IN -> pass int length
        return dev._ctx.backend.ctrl_transfer(dev._ctx.handle, bm, br, wValue, wIndex, int(data_or_len), timeout)
    else:            # OUT -> pass a real buffer
        if isinstance(data_or_len, (bytes, bytearray)):
            data_or_len = array('B', data_or_len)
        return dev._ctx.backend.ctrl_transfer(dev._ctx.handle, bm, br, wValue, wIndex, data_or_len, timeout)

def uvc_xu_set(dev, xu, cs, payload):
    wValue = (cs << 8); wIndex = (xu << 8) | VC_IF
    return ctrl(dev, 0x21, UVC_SET_CUR, wValue, wIndex, payload)

def uvc_xu_get(dev, xu, cs, length):
    wValue = (cs << 8); wIndex = (xu << 8) | VC_IF
    data = ctrl(dev, 0xA1, UVC_GET_CUR, wValue, wIndex, length)
    return bytes(data)

def main():
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=BACKEND)
    if not dev:
        sys.exit("Device 0C45:6366 not found.")
    print("Probing…")
    for xu in XU_CANDIDATES:
        for cs_set, cs_get in CS_PAIRS:
            ok = True
            for addr, ln in TESTS:
                try:
                    payload = bytes([(addr>>16)&0xFF,(addr>>8)&0xFF,addr&0xFF,(ln>>8)&0xFF,ln&0xFF])
                    uvc_xu_set(dev, xu, cs_set, payload)
                    data = uvc_xu_get(dev, xu, cs_get, ln)
                    if len(data) != ln:
                        ok = False; break
                except usb.core.USBError:
                    ok = False; break
            if ok:
                print(f"FOUND: XU={xu}  CS_SET=0x{cs_set:02X}  CS_GET=0x{cs_get:02X}")
                uvc_xu_set(dev, xu, cs_set, bytes([0,0,0,0,64]))
                data = uvc_xu_get(dev, xu, cs_get, 64)
                print("first64:", binascii.hexlify(data).decode())
                return
    print("No match yet. Replug and rerun (or I’ll ship the KS tool).")

if __name__ == "__main__":
    main()
