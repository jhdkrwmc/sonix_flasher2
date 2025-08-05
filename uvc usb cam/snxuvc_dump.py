#!/usr/bin/env python3
"""
snxuvc_dump.py — Windows-friendly UVC XU dumper for Sonix SN9C292x
Requires:  pip install pyusb libusb-package
Bind WinUSB to the camera's VideoControl interface (Interface 0) with Zadig.
"""
import argparse, binascii, time
import usb.core, usb.util
from usb.backend import libusb1

# backend wiring (uses libusb-package to find the DLL)
try:
    import libusb_package
    BACKEND = libusb1.get_backend(find_library=libusb_package.find_library)
except Exception:
    BACKEND = libusb1.get_backend()
if BACKEND is None:
    raise SystemExit("libusb backend not found. Install `libusb-package` or place libusb-1.0.dll on PATH.")

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81

def find_device(vid=0x0C45, pid=0x6366):
    dev = usb.core.find(idVendor=vid, idProduct=pid, backend=BACKEND)
    if dev is None:
        raise SystemExit(f"No device {vid:04x}:{pid:04x} found. Use --vid/--pid or plug the cam.")
    return dev

def detach_kernel_if_needed(dev, intf):
    try:
        if dev.is_kernel_driver_active(intf):
            dev.detach_kernel_driver(intf)
    except Exception:
        pass
        
import platform
from array import array

def ctrl(dev, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=3000):
    # Open the device handle explicitly, then talk EP0 via backend
    if getattr(dev._ctx, "handle", None) is None:
        dev._ctx.open()
    if platform.system() == "Windows":
        if (bmRequestType & 0x80):  # IN
            length = int(data_or_wLength)
            return dev._ctx.backend.ctrl_transfer(
                dev._ctx.handle, bmRequestType, bRequest, wValue, wIndex, length, timeout
            )
        else:  # OUT
            buf = data_or_wLength
            if isinstance(buf, (bytes, bytearray)):
                buf = array('B', buf)
            return dev._ctx.backend.ctrl_transfer(
                dev._ctx.handle, bmRequestType, bRequest, wValue, wIndex, buf, timeout
            )
    # Fallback path (non-Windows)
    return dev.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=timeout)

def uvc_xu_set(dev, vc_if, xu_id, cs, payload: bytes):
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
        if tok.lower().startswith("0x"):
            out.append(int(tok, 16) & 0xFF)
        else:
            base = 16 if all(c in "0123456789abcdefABCDEF" for c in tok) else 10
            out.append(int(tok, base) & 0xFF)
    return bytes(out)

def cmd_scan(args):
    found = list(usb.core.find(find_all=True, idVendor=args.vid, backend=BACKEND))
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
    addr, total, chunk = args.addr, args.length, args.chunk
    cs_set, cs_get = args.cs_set, args.cs_get
    if chunk <= 0 or chunk > 1023: chunk = 512
    if total <= 0: raise SystemExit("length must be > 0")
    with open(args.out, "wb") as f:
        remain, cur = total, addr
        while remain > 0:
            this = min(remain, chunk)
            payload = bytes([(cur>>16)&0xFF, (cur>>8)&0xFF, cur&0xFF, (this>>8)&0xFF, this&0xFF])
            for attempt in range(2):
                try:
                    uvc_xu_set(dev, args.vc_if, args.xu, cs_set, payload)
                    data = uvc_xu_get(dev, args.vc_if, args.xu, cs_get, this); break
                except usb.core.USBError:
                    if attempt==0: time.sleep(0.05); continue
                    raise
            if len(data)!=this: raise SystemExit(f"Short read at 0x{cur:06X}: got {len(data)} expected {this}")
            f.write(data); cur+=this; remain-=this
            if args.progress:
                done = total-remain; pct = (done*100.0)/total
                print(f"\rRead {done}/{total} bytes ({pct:5.1f}%)", end="", flush=True)
        if args.progress: print()
    print(f"Wrote: {args.out}")
    if args.verify:
        import hashlib
        h=hashlib.sha256()
        with open(args.out,"rb") as f:
            for blk in iter(lambda: f.read(65536), b""): h.update(blk)
        print("SHA-256:", h.hexdigest())

def main():
    ap = argparse.ArgumentParser(description="Sonix UVC XU dumper (Windows via PyUSB + libusb-package)")
    ap.add_argument("--vid", type=lambda x:int(x,0), default=0x0C45)
    ap.add_argument("--pid", type=lambda x:int(x,0), default=0x6366)
    ap.add_argument("--vc-if", type=int, default=0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan"); sc.set_defaults(func=cmd_scan)
    gx = sub.add_parser("xu-get"); gx.add_argument("--xu", type=int, required=True); gx.add_argument("--cs", type=lambda x:int(x,0), required=True); gx.add_argument("--len", type=int, required=True); gx.set_defaults(func=cmd_xu_get)
    sx = sub.add_parser("xu-set"); sx.add_argument("--xu", type=int, required=True); sx.add_argument("--cs", type=lambda x:int(x,0), required=True); sx.add_argument("--data", nargs="+", required=True); sx.set_defaults(func=cmd_xu_set)
    sf = sub.add_parser("sf-read"); sf.add_argument("--xu", type=int, default=3); sf.add_argument("--cs-set", type=lambda x:int(x,0), default=0x23); sf.add_argument("--cs-get", type=lambda x:int(x,0), default=0x24); sf.add_argument("--addr", type=lambda x:int(x,0), required=True); sf.add_argument("--length", type=lambda x:int(x,0), required=True); sf.add_argument("--chunk", type=int, default=512); sf.add_argument("--out", type=str, required=True); sf.add_argument("--progress", action="store_true"); sf.add_argument("--verify", action="store_true"); sf.set_defaults(func=cmd_sf_read)
    args = ap.parse_args(); args.func(args)

if __name__ == "__main__":
    main()
