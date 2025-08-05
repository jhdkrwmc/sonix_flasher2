#!/usr/bin/env python3
"""
snxuvc_dump.py — Windows-friendly UVC XU dumper for Sonix SN9C292x

Requires:
  - Python 3.8+ (works on 3.12)
  - PyUSB:    pip install pyusb
  - libusb-1.0 runtime DLL on Windows (see notes below)
  - WinUSB driver bound to the camera's VideoControl interface (use Zadig)

What it does
- Enumerates Sonix UVC cams (default VID:PID 0x0C45:0x6366), or any you specify.
- Issues UVC Extension Unit (XU) GET_CUR/SET_CUR on the VideoControl interface.
- Generic "xu-get"/"xu-set" helpers.
- "sf-read": chunked reads of external SPI flash via the Sonix XU used by Kurokesu's tool.

Defaults (override with CLI args):
  --vc-if 0     VideoControl interface number
  --xu 3        XU entity ID
  --cs-set 0x23 Control selector for SET (payload = [addr24_be(3), len16_be(2)])
  --cs-get 0x24 Control selector for GET (returns <len> bytes)
"""

import argparse, binascii, os, sys, time
import usb.core, usb.util

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81

def find_device(vid=0x0C45, pid=0x6366):
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise SystemExit(f"No device {vid:04x}:{pid:04x} found. Use --vid/--pid or plug the cam.")
    return dev

def detach_kernel_if_needed(dev, intf):
    # Mostly relevant on Linux. On Windows, just ignore errors here.
    try:
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
    except Exception:
        pass

def ctrl(dev, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=3000):
    return dev.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=timeout)

def uvc_xu_set(dev, vc_if, xu_id, cs, payload: bytes):
    # wValue: (CS << 8) | 0, wIndex: (EntityID << 8) | InterfaceNumber
    wValue = (cs << 8) | 0
    wIndex = (xu_id << 8) | vc_if
    return ctrl(dev, 0x21, UVC_SET_CUR, wValue, wIndex, payload)

def uvc_xu_get(dev, vc_if, xu_id, cs, length):
    wValue = (cs << 8) | 0
    wIndex = (xu_id << 8) | vc_if
    data = ctrl(dev, 0xA1, UVC_GET_CUR, wValue, wIndex, length)
    return bytes(data)

def parse_hex_bytes(lst):
    out = bytearray()
    for tok in lst:
        tok = tok.strip()
        if tok.lower().startswith("0x"):
            out.append(int(tok, 16) & 0xFF)
        else:
            # accept hex without 0x or decimal; bias to hex
            base = 16 if all(c in "0123456789abcdefABCDEF" for c in tok) else 10
            out.append(int(tok, base) & 0xFF)
    return bytes(out)

def cmd_scan(args):
    # List devices for given VID (default 0x0C45)
    found = list(usb.core.find(find_all=True, idVendor=args.vid))
    if not found:
        print("No USB devices with that VID.")
        return
    for dev in found:
        if args.pid != -1 and dev.idProduct != args.pid:
            continue
        print(f"{dev.idVendor:04x}:{dev.idProduct:04x}  Bus={getattr(dev,'bus','?')} Addr={getattr(dev,'address','?')}")
        try:
            for cfg in dev:
                for intf in cfg:
                    print(f"  cfg {cfg.bConfigurationValue}  if {intf.bInterfaceNumber}  cls={intf.bInterfaceClass:02x} sub={intf.bInterfaceSubClass:02x}")
        except Exception as e:
            print("  [warn] can't enumerate interfaces:", e)

def cmd_xu_get(args):
    dev = find_device(args.vid, args.pid)
    detach_kernel_if_needed(dev, args.vc_if)
    data = uvc_xu_get(dev, args.vc_if, args.xu, args.cs, args.len)
    print(binascii.hexlify(data).decode())

def cmd_xu_set(args):
    dev = find_device(args.vid, args.pid)
    detach_kernel_if_needed(dev, args.vc_if)
    payload = parse_hex_bytes(args.data)
    uvc_xu_set(dev, args.vc_if, args.xu, args.cs, payload)
    print(f"SET done ({len(payload)} bytes)")

def cmd_sf_read(args):
    dev = find_device(args.vid, args.pid)
    detach_kernel_if_needed(dev, args.vc_if)

    addr = args.addr
    total = args.length
    chunk = args.chunk
    cs_set = args.cs_set
    cs_get = args.cs_get

    # sanity
    if chunk <= 0 or chunk > 1023:
        # control xfers max ~1KB reliably. Keep <= 512 for safety.
        chunk = 512
    if total <= 0:
        raise SystemExit("length must be > 0")

    with open(args.out, "wb") as f:
        remain = total
        cur = addr
        while remain > 0:
            this = min(remain, chunk)
            payload = bytes([(cur >> 16) & 0xFF, (cur >> 8) & 0xFF, cur & 0xFF,
                             (this >> 8) & 0xFF, this & 0xFF])
            # Minor retry logic
            for attempt in range(2):
                try:
                    uvc_xu_set(dev, args.vc_if, args.xu, cs_set, payload)
                    data = uvc_xu_get(dev, args.vc_if, args.xu, cs_get, this)
                    break
                except usb.core.USBError as e:
                    if attempt == 0:
                        time.sleep(0.05)
                        continue
                    raise
            if len(data) != this:
                raise SystemExit(f"Short read at 0x{cur:06X}: got {len(data)} expected {this}")
            f.write(data)
            cur += this
            remain -= this
            if args.progress:
                done = total - remain
                pct = (done * 100.0) / total
                print(f"\rRead {done}/{total} bytes ({pct:5.1f}%)", end="", flush=True)
        if args.progress:
            print()
    print(f"Wrote: {args.out}")

    if args.verify:
        # Re-read and check SHA-256
        import hashlib
        h = hashlib.sha256()
        with open(args.out, "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
        print("SHA-256:", h.hexdigest())

def main():
    ap = argparse.ArgumentParser(description="Sonix UVC XU dumper (Windows-friendly via PyUSB)")
    ap.add_argument("--vid", type=lambda x:int(x,0), default=0x0C45, help="USB VID (default 0x0C45)")
    ap.add_argument("--pid", type=lambda x:int(x,0), default=0x6366, help="USB PID (default 0x6366; -1 to ignore)")
    ap.add_argument("--vc-if", type=int, default=0, help="VideoControl interface number (default 0)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="List candidate devices/interfaces")
    sc.set_defaults(func=cmd_scan)

    gx = sub.add_parser("xu-get", help="Raw UVC XU GET")
    gx.add_argument("--xu", type=int, required=True, help="XU entity ID")
    gx.add_argument("--cs", type=lambda x:int(x,0), required=True, help="control selector (hex)")
    gx.add_argument("--len", type=int, required=True, help="bytes to read")
    gx.set_defaults(func=cmd_xu_get)

    sx = sub.add_parser("xu-set", help="Raw UVC XU SET")
    sx.add_argument("--xu", type=int, required=True, help="XU entity ID")
    sx.add_argument("--cs", type=lambda x:int(x,0), required=True, help="control selector (hex)")
    sx.add_argument("--data", nargs="+", required=True, help="payload bytes (hex, e.g. 0A FF 01)")
    sx.set_defaults(func=cmd_xu_set)

    sf = sub.add_parser("sf-read", help="Read external SPI flash via XU (Sonix style)")
    sf.add_argument("--xu", type=int, default=3, help="XU entity ID (default 3)")
    sf.add_argument("--cs-set", type=lambda x:int(x,0), default=0x23, help="CS for SET (addr/len) (default 0x23)")
    sf.add_argument("--cs-get", type=lambda x:int(x,0), default=0x24, help="CS for GET (data) (default 0x24)")
    sf.add_argument("--addr", type=lambda x:int(x,0), required=True, help="start address (e.g. 0x0)")
    sf.add_argument("--length", type=lambda x:int(x,0), required=True, help="total length to read (e.g. 0x20000)")
    sf.add_argument("--chunk", type=int, default=512, help="chunk size (<=1023; default 512)")
    sf.add_argument("--out", type=str, required=True, help="output file path")
    sf.add_argument("--progress", action="store_true", help="print progress")
    sf.add_argument("--verify", action="store_true", help="print SHA-256 after read")
    sf.set_defaults(func=cmd_sf_read)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
