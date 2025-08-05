# Create a Windows-targeted Python CLI (PyUSB-based) to talk UVC Extension Units and dump Sonix SPI flash
# Note: This requires libusb and PyUSB on Windows. It provides generic XU GET/SET plus a convenience 'sf-read' that matches Sonix/Kurokesu workflow.
# The user can tweak xu_id and control selector IDs in a small config JSON, or pass them via CLI.
import textwrap, json, os, hashlib, struct, sys, time
from pathlib import Path

tool_code = r"""
#!/usr/bin/env python3
"""
tool_code += r'''
"""
snxuvc_dump.py — Windows-friendly UVC XU dumper for Sonix SN9C292x
Requires: Python 3.9+, PyUSB (pip install pyusb), and libusb-1.0 DLL available to PyUSB on Windows.

What it does
- Enumerates Sonix UVC cams (default VID:PID 0x0C45:0x6366), or any UVC device if you pass --vid/--pid.
- Sends UVC Extension Unit (XU) GET_CUR/SET_CUR transfers against the VideoControl interface.
- Provides a generic "xu-get" / "xu-set" for poking vendor controls.
- Provides "sf-read" convenience: chunked reads of external SPI flash via the Sonix XU control used by Kurokesu's tool.
  Default assumptions (override with args or config):
    * VideoControl interface index = 0
    * XU entity id = 3
    * Serial-flash control selector (CS) = 0x23 (SET for address/len) and 0x24 (GET data)
    * Payload: SET (5 bytes) -> [addr24 big-endian (3), len16 big-endian (2)]
              GET returns <len> bytes
If your firmware uses different unit-id / selectors, pass --xu, --cs-set, --cs-get explicitly.

Usage examples
# List devices
python snxuvc_dump.py scan

# Quick probe: read 64 bytes at 0x0 (try defaults xu/cs)
python snxuvc_dump.py sf-read --addr 0x0 --len 64 --out probe.bin

# Full dump (128 KiB)
python snxuvc_dump.py sf-read --addr 0x0 --len 0x20000 --out firmware_dump.bin --verify

# Raw XU read (entity 3, CS 0x10, 8 bytes)
python snxuvc_dump.py xu-get --xu 3 --cs 0x10 --len 8

# Raw XU write (entity 3, CS 0x10, payload bytes)
python snxuvc_dump.py xu-set --xu 3 --cs 0x10 --data 01 02 03 04

Notes
- Run in an elevated shell if Windows driver permissions block control transfers.
- Close any apps using the camera before running (MS Teams/Zoom/etc.).
- If multiple interfaces show up, use --vc-if to select the VideoControl interface (usually 0).
"""

import argparse
import binascii
import usb.core
import usb.util

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81

def find_device(vid=0x0C45, pid=0x6366):
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise SystemExit(f"No device found with VID:PID {vid:04x}:{pid:04x}. Use --vid/--pid or plug the device.")
    return dev

def detach_kernel_if_needed(dev, intf):
    # On Windows, usually not needed; on Linux it may be.
    try:
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
    except Exception:
        pass

def ctrl(dev, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=2000):
    return dev.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=timeout)

def uvc_xu_set(dev, vc_if, xu_id, cs, payload: bytes):
    # wValue: (CS << 8) | 0, wIndex: (EntityID << 8) | InterfaceNumber
    wValue = (cs << 8) | 0
    wIndex = (xu_id << 8) | vc_if
    return ctrl(dev, 0x21, UVC_SET_CUR, wValue, wIndex, payload)

def uvc_xu_get(dev, vc_if, xu_id, cs, length):
    wValue = (cs << 8) | 0
    wIndex = (xu_id << 8) | vc_if
    return bytes(ctrl(dev, 0xA1, UVC_GET_CUR, wValue, wIndex, length))

def parse_hex_bytes(lst):
    out = bytearray()
    for tok in lst:
        tok = tok.strip()
        if tok.startswith("0x") or tok.startswith("0X"):
            out.append(int(tok, 16) & 0xFF)
        else:
            out.append(int(tok, 16 if all(c in "0123456789abcdefABCDEF" for c in tok) else 10) & 0xFF)
    return bytes(out)

def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def cmd_scan(args):
    # Show all 0x0C45 devices by default
    found = list(usb.core.find(find_all=True, idVendor=args.vid))
    if not found:
        print("No USB devices with given VID found.")
        return
    for dev in found:
        if args.pid != -1 and dev.idProduct != args.pid:
            continue
        print(f"USB {dev.idVendor:04x}:{dev.idProduct:04x} Bus={getattr(dev, 'bus', '?')} Address={getattr(dev, 'address', '?')}")
        try:
            for cfg in dev:
                for intf in cfg:
                    print(f"  Config {cfg.bConfigurationValue} Interface {intf.bInterfaceNumber} Class={intf.bInterfaceClass:02x} SubClass={intf.bInterfaceSubClass:02x}")
        except Exception as e:
            print("  [warn] couldn't enumerate interfaces:", e)

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
    total = args.len
    chunk = args.chunk

    # Default Sonix-style protocol: SET to CS_SET with [addr24_be len16_be], then GET from CS_GET
    cs_set = args.cs_set
    cs_get = args.cs_get

    out_path = args.out
    with open(out_path, "wb") as f:
        remain = total
        cur = addr
        while remain > 0:
            this = min(remain, chunk)
            payload = bytes([(cur >> 16) & 0xFF, (cur >> 8) & 0xFF, cur & 0xFF, (this >> 8) & 0xFF, this & 0xFF])
            try:
                uvc_xu_set(dev, args.vc_if, args.xu, cs_set, payload)
                data = uvc_xu_get(dev, args.vc_if, args.xu, cs_get, this)
            except usb.core.USBError as e:
                print(f"[retry] addr=0x{cur:06X} len={this}: {e}")
                time.sleep(0.05)
                uvc_xu_set(dev, args.vc_if, args.xu, cs_set, payload)
                data = uvc_xu_get(dev, args.vc_if, args.xu, cs_get, this)

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

    print(f"Wrote: {out_path}")
    if args.verify:
        # Re-read file back through XU and compare hash
        import tempfile
        tmp = out_path + ".verify"
        try:
            with open(tmp, "wb") as vf:
                remain = total
                cur = addr
                while remain > 0:
                    this = min(remain, chunk)
                    payload = bytes([(cur >> 16) & 0xFF, (cur >> 8) & 0xFF, cur & 0xFF, (this >> 8) & 0xFF, this & 0xFF])
                    uvc_xu_set(dev, args.vc_if, args.xu, cs_set, payload)
                    data = uvc_xu_get(dev, args.vc_if, args.xu, cs_get, this)
                    vf.write(data)
                    cur += this
                    remain -= this
            import hashlib
            def sh(path):
                h=hashlib.sha256(); h.update(open(path,"rb").read()); return h.hexdigest()
            h1, h2 = sh(out_path), sh(tmp)
            print(f"SHA-256 orig: {h1}")
            print(f"SHA-256 read: {h2}")
            if h1 != h2:
                print("[WARN] Verification hash mismatch!")
            else:
                print("Verify OK")
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

def main():
    ap = argparse.ArgumentParser(description="Sonix UVC XU dumper (Windows-friendly via PyUSB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap.add_argument("--vid", type=lambda x:int(x,0), default=0x0C45, help="USB VID (default 0x0C45)")
    ap.add_argument("--pid", type=lambda x:int(x,0), default=0x6366, help="USB PID (default 0x6366; -1 to ignore)")
    ap.add_argument("--vc-if", type=int, default=0, help="VideoControl interface number (default 0)")

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
    sf.add_argument("--len", type=lambda x:int(x,0), required=True, help="total length to read (e.g. 0x20000)")
    sf.add_argument("--chunk", type=int, default=512, help="chunk size (<= 1023 is safe for control xfer)")
    sf.add_argument("--out", type=str, required=True, help="output file path")
    sf.add_argument("--progress", action="store_true", help="print progress")
    sf.add_argument("--verify", action="store_true", help="re-read and compare hash")
    sf.set_defaults(func=cmd_sf_read)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
'''
# Write to file
out = Path('/mnt/data/snxuvc_dump.py')
out.write_text(tool_code, encoding='utf-8')

print("Created:", out)
print("Size:", out.stat().st_size, "bytes")

