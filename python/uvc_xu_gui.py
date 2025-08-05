#!/usr/bin/env python3
"""
UVC XU GUI — live video + vendor (Extension Unit) toggles
=========================================================

Tested target: Sonix SN9C292A (VID:0x0C45, PID:0x6366), but should work with any UVC cam
that accepts standard class-specific interface control requests for Extension Units while
streaming via the OS UVC driver.

Platform: Linux recommended. On Windows, usbvideo.sys usually blocks libusb access unless
you swap drivers (not recommended for this tool).

Dependencies:
  pip install opencv-python pyusb pillow numpy

Usage:
  python3 uvc_xu_gui.py --vid 0x0C45 --pid 0x6366 --device 0

Features:
- Live preview via OpenCV (select device index with --device).
- Standard camera sliders: brightness/contrast/saturation/gain/exposure (best effort via OpenCV).
- Extension Unit (vendor) control panel:
    * Pick Unit ID (default 3/4), set Selector, auto-read LEN/INFO when possible.
    * GET_CUR / SET_CUR, with payload entered as hex ("00 ff 01" or "000102").
    * Quick payload presets (all-zeros, single-bit toggles).
    * Save labels: give a friendly name to (Unit, Selector), stored in xu_labels.json.
- Brute-force helper: iterate selectors and try a few payloads; logs transfers (does not guess effects).

Known limits:
- Some devices reject GET_LEN/GET_INFO; you can manually specify payload length.
- Standard sliders map to OpenCV properties; behavior depends on driver support.
- If libusb can't open your device while streaming, run as root or on Linux detach/attach may be required.

Author: ChatGPT
"""
import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List

import cv2
import numpy as np

try:
    import usb.core
    import usb.util
except Exception as e:
    print("PyUSB import failed. Install with: pip install pyusb")
    raise

try:
    from PIL import Image, ImageTk
except Exception as e:
    print("Pillow import failed. Install with: pip install pillow")
    raise

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# ---------- UVC XU low-level helpers ----------

SET_CUR = 0x01
GET_CUR = 0x81
GET_MIN = 0x82
GET_MAX = 0x83
GET_RES = 0x84
GET_LEN = 0x85
GET_INFO = 0x86
GET_DEF = 0x87

REQ_SET_INTF = 0x21  # host->device, class, interface
REQ_GET_INTF = 0xA1  # device->host, class, interface

VC_INTERFACE_DEFAULT = 0  # most cams put VideoControl at interface #0

def parse_hex_bytes(s: str) -> bytes:
    s = s.strip().lower().replace("0x", "").replace(",", " ").replace(";", " ").replace("-", " ")
    s = " ".join(s.split())
    if not s:
        return b""
    if " " in s:
        parts = s.split()
        return bytes(int(p, 16) for p in parts)
    # no spaces: treat as continuous hex nybbles
    if len(s) % 2 == 1:
        s = "0" + s
    return bytes(int(s[i:i+2], 16) for i in range(0, len(s), 2))

def to_hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)

@dataclass
class XUAddress:
    unit_id: int
    selector: int
    interface: int = VC_INTERFACE_DEFAULT

class UVCXU:
    def __init__(self, vid: int, pid: int, interface: int = VC_INTERFACE_DEFAULT):
        self.vid = vid
        self.pid = pid
        self.interface = interface
        self.dev = None
        self._open_device()

    def _open_device(self):
        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            raise IOError(f"UVC device {self.vid:#06x}:{self.pid:#06x} not found. "
                          "Check permissions (try sudo) and that the camera is plugged in.")
        # We do not claim the interface; we only use control endpoint (ep0).
        # Some platforms require setting active configuration explicitly.
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            # Already configured or permission issue; ignore if harmless.
            pass

    def ctrl_transfer_get(self, addr: XUAddress, bRequest: int, wLength: int) -> bytes:
        wIndex = (addr.unit_id << 8) | addr.interface
        wValue = (addr.selector << 8) | 0x00
        return self.dev.ctrl_transfer(REQ_GET_INTF, bRequest, wValue, wIndex, wLength, timeout=2000)

    def ctrl_transfer_set(self, addr: XUAddress, bRequest: int, data: bytes) -> int:
        wIndex = (addr.unit_id << 8) | addr.interface
        wValue = (addr.selector << 8) | 0x00
        return self.dev.ctrl_transfer(REQ_SET_INTF, bRequest, wValue, wIndex, data, timeout=2000)

    def get_len(self, addr: XUAddress) -> Optional[int]:
        try:
            data = self.ctrl_transfer_get(addr, GET_LEN, 2)
            if len(data) == 2:
                return data[0] | (data[1] << 8)
            if len(data) == 1:
                return data[0]
            return None
        except usb.core.USBError:
            return None

    def get_info(self, addr: XUAddress) -> Optional[int]:
        try:
            data = self.ctrl_transfer_get(addr, GET_INFO, 1)
            return int(data[0]) if len(data) >= 1 else None
        except usb.core.USBError:
            return None

    def get_cur(self, addr: XUAddress, length: int) -> Optional[bytes]:
        try:
            return bytes(self.ctrl_transfer_get(addr, GET_CUR, length))
        except usb.core.USBError:
            return None

    def set_cur(self, addr: XUAddress, payload: bytes) -> bool:
        try:
            self.ctrl_transfer_set(addr, SET_CUR, payload)
            return True
        except usb.core.USBError:
            return False

# ---------- Label store ----------

class LabelStore:
    def __init__(self, path="xu_labels.json"):
        self.path = path
        self.data: Dict[str, Dict[str, Dict[str, str]]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
        except Exception as e:
            print("Failed to save labels:", e)

    def key(self, unit_id: int, selector: int) -> str:
        return f"U{unit_id}_S{selector}"

    def get_label(self, unit_id: int, selector: int) -> str:
        return self.data.get(self.key(unit_id, selector), {}).get("label", "")

    def set_label(self, unit_id: int, selector: int, label: str):
        self.data.setdefault(self.key(unit_id, selector), {})["label"] = label
        self.save()

    def list_all(self) -> List[Tuple[int,int,str]]:
        out = []
        for k, v in self.data.items():
            try:
                parts = k.split("_")
                u = int(parts[0][1:])
                s = int(parts[1][1:])
                out.append((u, s, v.get("label", "")))
            except Exception:
                continue
        out.sort()
        return out

# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self, vid: int, pid: int, device_index: int, default_units=(3,4), interface: int = 0):
        super().__init__()
        self.title("UVC XU GUI — Live + Vendor Controls")
        self.geometry("1200x760")

        self.vid = vid
        self.pid = pid
        self.interface = interface
        self.default_units = default_units
        self.cam_index = device_index

        # USB control
        self.xu = UVCXU(vid, pid, interface=interface)
        self.labels = LabelStore()

        # Video
        self.cap = cv2.VideoCapture(self.cam_index, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            messagebox.showerror("Camera Error", f"Cannot open video device index {self.cam_index}")
            sys.exit(1)

        # UI layout
        self._build_ui()

        # Start video thread
        self.running = True
        self.thread = threading.Thread(target=self._video_loop, daemon=True)
        self.thread.start()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)

        # ---- Left: Video preview ----
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self.video_label = ttk.Label(left, text="Preview")
        self.video_label.grid(row=0, column=0, sticky="w", padx=8, pady=4)

        self.canvas = tk.Label(left)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        # Standard controls
        std = ttk.LabelFrame(left, text="Standard Controls (best effort)")
        std.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        def add_slider(name, prop, frm=0, to=1, init=None):
            row = len(std.grid_slaves()) // 2
            ttk.Label(std, text=name).grid(row=row, column=0, sticky="w", padx=4, pady=2)
            s = ttk.Scale(std, from_=frm, to=to, orient="horizontal")
            s.grid(row=row, column=1, sticky="ew", padx=4, pady=2)
            std.grid_columnconfigure(1, weight=1)

            def on_release(event):
                val = s.get()
                try:
                    self.cap.set(prop, val)
                except Exception:
                    pass
            s.bind("<ButtonRelease-1>", on_release)
            if init is not None:
                s.set(init)

        # Map some typical OpenCV properties
        add_slider("Brightness", cv2.CAP_PROP_BRIGHTNESS, 0, 255)
        add_slider("Contrast",   cv2.CAP_PROP_CONTRAST,   0, 255)
        add_slider("Saturation", cv2.CAP_PROP_SATURATION, 0, 255)
        add_slider("Gain",       cv2.CAP_PROP_GAIN,       0, 255)
        add_slider("Exposure",   cv2.CAP_PROP_EXPOSURE,  -13, -1)  # usually log-scale on UVC

        # ---- Right: XU panel ----
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)

        xu_box = ttk.LabelFrame(right, text="Extension Unit (Vendor) Controls")
        xu_box.grid(row=0, column=0, sticky="ew", padx=8, pady=6)

        ttk.Label(xu_box, text="Unit ID:").grid(row=0, column=0, sticky="w")
        self.unit_var = tk.IntVar(value=self.default_units[0])
        self.unit_entry = ttk.Combobox(xu_box, values=[str(u) for u in self.default_units], width=6)
        self.unit_entry.set(str(self.default_units[0]))
        self.unit_entry.grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(xu_box, text="Selector:").grid(row=0, column=2, sticky="w")
        self.sel_var = tk.IntVar(value=1)
        self.sel_spin = tk.Spinbox(xu_box, from_=1, to=64, width=6, textvariable=self.sel_var)
        self.sel_spin.grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(xu_box, text="Len:").grid(row=0, column=4, sticky="w")
        self.len_var = tk.IntVar(value=3)
        self.len_entry = tk.Entry(xu_box, width=6, textvariable=self.len_var)
        self.len_entry.grid(row=0, column=5, sticky="w", padx=4)

        self.info_label = ttk.Label(xu_box, text="Info: ?")
        self.info_label.grid(row=0, column=6, sticky="w", padx=6)

        # Payload entry
        ttk.Label(xu_box, text="Payload (hex):").grid(row=1, column=0, sticky="w", pady=4)
        self.payload_entry = tk.Entry(xu_box, width=60)
        self.payload_entry.grid(row=1, column=1, columnspan=5, sticky="ew", padx=4, pady=4)

        self.cur_val = ttk.Label(xu_box, text="CUR: --")
        self.cur_val.grid(row=1, column=6, sticky="w")

        # Buttons row
        btns = ttk.Frame(xu_box)
        btns.grid(row=2, column=0, columnspan=7, sticky="ew")
        for i in range(6):
            btns.grid_columnconfigure(i, weight=1)

        ttk.Button(btns, text="GET_LEN", command=self.on_get_len).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="GET_INFO", command=self.on_get_info).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="GET_CUR", command=self.on_get_cur).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="SET_CUR", command=self.on_set_cur).grid(row=0, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Zeroes", command=lambda: self.set_payload("00"*max(1,self.len_var.get()))).grid(row=0, column=4, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Bit Toggle (01..)", command=self.on_bit_toggle).grid(row=0, column=5, sticky="ew", padx=2, pady=2)

        # Labels list
        lab_box = ttk.LabelFrame(right, text="Named Controls")
        lab_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)
        lab_box.grid_rowconfigure(1, weight=1)
        lab_box.grid_columnconfigure(0, weight=1)

        self.labels_list = tk.Listbox(lab_box)
        self.labels_list.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)
        self.labels_list.bind("<<ListboxSelect>>", self.on_label_select)

        ttk.Button(lab_box, text="Refresh", command=self.refresh_labels).grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Button(lab_box, text="Load Current into Fields", command=self.on_label_select).grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(lab_box, text="Name for (Unit, Selector):").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.name_entry = tk.Entry(lab_box, width=40)
        self.name_entry.grid(row=2, column=1, sticky="w", padx=4, pady=2)
        ttk.Button(lab_box, text="Save/Update", command=self.on_save_label).grid(row=2, column=2, sticky="w", padx=4, pady=2)

        self.refresh_labels()

        # Brute-force box
        brute = ttk.LabelFrame(right, text="Brute-force helper")
        brute.grid(row=2, column=0, sticky="ew", padx=8, pady=6)
        ttk.Label(brute, text="Selectors 1..N:").grid(row=0, column=0, sticky="w")
        self.brute_sel_max = tk.IntVar(value=24)
        tk.Entry(brute, textvariable=self.brute_sel_max, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(brute, text="Payload patterns (comma-separated hex): e.g. '00 00 00, 01 00 00, FF 00 00'").grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        self.brute_payloads = tk.Entry(brute, width=70)
        self.brute_payloads.insert(0, "00 00 00, 01 00 00, FF 00 00")
        self.brute_payloads.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(brute, text="Run (watch video, press Esc in preview to stop)", command=self.on_bruteforce).grid(row=3, column=0, sticky="w", pady=4)

        # Log box
        logbox = ttk.LabelFrame(right, text="Log")
        logbox.grid(row=3, column=0, sticky="nsew", padx=8, pady=6)
        logbox.grid_rowconfigure(0, weight=1)
        logbox.grid_columnconfigure(0, weight=1)
        self.log = tk.Text(logbox, height=10)
        self.log.grid(row=0, column=0, sticky="nsew")

    # -------- video loop --------
    def _video_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                # Convert to RGB for Tkinter
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                self.canvas.imgtk = imgtk
                self.canvas.configure(image=imgtk)
            else:
                time.sleep(0.05)

    # -------- helpers --------
    def current_addr(self) -> XUAddress:
        try:
            unit = int(self.unit_entry.get())
        except Exception:
            unit = self.default_units[0]
        sel = int(self.sel_spin.get())
        return XUAddress(unit_id=unit, selector=sel, interface=self.interface)

    def logln(self, s: str):
        self.log.insert("end", s + "\n")
        self.log.see("end")

    def set_payload(self, hexstr: str):
        self.payload_entry.delete(0, "end")
        self.payload_entry.insert(0, hexstr)

    # -------- button actions --------
    def on_get_len(self):
        addr = self.current_addr()
        n = self.xu.get_len(addr)
        if n is not None:
            self.len_var.set(int(n))
            self.logln(f"[GET_LEN] U{addr.unit_id} S{addr.selector} -> {n}")
        else:
            self.logln(f"[GET_LEN] U{addr.unit_id} S{addr.selector} -> not supported")

    def on_get_info(self):
        addr = self.current_addr()
        info = self.xu.get_info(addr)
        if info is None:
            self.info_label.config(text="Info: n/a")
            self.logln(f"[GET_INFO] U{addr.unit_id} S{addr.selector} -> n/a")
            return
        # Info bitfield per UVC spec: bit0=GET_SUPPORT, bit1=SET_SUPPORT, bit2=AUTOUPDATE, bit3=ASYNC
        bits = f"{info:08b}"
        self.info_label.config(text=f"Info: 0b{bits}")
        self.logln(f"[GET_INFO] U{addr.unit_id} S{addr.selector} -> 0b{bits}")

    def on_get_cur(self):
        addr = self.current_addr()
        n = int(self.len_entry.get())
        cur = self.xu.get_cur(addr, n)
        if cur is None:
            self.cur_val.config(text="CUR: n/a")
            self.logln(f"[GET_CUR] U{addr.unit_id} S{addr.selector} -> n/a")
        else:
            self.cur_val.config(text=f"CUR: {to_hex(cur)}")
            self.logln(f"[GET_CUR] U{addr.unit_id} S{addr.selector} -> {to_hex(cur)}")

    def on_set_cur(self):
        addr = self.current_addr()
        payload = parse_hex_bytes(self.payload_entry.get())
        if len(payload) == 0:
            messagebox.showwarning("Payload", "Enter hex payload (e.g., '00 00 00').")
            return
        ok = self.xu.set_cur(addr, payload)
        if ok:
            self.logln(f"[SET_CUR] U{addr.unit_id} S{addr.selector} <= {to_hex(payload)}")
        else:
            self.logln(f"[SET_CUR] U{addr.unit_id} S{addr.selector} FAILED")

    def on_bit_toggle(self):
        # Create a payload with length from Len entry, and set first byte to 01 toggling bits.
        n = int(self.len_entry.get())
        data = bytearray([0]*max(1, n))
        data[0] = 1
        self.set_payload(to_hex(bytes(data)))

    def refresh_labels(self):
        self.labels_list.delete(0, "end")
        for u, s, name in self.labels.list_all():
            disp = f"U{u} S{s} : {name}"
            self.labels_list.insert("end", disp)

    def on_label_select(self, event=None):
        sel = self.labels_list.curselection()
        if not sel:
            return
        text = self.labels_list.get(sel[0])
        try:
            # Parse "U3 S7 : name"
            head, _, name = text.partition(":")
            parts = head.strip().split()
            u = int(parts[0][1:])
            s = int(parts[1][1:])
            self.unit_entry.set(str(u))
            self.sel_var.set(s)
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, name.strip())
        except Exception:
            pass

    def on_save_label(self):
        try:
            u = int(self.unit_entry.get())
            s = int(self.sel_spin.get())
            name = self.name_entry.get().strip()
            if not name:
                messagebox.showwarning("Name", "Please enter a non-empty name.")
                return
            self.labels.set_label(u, s, name)
            self.refresh_labels()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save label: {e}")

    def on_bruteforce(self):
        try:
            u = int(self.unit_entry.get())
            max_sel = int(self.brute_sel_max.get())
            payloads_str = self.brute_payloads.get()
            payloads = []
            for chunk in payloads_str.split(","):
                b = parse_hex_bytes(chunk.strip())
                if b:
                    payloads.append(b)
            if not payloads:
                messagebox.showwarning("Brute force", "Enter at least one payload.")
                return
            self.logln(f"[BRUTE] Unit {u}, selectors 1..{max_sel}, {len(payloads)} payloads")
            for s in range(1, max_sel+1):
                addr = XUAddress(unit_id=u, selector=s, interface=self.interface)
                n = self.xu.get_len(addr) or len(payloads[0])
                cur = self.xu.get_cur(addr, n)
                self.logln(f"  S{s:02d} LEN={n} CUR={to_hex(cur) if cur else 'n/a'}")
                self.update()
                for p in payloads:
                    ok = self.xu.set_cur(addr, p)
                    self.logln(f"    SET {to_hex(p)} -> {'OK' if ok else 'FAIL'}")
                    self.update()
                    time.sleep(0.15)
        except Exception as e:
            messagebox.showerror("Brute force error", str(e))

    def on_close(self):
        self.running = False
        time.sleep(0.1)
        try:
            self.cap.release()
        except Exception:
            pass
        self.destroy()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vid", type=lambda x: int(x,0), default=0x0C45, help="USB Vendor ID (e.g., 0x0C45)")
    ap.add_argument("--pid", type=lambda x: int(x,0), default=0x6366, help="USB Product ID (e.g., 0x6366)")
    ap.add_argument("--device", type=int, default=0, help="OpenCV video device index")
    ap.add_argument("--interface", type=int, default=0, help="VideoControl interface number (usually 0)")
    args = ap.parse_args()

    app = App(args.vid, args.pid, args.device, interface=args.interface)
    app.mainloop()

if __name__ == "__main__":
    main()
