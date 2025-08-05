#!/usr/bin/env python3
# snxuvc_probe2.py — enumerate UVC XUs + find the SPI read control pair (Windows, PyUSB+libusb-package)
import sys, struct, binascii
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

# UVC request codes
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86

# Descriptor types
DT_CONFIG = 0x02
CS_INTERFACE = 0x24
UVC_VC_EXTENSION_UNIT = 0x06  # bDescriptorSubtype

def ensure_open(dev):
    # open the libusb handle via PyUSB’s manager (no interface claim)
    try:
        if getattr(dev._ctx, "handle", None) is None:
            dev._ctx.managed_open()
    except Exception:
        dev._ctx.handle = dev._ctx.backend.open_device(dev)

def ep0_in(dev, bm, br, wValue, wIndex, length, timeout=2000):
    # IN needs a real buffer (array('B')) for backend.ctrl_transfer
    ensure_open(dev)
    buf = array('B', b'\x00' * int(length))
    data = dev._ctx.backend.ctrl_transfer(dev._ctx.handle, bm, br, wValue, wIndex, buf, timeout)
    return bytes(data)

def ep0_out(dev, bm, br, wValue, wIndex, payload, timeout=2000):
    ensure_open(dev)
    if isinstance(payload, (bytes, bytearray)):
        payload = array('B', payload)
    return dev._ctx.backend.ctrl_transfer(dev._ctx.handle, bm, br, wValue, wIndex, payload, timeout)

def uvc_req(dev, xu, cs, bRequest, payload_len=0, payload=None):
    # wValue=(cs<<8), wIndex=(xu<<8)|VC_IF
    wValue = (cs << 8)
    wIndex = (xu << 8) | VC_IF
    if bRequest == UVC_SET_CUR:
        return ep0_out(dev, 0x21, bRequest, wValue, wIndex, payload or b"", 2000)
    elif bRequest in (UVC_GET_CUR, UVC_GET_LEN, UVC_GET_INFO):
        return ep0_in(dev, 0xA1, bRequest, wValue, wIndex, payload_len or 64, 2000)
    else:
        raise ValueError("unsupported request")

def get_full_config(dev):
    # Read wTotalLength, then full config
    hdr = ep0_in(dev, 0x80, 0x06, (DT_CONFIG << 8) | 0, 0, 9, 1000)
    total = struct.unpack_from("<H", hdr, 2)[0]
    return ep0_in(dev, 0x80, 0x06, (DT_CONFIG << 8) | 0, 0, total, 1000)

def parse_xu_unit_ids(cfg_bytes):
    offs, xus = 0, []
    L = len(cfg_bytes)
    while offs + 2 <= L:
        bLength = cfg_bytes[offs]
        if bLength == 0 or offs + bLength > L:
            break
        bDescriptorType = cfg_bytes[offs+1]
        if bDescriptorType == CS_INTERFACE:
            bDescriptorSubType = cfg_bytes[offs+2]
            if bDescriptorSubType == UVC_VC_EXTENSION_UNIT:
                # bUnitID is last byte of the XU descriptor
                bUnitID = cfg_bytes[offs + bLength - 1]
                xus.append(bUnitID)
        offs += bLength
    return sorted(set(xus))

def main():
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=BACKEND)
    if not dev:
        sys.exit("Device 0C45:6366 not found.")
    print("Reading descriptors…")
    cfg = get_full_config(dev)
    xu_ids = parse_xu_unit_ids(cfg)
    if not xu_ids:
        print("No XU descriptors parsed; brute-forcing XU IDs 1..7")
        xu_ids = list(range(1, 8))
    else:
        print("XU units found:", xu_ids)

    candidates = []
    for xu in xu_ids:
        for cs in range(1, 41):  # try selectors 1..40
            # GET_LEN (2B) → control length if supported
            try:
                gl = uvc_req(dev, xu, cs, UVC_GET_LEN, 2)
                if len(gl) != 2:
                    continue
                ln = struct.unpack("<H", gl)[0]
            except usb.core.USBError:
                continue
            # GET_INFO (1B) → bit0 = GET supported, bit1 = SET supported
            try:
                gi = uvc_req(dev, xu, cs, UVC_GET_INFO, 1)
                info = gi[0] if gi else 0
            except usb.core.USBError:
                info = 0
            candidates.append((xu, cs, ln, info))

    if not candidates:
        print("No XU controls responded. Replug and retry.")
        return

    print("Found controls (interesting sizes):")
    for xu, cs, ln, info in candidates:
        if ln in (0,5,32,64,128,256,512) and (info & 0x03):
            print(f"  XU {xu}  CS 0x{cs:02X}  len={ln}  info=0x{info:02X}")

    # Heuristics:
    #   SET candidate: len==5 (addr24+len16) and SET supported
    #   GET candidate: GET supported and len==0 (variable) or >=64
    set_cands = [(xu, cs) for (xu, cs, ln, info) in candidates if ln == 5 and (info & 0x02)]
    get_cands = [(xu, cs) for (xu, cs, ln, info) in candidates if (info & 0x01) and (ln == 0 or ln >= 64)]

    if not set_cands or not get_cands:
        print("No obvious SET/GET pair. If you paste the list above, I’ll pick likely selectors manually.")
        return

    # Try pairs on same XU first, then cross-XU
    pairs = [(xu, css, csg) for (xu, css) in set_cands for (xu2, csg) in get_cands if xu2 == xu] \
         or [(xu1, css, csg) for (xu1, css) in set_cands for (xu2, csg) in get_cands]

    for (xu, cs_set, cs_get) in pairs:
        try:
            payload = bytes([0,0,0,0,64])  # addr=0, len=64
            uvc_req(dev, xu, cs_set, UVC_SET_CUR, len(payload), payload)
            data = uvc_req(dev, xu, cs_get, UVC_GET_CUR, 64)
            if len(data) == 64:
                print(f"\nFOUND: XU={xu}  CS_SET=0x{cs_set:02X}  CS_GET=0x{cs_get:02X}")
                print("first64:", binascii.hexlify(data).decode())
                return
        except usb.core.USBError:
            continue

    print("Scanned candidates; no working pair verified. Paste the 'Found controls' list or say 'ship KS tool'.")

if __name__ == "__main__":
    main()
